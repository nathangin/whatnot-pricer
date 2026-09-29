import pytest
import requests

import prices
from fakes import FakeResponse, FakeSession, api_card, search_result
from prices import (
    DEAL_FOREIGN,
    PriceCache,
    build_queries,
    clean_term,
    deal_for,
    parse_card,
    parse_card_number,
    pick_best_match,
    rate_deal,
    select_prices,
)

HOLO = {"holofoil": {"low": 250.0, "mid": 320.0, "high": 900.0, "market": 350.0}}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------- deal rating

@pytest.mark.parametrize("stream, market, expected", [
    (40.0, 100.0, "great"),
    (80.0, 100.0, "great"),      # ratio 0.80 is still a great deal
    (80.01, 100.0, "good"),
    (95.0, 100.0, "good"),       # 0.95
    (95.01, 100.0, "fair"),
    (105.0, 100.0, "fair"),      # 1.05
    (105.01, 100.0, "over"),
    (250.0, 100.0, "over"),
    (0.0, 100.0, "great"),       # a $0 opening bid is below market
])
def test_rate_deal_thresholds(stream, market, expected):
    assert rate_deal(stream, market) == expected


@pytest.mark.parametrize("stream, market", [(None, 100.0), (50.0, None), (50.0, 0.0), (None, None)])
def test_rate_deal_without_both_prices(stream, market):
    assert rate_deal(stream, market) == "none"


def test_deal_keys_cover_every_rating():
    assert set(prices.DEAL_KEYS) == {"great", "good", "fair", "over", "none", "foreign"}


def test_deal_for_uses_best_match_market_price():
    price_data = {"is_foreign": False, "best_match": {"prices": {"market": 100.0}}}
    assert deal_for({"streamPrice": 70.0}, price_data) == ("great", 100.0)
    assert deal_for({"streamPrice": 70.0}, {"is_foreign": False, "best_match": None}) == ("none", None)


def test_deal_for_never_rates_foreign_cards_against_english_prices():
    price_data = {"is_foreign": True, "best_match": {"prices": {"market": 100.0}}}
    assert deal_for({"streamPrice": 10.0}, price_data) == (DEAL_FOREIGN, None)


# ------------------------------------------------------------ query building

@pytest.mark.parametrize("raw, expected", [
    ("4/102", ("4", 102)),
    ("025/165", ("25", 165)),
    ("TG05/TG30", ("TG05", 30)),
    ("GG01/GG70", ("GG01", 70)),
    ("#58", ("58", None)),
    ("SWSH050", ("SWSH050", None)),
    (" 4 / 102 ", ("4", 102)),
    ("", (None, None)),
    (None, (None, None)),
    ("unreadable", (None, None)),
])
def test_parse_card_number(raw, expected):
    assert parse_card_number(raw) == expected


def test_clean_term_keeps_apostrophes_and_drops_double_quotes():
    assert clean_term("Farfetch’d") == "Farfetch'd"
    assert clean_term('Team Rocket\'s "Mewtwo"  ex') == "Team Rocket's Mewtwo ex"


def test_build_queries_most_specific_first():
    assert build_queries("Charizard", "Base", "4") == [
        'name:"Charizard" number:4',
        'name:"Charizard" set.name:"Base"',
        'name:"Charizard"',
    ]
    assert build_queries("Pikachu") == ['name:"Pikachu"']
    assert build_queries('  "  ') == []


# ----------------------------------------------------------- price selection

def test_select_prices_prefers_holofoil_for_holo_cards():
    tcg = {"normal": {"market": 1.0}, "holofoil": {"market": 5.0, "low": 4.0}}
    assert select_prices(tcg, is_holo=True)[0] == "holofoil"
    assert select_prices(tcg, is_holo=False)[0] == "normal"


def test_select_prices_prefers_unlimited_over_first_edition():
    tcg = {"1stEditionHolofoil": {"market": 900.0}, "unlimitedHolofoil": {"market": 120.0}}
    price_type, chosen = select_prices(tcg, is_holo=True)
    assert price_type == "unlimitedHolofoil"
    assert chosen["market"] == 120.0


def test_select_prices_falls_back_to_other_finish_and_unknown_buckets():
    assert select_prices({"reverseHolofoil": {"market": 2.0}}, is_holo=False)[0] == "reverseHolofoil"
    assert select_prices({"someNewBucket": {"market": 3.0}}, is_holo=True)[0] == "someNewBucket"


def test_select_prices_skips_buckets_without_market_price():
    tcg = {"holofoil": {"low": 10.0, "market": None}, "reverseHolofoil": {"market": 12.0}}
    assert select_prices(tcg, is_holo=True)[0] == "reverseHolofoil"
    # nothing has a market price: keep the first bucket so the range still shows
    price_type, chosen = select_prices({"holofoil": {"low": 10.0, "high": 20.0}}, is_holo=True)
    assert price_type == "holofoil" and chosen["market"] is None and chosen["high"] == 20.0


def test_select_prices_with_no_tcgplayer_data():
    assert select_prices(None, is_holo=True) == (None, {})


def test_parse_card_extracts_display_fields():
    parsed = parse_card(api_card("base1-4", "Charizard", "Base", "4", 102, HOLO), is_holo=True)
    assert parsed == {
        "id": "base1-4", "name": "Charizard", "set": "Base", "printed_total": 102,
        "number": "4", "rarity": "Rare Holo", "price_type": "holofoil",
        "prices": {"market": 350.0, "low": 250.0, "mid": 320.0, "high": 900.0, "type": "holofoil"},
    }
    assert parse_card(api_card("x-1", "Card", "Set", "1", 10, None), is_holo=False)["prices"] == {}


def test_pick_best_match_ranking():
    newest_no_price = parse_card(api_card("new-4", "Charizard", "New", "4", 200, None), True)
    wrong_total = parse_card(api_card("base4-4", "Charizard", "Base Set 2", "4", 130, HOLO), True)
    right_total = parse_card(api_card("base1-4", "Charizard", "Base", "4", 102, HOLO), True)
    ex_card = parse_card(api_card("sv3-125", "Charizard ex", "Obsidian Flames", "125", 197, HOLO), True)

    cards = [newest_no_price, wrong_total, right_total]
    assert pick_best_match(cards, "Charizard", printed_total=102) is right_total
    # without a printed total, API order (newest first) among priced cards wins
    assert pick_best_match(cards, "Charizard") is wrong_total
    # an exact name beats a longer name when nothing else separates them
    assert pick_best_match([ex_card, right_total], "Charizard") is right_total
    assert pick_best_match([], "Charizard") is None


# ------------------------------------------------------------ HTTP + caching

def test_lookup_sends_documented_request_without_key():
    session = FakeSession(search_result(api_card("base1-4", "Charizard", "Base", "4", 102, HOLO)))
    result = PriceCache(session=session).lookup("Charizard", "Base", is_holo=True, card_number="4/102")

    call = session.calls[0]
    assert call["url"] == "https://api.pokemontcg.io/v2/cards"
    assert call["params"] == {"q": 'name:"Charizard" number:4', "orderBy": "-set.releaseDate", "pageSize": 6}
    assert call["headers"] == {}
    assert call["timeout"] == prices.REQUEST_TIMEOUT
    assert result["error"] is None
    assert result["best_match"]["id"] == "base1-4"
    assert result["best_match"]["prices"]["market"] == 350.0


def test_lookup_sends_optional_api_key_header(monkeypatch):
    monkeypatch.setenv("POKEMONTCG_API_KEY", " test-key ")
    session = FakeSession(search_result())
    PriceCache(session=session).lookup("Pikachu")
    assert session.calls[0]["headers"] == {"X-Api-Key": "test-key"}


def test_lookup_falls_back_to_broader_queries():
    session = FakeSession(search_result(), search_result(),
                          search_result(api_card("sv1-25", "Pikachu", "Scarlet & Violet", "25", 198, HOLO)))
    result = PriceCache(session=session).lookup("Pikachu", "Wrong Set", card_number="99/100")
    assert session.queries == [
        'name:"Pikachu" number:99',
        'name:"Pikachu" set.name:"Wrong Set"',
        'name:"Pikachu"',
    ]
    assert result["best_match"]["id"] == "sv1-25"


def test_lookup_caches_results_for_ten_minutes():
    clock = FakeClock()
    card = api_card("base1-4", "Charizard", "Base", "4", 102, HOLO)
    session = FakeSession(search_result(card), search_result(card))
    cache = PriceCache(session=session, clock=clock)

    first = cache.lookup("Charizard", "Base", True)
    clock.now += prices.CACHE_TTL - 1
    assert cache.lookup("CHARIZARD", "base", True) is first   # same card, different case
    assert len(session.calls) == 1

    clock.now += 2  # past the TTL
    cache.lookup("Charizard", "Base", True)
    assert len(session.calls) == 2


def test_cache_key_includes_holo_and_number():
    session = FakeSession(*(search_result() for _ in range(6)))
    cache = PriceCache(session=session)
    cache.lookup("Pikachu", is_holo=False)
    cache.lookup("Pikachu", is_holo=True)
    cache.lookup("Pikachu", is_holo=True, card_number="25/102")   # number, then name
    cache.lookup("Pikachu", is_holo=True, card_number="25/130")   # different set size
    cache.lookup("Pikachu", is_holo=True, card_number="025/130")  # same card as above
    assert len(session.calls) == 1 + 1 + 2 + 2


def test_foreign_cards_skip_the_request():
    session = FakeSession()
    result = PriceCache(session=session).lookup("Pikachu", language="Japanese")
    assert result["is_foreign"] is True
    assert result["best_match"] is None and result["error"] is None
    assert session.calls == []


def test_english_result_is_not_mixed_up_with_a_foreign_one():
    session = FakeSession(search_result(api_card("sv1-25", "Pikachu", "SV", "25", 198, HOLO)))
    cache = PriceCache(session=session)
    cache.lookup("Pikachu", language="Japanese")
    english = cache.lookup("Pikachu", language="English")
    assert english["is_foreign"] is False and english["best_match"]["id"] == "sv1-25"


@pytest.mark.parametrize("failure, message", [
    (requests.Timeout("read timed out"), "timed out"),
    (requests.ConnectionError("dns failure"), "network error"),
    (FakeResponse(status=429), "rate limited"),
    (FakeResponse(status=503), "HTTP 503"),
    (FakeResponse(payload=ValueError("not json")), "bad response"),
])
def test_network_errors_are_reported_not_raised(failure, message):
    session = FakeSession(failure)
    result = PriceCache(session=session).lookup("Charizard")
    assert result["error"] == message
    assert result["best_match"] is None


def test_failed_lookups_are_not_cached():
    card = api_card("base1-4", "Charizard", "Base", "4", 102, HOLO)
    session = FakeSession(requests.Timeout("slow"), search_result(card))
    cache = PriceCache(session=session)
    assert cache.lookup("Charizard")["error"] == "timed out"
    assert cache.lookup("Charizard")["best_match"]["id"] == "base1-4"
    assert len(session.calls) == 2


def test_unexpected_payload_shape_means_no_results():
    session = FakeSession(FakeResponse(payload=["not", "a", "dict"]))
    result = PriceCache(session=session).lookup("Charizard")
    assert result["error"] is None and result["cards"] == []


def test_empty_name_does_not_hit_the_api():
    session = FakeSession()
    assert PriceCache(session=session).lookup('  " ')["error"] == "empty card name"
    assert session.calls == []
