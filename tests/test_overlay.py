"""UI checks for the overlay. They need Tk and a display (e.g. `xvfb-run`) and are
skipped otherwise; all pricing logic is covered by the display-free tests."""

from types import SimpleNamespace

import pytest

import prices
from fakes import CHARIZARD
from monitor import normalize_card

tk = pytest.importorskip("tkinter")

PRICED = {"is_foreign": False, "error": None, "best_match": {
    "set": "Base", "number": "4", "price_type": "holofoil",
    "prices": {"market": 400.0, "low": 250.0, "high": 900.0},
}}


@pytest.fixture
def overlay():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    from overlay import OverlayWindow

    window = OverlayWindow(root)
    yield window
    root.destroy()


def log_text(window) -> str:
    return window._log_text.get("1.0", "end")


def test_every_deal_key_has_a_style():
    from overlay import DEAL_STYLES

    assert set(DEAL_STYLES) == set(prices.DEAL_KEYS)


def test_priced_card_shows_market_range_match_and_deal(overlay):
    overlay.update_card(normalize_card(CHARIZARD), PRICED)   # $300 vs $400 market = 0.75
    assert overlay._stream_var.get() == "$300.00"
    assert overlay._market_var.get() == "$400.00"
    assert overlay._range_var.get() == "$250.00 – $900.00"
    assert overlay._match_var.get() == "Base #4 · holofoil"
    assert overlay._deal_var.get() == "\U0001f525 Great Deal"
    assert "$400.00 mkt  \U0001f525 Great Deal" in log_text(overlay)


def test_foreign_card_is_not_rated_in_panel_or_log(overlay):
    card = normalize_card({**CHARIZARD, "language": "Japanese"})
    overlay.update_card(card, {"is_foreign": True, "best_match": None, "error": None})
    assert overlay._market_var.get() == "⚠️ Foreign card"
    assert overlay._range_var.get() == "TCGPlayer = English only"
    assert overlay._deal_var.get() == "\U0001f30f Check foreign market"
    assert "Check foreign market" in log_text(overlay)
    assert "Great Deal" not in log_text(overlay)


def test_failed_price_lookup_is_explained(overlay):
    overlay.update_card(normalize_card(CHARIZARD),
                        {"is_foreign": False, "best_match": None, "error": "timed out"})
    assert overlay._range_var.get() == "Price lookup failed"
    assert overlay._deal_var.get() == ""
    assert overlay._status_var.get() == "⚠️ Price lookup: timed out"


def test_log_keeps_the_last_twenty_cards(overlay):
    for i in range(25):
        overlay.update_card(normalize_card({**CHARIZARD, "cardName": f"Card {i}"}), PRICED)
    text = log_text(overlay)
    assert text.startswith("\U0001f1fa\U0001f1f8 Card 24")   # newest first
    assert "Card 5\n" in text and "Card 4\n" not in text


def test_dragging_ignores_buttons_and_the_log(overlay):
    overlay.root.update()
    start = overlay.win.geometry()
    for widget in (overlay._pause_btn, overlay._log_text):
        overlay._drag_start(SimpleNamespace(widget=widget, x_root=50, y_root=50))
        overlay._drag_move(SimpleNamespace(widget=widget, x_root=150, y_root=150))
    overlay.root.update()
    assert overlay.win.geometry() == start

    overlay._drag_start(SimpleNamespace(widget=overlay._deal_lbl, x_root=50, y_root=50))
    overlay._drag_move(SimpleNamespace(widget=overlay._deal_lbl, x_root=150, y_root=150))
    overlay.root.update()
    assert overlay.win.geometry() != start


def test_region_border_is_four_strips_outside_the_region(overlay):
    region = {"left": 100, "top": 100, "width": 400, "height": 300}
    overlay.show_region_border(region)
    overlay.root.update()
    assert len(overlay._border_wins) == 4
    for strip in overlay._border_wins:
        x, y = strip.winfo_rootx(), strip.winfo_rooty()
        w, h = strip.winfo_width(), strip.winfo_height()
        overlaps = (x < 500 and x + w > 100) and (y < 400 and y + h > 100)
        assert not overlaps, (x, y, w, h)
