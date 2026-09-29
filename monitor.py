"""Capture loop: grab the stream region, skip unchanged frames, ask Claude which
card is on screen (forced tool use), then look up its price.

The frame helpers, tool-input normalisation and backoff are plain functions so
they can be unit tested without a screen or an API key.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import os
import re
import threading
from typing import Any, Callable, Mapping

import anthropic
import numpy as np
from PIL import Image

from prices import PriceCache

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ constants

MODEL_ENV_VAR = "WHATNOT_MODEL"
DEFAULT_MODEL = "claude-sonnet-5"   # faster and cheaper: claude-haiku-4-5-20251001
MAX_TOKENS = 512
API_TIMEOUT = 30.0      # seconds per request (the SDK default is 10 minutes)
API_MAX_RETRIES = 1     # quick SDK-level retry; longer outages use the backoff below

NORMAL_INTERVAL = 2.5   # seconds between captures
SLOW_INTERVAL = 5.0     # used after NO_CARD_SLOW_THRESHOLD frames without a card
NO_CARD_SLOW_THRESHOLD = 3
PAUSE_POLL = 0.5
BACKOFF_BASE = 5.0      # first wait after an API error, doubled for each failure in a row
BACKOFF_MAX = 60.0

DIFF_THRESHOLD = 8.0    # mean absolute pixel difference (0-255) that counts as a new frame
THUMB_SIZE = (160, 120)
JPEG_QUALITY = 85
MAX_IMAGE_EDGE = 1568   # px; larger captures are downscaled before upload

LANGUAGES = ("English", "Japanese", "Chinese", "Korean", "Other")
CONFIDENCE_LEVELS = ("high", "medium", "low")
_NULL_STRINGS = {"", "null", "none", "n/a", "unknown"}

CARD_TOOL_NAME = "report_card"
CARD_TOOL: dict[str, Any] = {
    "name": CARD_TOOL_NAME,
    "description": (
        "Report the Pokemon card that is currently being sold in a Whatnot live stream "
        "screenshot. Call this exactly once per screenshot. If no Pokemon card is clearly "
        "visible, set cardVisible to false; the other fields are then ignored. Every value "
        "must come from what is visible on screen (the card, its slab label, the stream title "
        "or the price display); use null for anything you cannot read instead of guessing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cardVisible": {"type": "boolean",
                            "description": "True if a Pokemon card is visible and being sold."},
            "cardName": {"type": ["string", "null"],
                         "description": "Card name as printed, including suffixes such as ex, V, "
                                        "VMAX or GX, e.g. 'Charizard ex'."},
            "setName": {"type": ["string", "null"],
                        "description": "English set name if shown or identifiable, e.g. 'Paldean Fates'."},
            "cardNumber": {"type": ["string", "null"],
                           "description": "Collector number as printed, e.g. '4/102' or 'TG05/TG30'."},
            "language": {"type": "string", "enum": list(LANGUAGES),
                         "description": "Language the card is printed in."},
            "isHolo": {"type": "boolean",
                       "description": "True for holofoil or reverse holofoil cards."},
            "isGraded": {"type": "boolean",
                         "description": "True if the card is in a grading slab."},
            "gradingCompany": {"type": ["string", "null"],
                               "description": "Grading company on the slab label, e.g. PSA, BGS or CGC."},
            "grade": {"type": ["number", "null"],
                      "description": "Numeric grade on the slab label, e.g. 9.5."},
            "streamPrice": {"type": ["number", "null"],
                            "description": "Current bid or Buy It Now price shown in the stream, "
                                           "in US dollars, as a plain number."},
            "condition": {"type": ["string", "null"],
                          "description": "Condition if stated: NM, LP, MP, HP or DMG."},
            "confidence": {"type": "string", "enum": list(CONFIDENCE_LEVELS),
                           "description": "Confidence in the card identification."},
        },
        "required": ["cardVisible", "cardName", "language", "isHolo", "streamPrice", "confidence"],
    },
}

CARD_PROMPT = (
    "This is a screenshot of a Whatnot live stream where Pokemon cards are sold. "
    "Identify the card currently being sold and report it with the report_card tool. "
    "Read the card name and collector number exactly as printed. streamPrice is the "
    "price the stream shows on screen for this card (current bid or Buy It Now)."
)


# ================================================================ pure logic

def resolve_model(env: Mapping[str, str] | None = None) -> str:
    """Model ID from WHATNOT_MODEL, falling back to DEFAULT_MODEL."""
    env = os.environ if env is None else env
    return (env.get(MODEL_ENV_VAR) or "").strip() or DEFAULT_MODEL


def make_thumbnail(img: Image.Image) -> np.ndarray:
    """Small RGB array used only for frame-change detection."""
    small = img.convert("RGB").resize(THUMB_SIZE, Image.Resampling.BILINEAR)
    return np.asarray(small, dtype=np.uint8)


def pixel_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference between two thumbnails (0-255)."""
    if a.shape != b.shape:
        return math.inf
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))


def frame_changed(prev: np.ndarray | None, curr: np.ndarray,
                  threshold: float = DIFF_THRESHOLD) -> bool:
    """True if there is no previous frame or the picture changed enough."""
    return prev is None or pixel_diff(prev, curr) >= threshold


def encode_jpeg(img: Image.Image, max_edge: int = MAX_IMAGE_EDGE,
                quality: int = JPEG_QUALITY) -> bytes:
    """JPEG bytes for the API, downscaled so the long edge is at most max_edge."""
    img = img.convert("RGB")
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def backoff_delay(failures: int, base: float = BACKOFF_BASE, cap: float = BACKOFF_MAX) -> float:
    """Exponential backoff: base, 2*base, 4*base, ... capped at `cap`."""
    if failures <= 0:
        return 0.0
    return min(cap, base * 2 ** min(failures - 1, 16))


def extract_tool_input(response: Any, tool_name: str = CARD_TOOL_NAME) -> dict[str, Any] | None:
    """Input dict of the first tool_use block for `tool_name`, if any."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = getattr(block, "input", None)
            return data if isinstance(data, dict) else None
    return None


def _clean_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return None if text.lower() in _NULL_STRINGS else text


def _to_number(value: Any) -> float | None:
    """Accept 25, 25.0 or strings like "$1,250.00"; reject anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:  # drop "$", "," and spaces; "25-30" or "-5" stay invalid
            number = float(re.sub(r"[^\d.\-]", "", value))
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def normalize_card(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce Claude's tool input into the card dict the overlay expects.

    Returns None when no card is visible or no name could be read. Every key
    is always present, with None for unknown values.
    """
    if not isinstance(raw, dict) or raw.get("cardVisible") is False:
        return None
    name = _clean_str(raw.get("cardName"))
    if not name:
        return None
    language = (_clean_str(raw.get("language")) or "English").capitalize()
    confidence = (_clean_str(raw.get("confidence")) or "").lower()
    company = _clean_str(raw.get("gradingCompany"))
    return {
        "cardName": name,
        "setName": _clean_str(raw.get("setName")),
        "cardNumber": _clean_str(raw.get("cardNumber")),
        "language": language,
        "isHolo": _to_bool(raw.get("isHolo")),
        "isGraded": _to_bool(raw.get("isGraded")),
        "gradingCompany": company.upper() if company else None,
        "grade": _to_number(raw.get("grade")),
        "streamPrice": _to_number(raw.get("streamPrice")),
        "condition": _clean_str(raw.get("condition")),
        "confidence": confidence if confidence in CONFIDENCE_LEVELS else "low",
    }


def api_error_label(exc: Exception) -> str:
    """Short description of an Anthropic SDK error for the status bar."""
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited"
    if isinstance(exc, anthropic.AuthenticationError):
        return "Invalid API key"
    if isinstance(exc, anthropic.APITimeoutError):
        return "API timed out"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Network error"
    if isinstance(exc, anthropic.APIStatusError):
        return f"API error {exc.status_code}"
    return "API error"


# ====================================================================== I/O

def grab_region(region: dict[str, int]) -> Image.Image:
    """Screenshot of the region. mss is imported here because it needs a display."""
    import mss

    with mss.MSS() as sct:
        shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.rgb)


class Monitor:
    """Background thread that watches a screen region and reports cards.

    ``on_result(card, price_data)`` and ``on_status(message)`` are called from
    the worker thread; the caller is responsible for handing them to the UI
    thread. ``client``, ``price_cache`` and ``grab`` can be injected for tests.
    """

    def __init__(self, region: dict[str, int],
                 on_result: Callable[[dict[str, Any], dict[str, Any]], None],
                 on_status: Callable[[str], None], *,
                 model: str | None = None, client: Any = None,
                 price_cache: PriceCache | None = None,
                 grab: Callable[[dict[str, int]], Image.Image] = grab_region) -> None:
        self.region = region
        self.on_result = on_result
        self.on_status = on_status
        self.model = model or resolve_model()
        self.paused = False
        self._client = client or anthropic.Anthropic(timeout=API_TIMEOUT, max_retries=API_MAX_RETRIES)
        self._prices = price_cache or PriceCache()
        self._grab = grab
        self._force_tool = True
        self._prev_thumb: np.ndarray | None = None
        self._no_card_streak = 0
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- control

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            delay = PAUSE_POLL if self.paused else self.tick()
            self._stop.wait(delay)

    def tick(self) -> float:
        """One loop iteration that never raises. Returns seconds to wait."""
        try:
            return self.step()
        except Exception as exc:  # keep the thread alive whatever happens
            log.exception("Monitor error")
            self.on_status(f"⚠️ {str(exc)[:40]}")
            return NORMAL_INTERVAL

    def step(self) -> float:
        """Capture a frame and, if it changed, identify and price the card."""
        img = self._grab(self.region)
        thumb = make_thumbnail(img)
        if not frame_changed(self._prev_thumb, thumb):
            return self._interval()

        self.on_status("⏳ Fetching...")
        try:
            card = self.analyze(encode_jpeg(img))
        except anthropic.AnthropicError as exc:
            # The frame is not marked as seen, so it is retried after the wait.
            self._failures += 1
            delay = backoff_delay(self._failures)
            label = api_error_label(exc)
            log.warning("%s (%s); retrying in %.0fs", label, exc, delay)
            self.on_status(f"⚠️ {label}, retrying in {delay:.0f}s")
            return delay

        self._failures = 0
        self._prev_thumb = thumb
        if card is None:
            self._no_card_streak += 1
            self.on_status("⏸ No card (slow mode)" if self._slow() else "\U0001f440 Watching...")
            return self._interval()

        self._no_card_streak = 0
        self.on_status(f"\U0001f50d Looking up {card['cardName']}...")
        price_data = self._prices.lookup(
            card["cardName"], card["setName"], card["isHolo"], card["language"],
            card_number=card["cardNumber"],
        )
        self.on_result(card, price_data)
        return self._interval()

    def _slow(self) -> bool:
        return self._no_card_streak >= NO_CARD_SLOW_THRESHOLD

    def _interval(self) -> float:
        return SLOW_INTERVAL if self._slow() else NORMAL_INTERVAL

    # -------------------------------------------------------------- claude

    def analyze(self, jpeg: bytes) -> dict[str, Any] | None:
        """Send one frame to Claude and return the normalised card, or None."""
        try:
            response = self._request(jpeg)
        except anthropic.BadRequestError as exc:
            # Some newer models (e.g. Sonnet 5.5, Opus 5.5) reject forced tool
            # use with a 400 that names tool_choice; switch to "auto" once.
            if not (self._force_tool and "tool_choice" in str(exc)):
                raise
            log.warning("%s rejects forced tool_choice; using tool_choice=auto", self.model)
            self._force_tool = False
            response = self._request(jpeg)

        if getattr(response, "stop_reason", None) == "max_tokens":
            log.warning("Response hit max_tokens (%d); tool input may be incomplete", MAX_TOKENS)
        return normalize_card(extract_tool_input(response))

    def _request(self, jpeg: bytes) -> Any:
        if self._force_tool:
            tool_choice = {"type": "tool", "name": CARD_TOOL_NAME, "disable_parallel_tool_use": True}
        else:
            tool_choice = {"type": "auto", "disable_parallel_tool_use": True}
        return self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            tools=[CARD_TOOL],
            tool_choice=tool_choice,
            messages=[{
                "role": "user",
                "content": [
                    {   # image first, then the instructions (recommended for vision)
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(jpeg).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": CARD_PROMPT},
                ],
            }],
        )
