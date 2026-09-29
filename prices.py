"""TCGPlayer price lookup through the pokemontcg.io v2 API, plus the deal rating.

Only ``PriceCache._search`` touches the network; the query building, price
selection and deal rating below are plain functions so they can be unit tested
offline.
"""

from __future__ import annotations

import logging
import os
import re
import time
from threading import Lock
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ constants

POKEMONTCG_API = "https://api.pokemontcg.io/v2/cards"
API_KEY_ENV_VAR = "POKEMONTCG_API_KEY"   # optional: raises the daily rate limit
API_KEY_HEADER = "X-Api-Key"
REQUEST_TIMEOUT = 10                     # seconds
PAGE_SIZE = 6
ORDER_BY = "-set.releaseDate"            # newest printing first
CACHE_TTL = 600                          # 10 minutes

# TCGPlayer price buckets in order of preference. Unlimited is preferred over
# 1st Edition because the edition isn't detected and unlimited copies are far
# more common.
HOLO_PRICE_PREF = ("holofoil", "reverseHolofoil", "unlimitedHolofoil", "1stEditionHolofoil")
NORMAL_PRICE_PREF = ("normal", "1stEditionNormal")

# Deal rating: ratio = stream price / TCGPlayer market price. The first
# threshold the ratio is <= wins; anything above the last one is "over".
DEAL_THRESHOLDS = (
    (0.80, "great"),
    (0.95, "good"),
    (1.05, "fair"),
)
DEAL_OVER = "over"
DEAL_NONE = "none"        # stream price or market price unknown
DEAL_FOREIGN = "foreign"  # non-English card: TCGPlayer prices don't apply
DEAL_KEYS = (*(key for _, key in DEAL_THRESHOLDS), DEAL_OVER, DEAL_NONE, DEAL_FOREIGN)

# "025/165" -> number 25 of a 165-card set; "TG05/TG30" -> TG05 of 30.
_CARD_NUMBER_RE = re.compile(
    r"^#?\s*([A-Za-z]*)(\d+)([A-Za-z]?)\s*(?:/\s*[A-Za-z]*(\d+))?$"
)


# ================================================================ pure logic

def is_foreign(language: str | None) -> bool:
    """TCGPlayer prices on pokemontcg.io are for English cards only."""
    return (language or "English").strip().lower() != "english"


def clean_term(text: str | None) -> str:
    """Make free text from the vision model safe to put inside a quoted term.

    Double quotes would end the phrase, so they are dropped. Apostrophes are
    kept (names like "Farfetch'd" and "Team Rocket's Mewtwo" need them), with
    curly ones normalised to the straight apostrophe the API uses.
    """
    if not text:
        return ""
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace('"', "").replace("“", "").replace("”", "")
    return " ".join(text.split())


def parse_card_number(raw: str | None) -> tuple[str | None, int | None]:
    """Split a printed collector number into (API number, printed set total).

    pokemontcg.io stores plain numbers without leading zeros ("025" -> "25")
    but keeps letter prefixes as printed ("TG05"). Returns (None, None) when
    the text doesn't look like a collector number.
    """
    match = _CARD_NUMBER_RE.match((raw or "").strip())
    if not match:
        return None, None
    prefix, digits, suffix, total = match.groups()
    if prefix:
        number = f"{prefix.upper()}{digits}{suffix}"
    else:
        number = f"{digits.lstrip('0') or '0'}{suffix}"
    return number, (int(total) if total else None)


def build_queries(card_name: str, set_name: str | None = None,
                  number: str | None = None) -> list[str]:
    """Search queries to try in order, most specific first.

    Name + collector number is the most reliable (the number is printed on
    the card), then name + set, then the name on its own.
    """
    name = clean_term(card_name)
    if not name:
        return []
    base = f'name:"{name}"'
    queries = []
    if number:
        queries.append(f"{base} number:{number}")
    clean_set = clean_term(set_name)
    if clean_set:
        queries.append(f'{base} set.name:"{clean_set}"')
    queries.append(base)
    return queries


def select_prices(tcg_prices: dict[str, Any] | None, is_holo: bool) -> tuple[str | None, dict[str, Any]]:
    """Pick the TCGPlayer price bucket that best fits the detected card.

    Buckets are tried in holo / non-holo preference order, then the other
    list, then anything else the API returned. The first bucket with a market
    price wins; if none has one, the first bucket present is used so the
    low/high range can still be shown.
    """
    tcg_prices = tcg_prices or {}
    pref, fallback = (HOLO_PRICE_PREF, NORMAL_PRICE_PREF) if is_holo else (NORMAL_PRICE_PREF, HOLO_PRICE_PREF)
    others = tuple(k for k in tcg_prices if k not in pref and k not in fallback)
    present = [b for b in (*pref, *fallback, *others) if isinstance(tcg_prices.get(b), dict)]
    if not present:
        return None, {}
    chosen = next((b for b in present if tcg_prices[b].get("market")), present[0])
    p = tcg_prices[chosen]
    return chosen, {
        "market": p.get("market"),
        "low": p.get("low"),
        "mid": p.get("mid"),
        "high": p.get("high"),
        "type": chosen,
    }


def parse_card(card: dict[str, Any], is_holo: bool) -> dict[str, Any]:
    """Reduce a pokemontcg.io card object to the fields the overlay uses."""
    card_set = card.get("set") or {}
    price_type, prices = select_prices((card.get("tcgplayer") or {}).get("prices"), is_holo)
    return {
        "id": card.get("id"),
        "name": card.get("name"),
        "set": card_set.get("name"),
        "printed_total": card_set.get("printedTotal"),
        "number": card.get("number"),
        "rarity": card.get("rarity"),
        "prices": prices,
        "price_type": price_type,
    }


def pick_best_match(cards: list[dict[str, Any]], card_name: str = "",
                    printed_total: int | None = None) -> dict[str, Any] | None:
    """Choose the parsed card whose price to show.

    Preference: has a market price, then the printed set total matches
    (e.g. the "102" in "4/102"), then the name matches exactly. Ties keep the
    API order, which is newest set first.
    """
    if not cards:
        return None
    wanted = clean_term(card_name).lower()

    def rank(card: dict[str, Any]) -> tuple[bool, bool, bool]:
        return (
            not card["prices"].get("market"),
            printed_total is None or card.get("printed_total") != printed_total,
            (card.get("name") or "").lower() != wanted,
        )

    return sorted(cards, key=rank)[0]


def rate_deal(stream_price: float | None, market_price: float | None) -> str:
    """Rate a stream price against the market price (see DEAL_THRESHOLDS)."""
    if stream_price is None or not market_price:
        return DEAL_NONE
    ratio = stream_price / market_price
    for threshold, key in DEAL_THRESHOLDS:
        if ratio <= threshold:
            return key
    return DEAL_OVER


def market_price(price_data: dict[str, Any] | None) -> float | None:
    """TCGPlayer market price of the best match, if there is one."""
    best = (price_data or {}).get("best_match") or {}
    return (best.get("prices") or {}).get("market")


def deal_for(card: dict[str, Any], price_data: dict[str, Any] | None) -> tuple[str, float | None]:
    """Return (deal key, market price) for a detected card and its lookup.

    Foreign cards are never rated against English prices.
    """
    if (price_data or {}).get("is_foreign"):
        return DEAL_FOREIGN, None
    market = market_price(price_data)
    return rate_deal(card.get("streamPrice"), market), market


def describe_error(exc: Exception) -> str:
    """Short, overlay-friendly description of a failed price request."""
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "network error"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 429:
            return "rate limited"
        return f"HTTP {exc.response.status_code}"
    return "bad response"


# ====================================================================== I/O

class PriceCache:
    """Looks up TCGPlayer prices and remembers successful results for CACHE_TTL."""

    def __init__(self, api_key: str | None = None, session: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if api_key is None:
            api_key = os.environ.get(API_KEY_ENV_VAR, "")
        api_key = api_key.strip()
        self._headers = {API_KEY_HEADER: api_key} if api_key else {}
        self._session = session or requests.Session()
        self._clock = clock
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self._lock = Lock()

    def lookup(self, card_name: str, set_name: str | None = None, is_holo: bool = False,
               language: str | None = "English", card_number: str | None = None) -> dict[str, Any]:
        """Return price info for a card. Never raises: failures set ``error``.

        Foreign cards are returned immediately without a request, because
        TCGPlayer (via pokemontcg.io) only prices English cards.
        """
        result: dict[str, Any] = {
            "card_name": card_name,
            "language": language or "English",
            "is_foreign": is_foreign(language),
            "cards": [],
            "best_match": None,
            "error": None,
        }
        if result["is_foreign"]:
            return result
        if not clean_term(card_name):
            result["error"] = "empty card name"
            return result

        number, printed_total = parse_card_number(card_number)
        key = (clean_term(card_name).lower(), clean_term(set_name).lower(),
               number, printed_total, bool(is_holo))
        with self._lock:
            entry = self._cache.get(key)
            if entry and self._clock() - entry[0] < CACHE_TTL:
                return entry[1]

        try:
            cards: list[dict[str, Any]] = []
            for query in build_queries(card_name, set_name, number):
                cards = self._search(query)
                if cards:
                    break
        except (requests.RequestException, ValueError) as exc:
            log.warning("Price lookup for %r failed: %s", card_name, exc)
            result["error"] = describe_error(exc)
            return result  # not cached, so the next sighting retries

        parsed = [parse_card(card, is_holo) for card in cards]
        result["cards"] = parsed
        result["best_match"] = pick_best_match(parsed, card_name, printed_total)
        best = result["best_match"]
        if best:
            log.info("Price for %r: %s #%s (%s) market=%s", card_name, best["set"],
                     best["number"], best["price_type"], best["prices"].get("market"))
        else:
            log.info("No pokemontcg.io match for %r", card_name)

        with self._lock:
            self._cache[key] = (self._clock(), result)
        return result

    def _search(self, query: str) -> list[dict[str, Any]]:
        resp = self._session.get(
            POKEMONTCG_API,
            params={"q": query, "orderBy": ORDER_BY, "pageSize": PAGE_SIZE},
            headers=self._headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, list) else []
