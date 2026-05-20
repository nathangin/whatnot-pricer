import time
from threading import Lock

import requests

POKEMONTCG_API = "https://api.pokemontcg.io/v2/cards"
CACHE_TTL = 600  # 10 minutes

FOREIGN_LANGUAGES = {"japanese", "chinese", "korean"}

# Map holo status to TCGPlayer price bucket preference order
HOLO_PRICE_PREF = ["holofoil", "reverseHolofoil", "1stEditionHolofoil"]
NORMAL_PRICE_PREF = ["normal", "1stEditionNormal", "unlimitedHolofoil"]


class PriceCache:
    def __init__(self):
        self._cache: dict = {}
        self._lock = Lock()

    def lookup(self, card_name: str, set_name=None, is_holo=False,
               language="English") -> dict:
        cache_key = f"{card_name.lower()}|{(set_name or '').lower()}|{is_holo}"

        with self._lock:
            entry = self._cache.get(cache_key)
            if entry and time.time() - entry["ts"] < CACHE_TTL:
                return entry["data"]

        data = self._fetch(card_name, set_name, is_holo, language)

        with self._lock:
            self._cache[cache_key] = {"ts": time.time(), "data": data}

        return data

    # ----------------------------------------------------------------- fetch

    def _fetch(self, card_name: str, set_name, is_holo: bool,
               language: str) -> dict:
        result = {
            "card_name": card_name,
            "language": language,
            "is_foreign": language.lower() in FOREIGN_LANGUAGES,
            "cards": [],
            "best_match": None,
            "error": None,
        }

        clean_name = card_name.replace('"', "").replace("'", "").strip()
        if not clean_name:
            result["error"] = "empty card name"
            return result

        try:
            cards = self._query(clean_name, set_name)
            if not cards and set_name:
                # Retry without set filter
                cards = self._query(clean_name, None)

            parsed = []
            for card in cards:
                info = self._parse_card(card, is_holo)
                parsed.append(info)
            result["cards"] = parsed

            # Best match: first card that has a market price
            for card in parsed:
                if card["prices"].get("market"):
                    result["best_match"] = card
                    break
            if result["best_match"] is None and parsed:
                result["best_match"] = parsed[0]

        except requests.RequestException as exc:
            result["error"] = str(exc)

        return result

    def _query(self, card_name: str, set_name) -> list:
        q = f'name:"{card_name}"'
        if set_name:
            clean_set = set_name.replace('"', "").replace("'", "").strip()
            q += f' set.name:"{clean_set}"'

        resp = requests.get(
            POKEMONTCG_API,
            params={"q": q, "orderBy": "-set.releaseDate", "pageSize": 6},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def _parse_card(self, card: dict, is_holo: bool) -> dict:
        prices_raw = card.get("tcgplayer", {}).get("prices", {})
        pref = HOLO_PRICE_PREF if is_holo else NORMAL_PRICE_PREF
        fallback = NORMAL_PRICE_PREF if is_holo else HOLO_PRICE_PREF

        chosen_type, chosen_prices = None, {}
        for bucket in pref + fallback:
            if bucket in prices_raw:
                chosen_type = bucket
                p = prices_raw[bucket]
                chosen_prices = {
                    "market": p.get("market"),
                    "low": p.get("low"),
                    "mid": p.get("mid"),
                    "high": p.get("high"),
                    "type": bucket,
                }
                break

        return {
            "id": card.get("id"),
            "name": card.get("name"),
            "set": card.get("set", {}).get("name"),
            "number": card.get("number"),
            "rarity": card.get("rarity"),
            "prices": chosen_prices,
            "price_type": chosen_type,
        }
