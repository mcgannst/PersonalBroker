"""OPTSIM T11: the market-wide candidate screen's FinViz side (no network: the scraper is replaced)."""

from typing import Any

import pytest

from trader.option_strategies.wheel import screener
from trader.option_strategies.wheel.config import WheelParams

CFG = WheelParams()


def test_market_candidates_are_new_unique_and_in_screen_order() -> None:
    asked: list[str] = []

    def screen(filters: str) -> list[str]:
        asked.append(filters)
        return ["KO", "f", "PFE", "KO", " t ", ""]

    assert screener.market_candidates(screen, CFG, {"F"}) == ["KO", "PFE", "T"]
    assert asked == [",".join(CFG.market_screen_filters)]


@pytest.mark.parametrize("change", [{"market_screen_max_candidates": 0}, {"market_screen_filters": ()}])
def test_nothing_is_asked_without_a_cap_or_filters(change: dict[str, Any]) -> None:
    def screen(filters: str) -> list[str]:
        raise AssertionError("FinViz must not be asked")

    assert screener.market_candidates(screen, CFG.model_copy(update=change), set()) == []


def test_the_default_source_reads_the_finviz_screener(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Page:
        rows = [{"Ticker": "KO"}, {"Ticker": "BRK-B"}]

    class Scraper:
        def __init__(self, **options: Any) -> None:
            seen["options"] = options

        def __enter__(self) -> "Scraper":
            return self

        def __exit__(self, *exc: object) -> None:
            seen["closed"] = True

        def screen(self, filters: str) -> Page:
            seen["filters"] = filters
            return Page()

    monkeypatch.setattr(screener, "FinvizScraper", Scraper)
    assert screener.source is screener.finviz_tickers
    assert screener.finviz_tickers("cap_largeover") == ["KO", "BRK.B"]  # Questrade's spelling
    assert seen == {"options": {"cache_screens": False}, "filters": "cap_largeover", "closed": True}
