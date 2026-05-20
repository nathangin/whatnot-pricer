import threading
import time
import base64
import io
import json

import mss
import numpy as np
from PIL import Image
import anthropic

CLAUDE_MODEL = "claude-sonnet-4-20250514"

CLAUDE_PROMPT = (
    'You are analyzing a Whatnot live stream screenshot. A Pokemon card is being sold. '
    'Extract: cardName (Pokemon name), setName (if visible), cardNumber (e.g. 4/102), '
    'language (English/Japanese/Chinese/Korean), isHolo (bool), isGraded (bool), '
    'gradingCompany (PSA/BGS/CGC/null), grade (number/null), '
    'streamPrice (number shown on screen/null), condition (NM/LP/etc/null), '
    'confidence (high/medium/low). '
    'Respond ONLY with a JSON object, no other text. '
    'If no card visible return {"error": "no card"}'
)

NORMAL_INTERVAL = 2.5
SLOW_INTERVAL = 5.0
NO_CARD_SLOW_THRESHOLD = 3
DIFF_THRESHOLD = 8.0  # Mean absolute pixel diff to consider frame "new"
THUMB_SIZE = (160, 120)


class Monitor:
    def __init__(self, region, on_result, on_status):
        self.region = region
        self.on_result = on_result
        self.on_status = on_status
        self.running = False
        self.paused = False
        self._thread = None
        self._client = anthropic.Anthropic()
        self._prev_thumb = None
        self._no_card_streak = 0

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    # ------------------------------------------------------------------ loop

    def _loop(self):
        from prices import PriceCache
        price_cache = PriceCache()

        while self.running:
            if self.paused:
                time.sleep(0.5)
                continue

            interval = (
                SLOW_INTERVAL
                if self._no_card_streak >= NO_CARD_SLOW_THRESHOLD
                else NORMAL_INTERVAL
            )

            try:
                frame_bytes, thumb = self._capture()

                if self._prev_thumb is not None:
                    diff = self._pixel_diff(self._prev_thumb, thumb)
                    if diff < DIFF_THRESHOLD:
                        time.sleep(interval)
                        continue

                self._prev_thumb = thumb
                self.on_status("⏳ Fetching...")

                card_data = self._analyze(frame_bytes)

                if card_data is None or "error" in card_data:
                    self._no_card_streak += 1
                    label = (
                        "⏸ No card (slow mode)"
                        if self._no_card_streak >= NO_CARD_SLOW_THRESHOLD
                        else "👀 Watching..."
                    )
                    self.on_status(label)
                else:
                    self._no_card_streak = 0
                    card_name = card_data.get("cardName", "")
                    if card_name:
                        self.on_status(f"🔍 Looking up {card_name}...")
                        price_data = price_cache.lookup(
                            card_name,
                            card_data.get("setName"),
                            card_data.get("isHolo", False),
                            card_data.get("language", "English"),
                        )
                        self.on_result(card_data, price_data)
                    else:
                        self.on_status("👀 Watching...")

            except Exception as exc:
                print(f"Monitor error: {exc}")
                self.on_status(f"⚠️ {str(exc)[:40]}")

            time.sleep(interval)

    # --------------------------------------------------------------- capture

    def _capture(self):
        with mss.mss() as sct:
            shot = sct.grab(self.region)
            img = Image.frombytes("RGB", shot.size, shot.rgb)

        thumb = np.array(img.resize(THUMB_SIZE, Image.BILINEAR))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), thumb

    def _pixel_diff(self, a, b):
        if a.shape != b.shape:
            return 999.0
        return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))

    # ---------------------------------------------------------------- claude

    def _analyze(self, frame_bytes):
        img_b64 = base64.standard_b64encode(frame_bytes).decode("utf-8")
        text = ""
        for attempt in range(2):
            try:
                response = self._client.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=512,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": img_b64,
                                    },
                                },
                                {"type": "text", "text": CLAUDE_PROMPT},
                            ],
                        }
                    ],
                )
                text = response.content[0].text.strip()
                # Strip markdown fences if model wraps in ```json ... ```
                if text.startswith("```"):
                    parts = text.split("```")
                    text = parts[1]
                    if "\n" in text:
                        text = text.split("\n", 1)[1]
                    text = text.strip()
                return json.loads(text)

            except json.JSONDecodeError:
                print(f"JSON parse error, response: {text[:120]}")
                return None
            except anthropic.RateLimitError:
                wait = 5 * (attempt + 1)
                print(f"Rate limited — waiting {wait}s")
                time.sleep(wait)
            except anthropic.APIStatusError as exc:
                print(f"API error {exc.status_code}: {exc.message}")
                if attempt == 1:
                    raise
                time.sleep(2)

        return None
