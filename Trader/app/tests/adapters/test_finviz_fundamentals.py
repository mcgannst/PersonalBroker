from datetime import date
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
import respx

from trader.adapters.finviz.fundamentals import (
    LABELS,
    FinvizFundamentals,
    parse_snapshot,
    snapshot_problem,
    to_fundamentals,
)
from trader.adapters.finviz.scraper import FinvizBlocked, FinvizError, FinvizParseError, FinvizScraper

FIXTURES = Path(__file__).parents[1] / "fixtures/finviz"
TODAY = date(2026, 10, 6)
QUOTE_URL = "https://finviz.com/quote.ashx"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _scraper(tmp: Path | None = None) -> FinvizScraper:
    return FinvizScraper(cache_dir=tmp, sleep=lambda _s: None, monotonic=lambda: 0.0, wall=lambda: 1e6)


def test_parse_snapshot_fixture() -> None:
    raw = parse_snapshot(_page("snapshot_F.html"))
    assert raw == {
        "Index": "S&P 500",
        "Market Cap": "47.12B",
        "Book/sh": "11.32",
        "Dividend Ex-Date": "Aug 11, 2026",
        "Payout": "54.20%",
        "Debt/Eq": "3.56",
        "EPS next Y": "1.45",  # the label is on the page twice: the first value wins
        "EPS (ttm)": "1.10",
        "EPS Y/Y TTM": "-12.40%",
        "Earnings": "Oct 22 AMC",
        "Short Float": "3.10%",
        "RSI (14)": "48.21",
        "Trades": "",
    }
    assert snapshot_problem(raw) is None
    assert to_fundamentals(raw, TODAY) == FinvizFundamentals(
        eps_growth_yoy=D("-0.1240"),
        debt_to_equity=D("3.56"),
        book_value_per_share=D("11.32"),
        payout_ratio=D("0.5420"),
        short_float=D("0.0310"),
        next_earnings_date=date(2026, 10, 22),
        rsi14=D("48.21"),
        market_cap_usd=D("47120000000"),
    )


def test_live_quote_page_has_every_label() -> None:
    raw = parse_snapshot(_page("raw_quote_AAPL.html"))
    assert set(LABELS.values()) <= raw.keys()
    got = to_fundamentals(raw, date(2026, 9, 28))
    assert (got.eps_growth_yoy, got.debt_to_equity, got.rsi14) == (D("0.3257"), D("0.78"), D("65.67"))
    assert got.market_cap_usd == D("4977640000000")
    assert got.next_earnings_date is None  # the page still shows the last report, "Jul 30 AMC"


@pytest.mark.parametrize(
    ("label", "text", "field", "want"),
    [
        ("EPS Y/Y TTM", "32.57%", "eps_growth_yoy", D("0.3257")),
        ("EPS Y/Y TTM", "-2.28%", "eps_growth_yoy", D("-0.0228")),
        ("EPS Y/Y TTM", "-", "eps_growth_yoy", None),
        ("Payout", "0.00%", "payout_ratio", D("0")),
        ("Short Float", "12.5", "short_float", None),  # no percent sign: not a percentage
        ("Debt/Eq", "-", "debt_to_equity", None),
        ("Book/sh", "-1.75", "book_value_per_share", D("-1.75")),
        ("RSI (14)", "n/a", "rsi14", None),
        ("Market Cap", "4977.64B", "market_cap_usd", D("4977640000000")),
        ("Market Cap", "850.5M", "market_cap_usd", D("850500000")),
        ("Market Cap", "-", "market_cap_usd", None),
        ("Earnings", "Oct 22 AMC", "next_earnings_date", date(2026, 10, 22)),
        ("Earnings", "Oct 06 BMO", "next_earnings_date", TODAY),
        ("Earnings", "Jan 28", "next_earnings_date", date(2027, 1, 28)),  # the year nearest today
        ("Earnings", "Jul 30 AMC", "next_earnings_date", None),  # already reported
        ("Earnings", "-", "next_earnings_date", None),
    ],
)
def test_to_fundamentals(label: str, text: str, field: str, want: object) -> None:
    got = to_fundamentals({label: text}, TODAY)
    assert getattr(got, field) == want
    assert [f for f in LABELS if f != field and getattr(got, f) is not None] == []  # a missing label is None


@respx.mock
def test_snapshot_reads_the_quote_page_and_shares_the_news_cache_entry(tmp_path: Path) -> None:
    route = respx.get(QUOTE_URL, params={"t": "BF-B"}).mock(
        return_value=httpx.Response(200, text=_page("raw_quote_AAPL.html"))
    )
    scraper = _scraper(tmp_path)
    raw = scraper.snapshot("BF.B", TODAY)
    assert raw["Debt/Eq"] == "0.78"
    assert scraper.snapshot("BF.B", TODAY) == raw and scraper.news("BF.B", TODAY)
    assert route.call_count == 1  # the second read and the headlines came from the cache


@respx.mock
def test_blocked_or_changed_page_raises_finviz_error(tmp_path: Path) -> None:
    pad = "<!--" + "x" * 2000 + "-->"
    renamed = "<table class='snapshot-table2'><tr><td>Renamed</td><td>1</td></tr></table>"
    pages = [
        (httpx.Response(403, text="denied"), FinvizBlocked),
        (httpx.Response(200, text=f"<html>{pad}<p>no snapshot here</p></html>"), FinvizParseError),
        (httpx.Response(200, text=f"<html>{pad}{renamed}</html>"), FinvizParseError),
    ]
    scraper = _scraper(tmp_path)
    for response, error in pages:
        respx.get(QUOTE_URL).mock(return_value=response)
        with pytest.raises(error) as caught:
            scraper.snapshot("F", TODAY)
        assert isinstance(caught.value, FinvizError)
    assert list(tmp_path.glob("*.html")) == []  # nothing that raised was cached
