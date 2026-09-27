from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trader.adapters.finviz.parser import (
    blocked_reason,
    parse_news,
    parse_screener,
    parse_universe_row,
    to_questrade_ticker,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "finviz"


def test_screener_fixture_total_header_and_first_row() -> None:
    page = parse_screener((FIX / "raw_screener_p1.html").read_text())
    assert page.total == 695
    assert page.header == [
        "No.",
        "Ticker",
        "Company",
        "Sector",
        "Industry",
        "Country",
        "Market Cap",
        "P/E",
        "Price",
        "Change %",
        "Volume",
    ]
    assert len(page.rows) == 20
    assert page.rows[0] == {
        "No.": "1",
        "Ticker": "AA",
        "Company": "Alcoa Corp",
        "Sector": "Basic Materials",
        "Industry": "Aluminum",
        "Country": "USA",
        "Market Cap": "11.31B",
        "P/E": "8.79",
        "Price": "42.85",
        "Change %": "0.40%",
        "Volume": "3,401,413",
    }


def test_universe_row_typed() -> None:
    page = parse_screener((FIX / "raw_screener_p1.html").read_text())
    row = parse_universe_row(page.rows[0])
    assert row.ticker == "AA"
    assert row.price == Decimal("42.85")
    assert row.volume == 3401413


def test_universe_row_missing_values() -> None:
    row = parse_universe_row(
        {
            "Ticker": "BF-B",
            "Company": "Brown-Forman",
            "Sector": "",
            "Industry": "",
            "Price": "-",
            "Volume": "",
        }
    )
    assert row.ticker == "BF.B"
    assert row.price is None and row.volume is None


def test_news_dates_carry_forward_and_convert_to_utc() -> None:
    items = parse_news((FIX / "raw_quote_AAPL.html").read_text(), today_et=date(2026, 9, 26))
    assert len(items) >= 10
    first, second, third = items[:3]
    assert first.title == "China, U.S. agree to $30 billion tariff cut, launch AI dialogue"
    assert first.source == "Investing.com"
    assert first.ts == datetime(2026, 9, 26, 10, 7, tzinfo=UTC)  # 06:07 ET "Today"
    assert second.ts == datetime(2026, 9, 25, 20, 18, tzinfo=UTC)  # "Sep-25-26 04:18PM"
    assert third.ts == datetime(2026, 9, 25, 20, 2, tzinfo=UTC)  # "04:02PM", date carried forward
    assert third.url.startswith("https://")


def test_news_without_table_is_empty() -> None:
    assert parse_news("<html><body>nothing</body></html>", today_et=date(2026, 9, 26)) == []


def test_empty_body_is_blocked() -> None:
    assert blocked_reason(200, "") is not None
    assert blocked_reason(403, "x" * 5000) == "HTTP 403"
    assert blocked_reason(200, "<title>Just a moment...</title>" + "x" * 5000) is not None
    assert blocked_reason(200, (FIX / "raw_screener_p1.html").read_text()) is None


def test_ticker_mapping() -> None:
    assert to_questrade_ticker("BF-B") == "BF.B"
    assert to_questrade_ticker("AAPL") == "AAPL"
