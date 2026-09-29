"""Pytest configuration shared by all tests (helpers live in fakes.py)."""

import pytest


@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own keys and model choice out of the tests."""
    for var in ("ANTHROPIC_API_KEY", "POKEMONTCG_API_KEY", "WHATNOT_MODEL"):
        monkeypatch.delenv(var, raising=False)
