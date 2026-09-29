import base64
import io
import math
import time

import anthropic
import pytest
from PIL import Image

import monitor
from fakes import CHARIZARD, FakeClient, api_error, solid, text_response, tool_response
from monitor import (
    CARD_TOOL,
    NORMAL_INTERVAL,
    SLOW_INTERVAL,
    Monitor,
    backoff_delay,
    encode_jpeg,
    extract_tool_input,
    frame_changed,
    make_thumbnail,
    normalize_card,
    pixel_diff,
    resolve_model,
)

RED, GREEN, BLUE, GRAY = (solid(c) for c in [(200, 30, 30), (30, 200, 30), (30, 30, 200), (128, 128, 128)])
NO_CARD = {"cardVisible": False, "cardName": None, "language": "English", "isHolo": False,
           "streamPrice": None, "confidence": "high"}
TOOL_CHOICE_400 = 'tool_choice: type "tool" and "any" are not supported for this model.'


class FakePrices:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"is_foreign": False, "best_match": None, "error": None}

    def lookup(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class Harness:
    """A Monitor wired to fakes: scripted frames in, recorded callbacks out."""

    def __init__(self, frames, *outcomes, prices=None):
        self.frames = list(frames)
        self.results, self.statuses = [], []
        self.client = FakeClient(*outcomes)
        self.prices = prices or FakePrices()
        self.monitor = Monitor(
            {"left": 0, "top": 0, "width": 640, "height": 480},
            on_result=lambda card, price: self.results.append((card, price)),
            on_status=self.statuses.append,
            model="test-model", client=self.client, price_cache=self.prices,
            grab=lambda region: self.frames.pop(0),
        )

    @property
    def api_calls(self):
        return self.client.messages.calls


# ------------------------------------------------------------------ model

def test_default_model_and_env_override(monkeypatch):
    assert resolve_model({}) == "claude-sonnet-5"
    assert resolve_model({"WHATNOT_MODEL": "  "}) == "claude-sonnet-5"
    assert resolve_model({"WHATNOT_MODEL": " claude-haiku-4-5-20251001 "}) == "claude-haiku-4-5-20251001"

    monkeypatch.setenv("WHATNOT_MODEL", "claude-haiku-4-5-20251001")
    m = Monitor({}, print, print, client=FakeClient(), price_cache=FakePrices())
    assert m.model == "claude-haiku-4-5-20251001"


def test_default_client_has_short_timeout(monkeypatch):
    created = {}
    monkeypatch.setattr(monitor.anthropic, "Anthropic", lambda **kw: created.update(kw) or object())
    Monitor({}, print, print, price_cache=FakePrices())
    assert created == {"timeout": monitor.API_TIMEOUT, "max_retries": monitor.API_MAX_RETRIES}


# ----------------------------------------------------------------- frames

def test_thumbnail_is_small_rgb():
    thumb = make_thumbnail(Image.new("RGBA", (1920, 1080), (1, 2, 3, 4)))
    assert thumb.shape == (120, 160, 3)


def test_frame_change_threshold():
    base = make_thumbnail(solid((100, 100, 100)))
    assert frame_changed(None, base)
    assert not frame_changed(base, base)
    assert not frame_changed(base, make_thumbnail(solid((107, 107, 107))))   # diff 7
    assert frame_changed(base, make_thumbnail(solid((108, 108, 108))))       # diff 8
    assert pixel_diff(base, base[:10]) == math.inf


def test_encode_jpeg_downscales_large_captures():
    big = solid((10, 20, 30), size=(3000, 2000))
    decoded = Image.open(io.BytesIO(encode_jpeg(big)))
    assert decoded.format == "JPEG" and max(decoded.size) == monitor.MAX_IMAGE_EDGE
    assert big.size == (3000, 2000)   # the capture itself is untouched

    small = Image.open(io.BytesIO(encode_jpeg(solid((0, 0, 0), size=(640, 480)))))
    assert small.size == (640, 480)


def test_backoff_doubles_and_caps():
    assert [backoff_delay(n) for n in range(7)] == [0.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert backoff_delay(10_000) == 60.0   # no float overflow after a long outage


# ------------------------------------------------------ tool-use handling

def test_extract_tool_input_from_sdk_message():
    assert extract_tool_input(tool_response(CHARIZARD)) == CHARIZARD
    assert extract_tool_input(tool_response(CHARIZARD, name="other_tool")) is None
    assert extract_tool_input(text_response("I can't see a card.")) is None


def test_normalize_card_full_input():
    assert normalize_card(CHARIZARD) == {
        "cardName": "Charizard", "setName": "Base", "cardNumber": "4/102", "language": "English",
        "isHolo": True, "isGraded": False, "gradingCompany": None, "grade": None,
        "streamPrice": 300.0, "condition": "LP", "confidence": "high",
    }


@pytest.mark.parametrize("raw", [
    NO_CARD,
    {"cardVisible": True, "cardName": None},
    {"cardVisible": True, "cardName": "  "},
    {"cardVisible": True, "cardName": "null"},
    None,
    ["not", "a", "dict"],
])
def test_normalize_card_no_card(raw):
    assert normalize_card(raw) is None


def test_normalize_card_coerces_loose_values():
    card = normalize_card({
        "cardName": " Umbreon VMAX ", "setName": "null", "cardNumber": "215/203",
        "language": "japanese", "isHolo": "true", "isGraded": True, "gradingCompany": "psa",
        "grade": "9.5", "streamPrice": "$1,250.50", "confidence": "HIGH",
    })
    assert card["cardName"] == "Umbreon VMAX" and card["setName"] is None
    assert card["language"] == "Japanese" and card["isHolo"] is True
    assert card["gradingCompany"] == "PSA" and card["grade"] == 9.5
    assert card["streamPrice"] == 1250.5 and card["confidence"] == "high"


def test_normalize_card_defaults_for_missing_or_bad_values():
    card = normalize_card({"cardVisible": True, "cardName": "Pikachu", "streamPrice": "ask",
                           "grade": True, "confidence": "certain"})
    assert card["language"] == "English" and card["isHolo"] is False
    assert card["streamPrice"] is None and card["grade"] is None
    assert card["confidence"] == "low"
    for bad in (-5, "-5", "25-30", float("nan"), float("inf"), True, [], "1.2.3"):
        assert normalize_card({"cardName": "Pikachu", "streamPrice": bad})["streamPrice"] is None


# ------------------------------------------------------------ monitor loop

def test_new_frame_is_sent_with_forced_tool_use_and_priced():
    price_data = {"is_foreign": False, "best_match": {"prices": {"market": 350.0}}, "error": None}
    h = Harness([RED], tool_response(CHARIZARD), prices=FakePrices(price_data))

    assert h.monitor.step() == NORMAL_INTERVAL

    call = h.api_calls[0]
    assert call["model"] == "test-model"
    assert call["tools"] == [CARD_TOOL]
    assert call["tool_choice"] == {"type": "tool", "name": "report_card", "disable_parallel_tool_use": True}
    image, text = call["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert base64.standard_b64decode(image["source"]["data"])[:2] == b"\xff\xd8"  # JPEG magic
    assert text["type"] == "text"

    assert h.prices.calls == [(("Charizard", "Base", True, "English"), {"card_number": "4/102"})]
    assert h.results == [(normalize_card(CHARIZARD), price_data)]
    assert h.statuses == ["⏳ Fetching...", "\U0001f50d Looking up Charizard..."]


def test_unchanged_frames_are_skipped():
    h = Harness([RED, RED, RED, BLUE], tool_response(NO_CARD), tool_response(CHARIZARD))
    for _ in range(4):
        h.monitor.step()
    assert len(h.api_calls) == 2          # first RED and BLUE only
    assert len(h.results) == 1


def test_no_card_frames_switch_to_slow_mode_until_a_card_appears():
    h = Harness([RED, GREEN, BLUE, GRAY], *(tool_response(NO_CARD) for _ in range(3)),
                tool_response(CHARIZARD))
    delays = [h.monitor.step() for _ in range(4)]
    assert delays == [NORMAL_INTERVAL, NORMAL_INTERVAL, SLOW_INTERVAL, NORMAL_INTERVAL]
    assert "\U0001f440 Watching..." in h.statuses
    assert "⏸ No card (slow mode)" in h.statuses
    assert h.prices.calls and len(h.results) == 1


def test_rate_limit_backs_off_and_retries_the_same_frame():
    h = Harness([RED, RED, RED, RED, BLUE],
                api_error(anthropic.RateLimitError, 429),
                api_error(anthropic.RateLimitError, 429),
                tool_response(CHARIZARD),
                api_error(anthropic.RateLimitError, 429))

    assert h.monitor.step() == 5.0
    assert h.statuses[-1] == "⚠️ Rate limited, retrying in 5s"
    assert h.monitor.step() == 10.0        # same frame re-sent, longer wait
    assert h.monitor.step() == NORMAL_INTERVAL
    assert len(h.results) == 1
    assert h.monitor.step() == NORMAL_INTERVAL   # RED again: now seen, so skipped
    assert h.monitor.step() == 5.0         # success reset the backoff
    assert len(h.api_calls) == 4


@pytest.mark.parametrize("error, label", [
    (api_error(anthropic.AuthenticationError, 401), "Invalid API key"),
    (api_error(anthropic.BadRequestError, 400, "invalid image"), "API error 400"),
    (api_error(anthropic.InternalServerError, 500), "API error 500"),
    (api_error(anthropic.OverloadedError, 529), "API error 529"),
    (anthropic.APIConnectionError(request=None), "Network error"),
    (anthropic.APITimeoutError(request=None), "API timed out"),
])
def test_api_errors_become_status_messages(error, label):
    h = Harness([RED], error)
    assert h.monitor.step() == 5.0
    assert h.statuses[-1] == f"⚠️ {label}, retrying in 5s"
    assert h.results == []


def test_models_without_forced_tool_use_fall_back_to_auto():
    h = Harness([RED, BLUE],
                api_error(anthropic.BadRequestError, 400, TOOL_CHOICE_400),
                tool_response(CHARIZARD),
                text_response("There is no card in this frame."))

    assert h.monitor.step() == NORMAL_INTERVAL
    assert [c["tool_choice"]["type"] for c in h.api_calls] == ["tool", "auto"]
    assert len(h.results) == 1

    h.monitor.step()                       # remembered: goes straight to auto
    assert h.api_calls[-1]["tool_choice"]["type"] == "auto"
    assert len(h.api_calls) == 3
    assert h.statuses[-1] == "\U0001f440 Watching..."   # a text reply counts as "no card"


def test_truncated_tool_input_is_treated_as_no_card():
    h = Harness([RED], tool_response({"cardVisible": True}, stop_reason="max_tokens"))
    assert h.monitor.step() == NORMAL_INTERVAL
    assert h.results == []


def test_tick_survives_capture_errors():
    def broken_grab(region):
        raise OSError("screen grab failed")

    h = Harness([])
    h.monitor._grab = broken_grab
    assert h.monitor.tick() == NORMAL_INTERVAL
    assert h.statuses == ["⚠️ screen grab failed"]


def test_thread_pauses_and_stops_promptly():
    class AlwaysNoCard:
        def __init__(self):
            self.calls = 0
            self.messages = self

        def create(self, **kwargs):
            self.calls += 1
            return tool_response(NO_CARD)

    client = AlwaysNoCard()
    m = Monitor({}, lambda *a: None, lambda s: None, client=client,
                price_cache=FakePrices(), grab=lambda region: RED)
    m.pause()
    m.start()
    time.sleep(0.2)
    assert client.calls == 0               # paused: nothing sent

    m.resume()
    deadline = time.monotonic() + 2.0      # first frame within one pause poll
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.calls == 1

    m.stop()                               # wakes the 2.5 s wait immediately
    m._thread.join(timeout=1.0)
    assert not m._thread.is_alive()
