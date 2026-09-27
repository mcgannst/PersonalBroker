from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from trader.adapters.finviz.parser import (
    blocked_reason,
    parse_news,
    parse_news_page,
    parse_screener,
    parse_universe_row,
    to_finviz_ticker,
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


# --- gauntlet fix round (attempt 2): layout changes must be detectable, never silently skipped ---

PAD = "<!--" + "x" * 2000 + "-->"


def news_page(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td>{when}</td><td><a class="tab-link-news" href="/news/{i}">{title}</a></td></tr>'
        for i, (when, title) in enumerate(rows)
    )
    return f'<html><body>{PAD}<table id="news-table">{body}</table></body></html>'


def test_screener_counts_rows_whose_cell_count_differs_from_header() -> None:
    html = (
        '<div class="count-text">#1 / 2 Total</div><table class="screener_table">'
        "<tr><th>No.</th><th>Ticker</th></tr>"
        '<tr><td>1</td><td data-boxover-ticker="A">A</td></tr>'
        '<tr><td>2</td><td data-boxover-ticker="B">B</td><td>extra</td></tr></table>'
    )
    page = parse_screener(html)
    assert [r["Ticker"] for r in page.rows] == ["A"]
    assert page.bad_rows == 1


def test_screener_fixture_has_no_bad_rows() -> None:
    assert parse_screener((FIX / "raw_screener_p1.html").read_text()).bad_rows == 0


def test_decimal_rejects_nan_and_infinity() -> None:
    for bad in ("NaN", "nan", "Infinity", "-Infinity", "inf", "sNaN"):
        row = parse_universe_row(
            {"Ticker": "X", "Company": "", "Sector": "", "Industry": "", "Price": bad, "Volume": bad}
        )
        assert row.price is None, bad
        assert row.volume is None, bad


def test_to_finviz_ticker_maps_questrade_share_classes_back() -> None:
    assert to_finviz_ticker("BF.B") == "BF-B"
    assert to_finviz_ticker("BRK.B") == "BRK-B"
    assert to_finviz_ticker(" AAPL ") == "AAPL"
    assert to_finviz_ticker(to_questrade_ticker("BRK-B")) == "BRK-B"


def test_news_page_fixture_has_no_problem() -> None:
    page = parse_news_page((FIX / "raw_quote_AAPL.html").read_text(), today_et=date(2026, 9, 26))
    assert page.problem is None
    assert len(page.headlines) >= 10


def test_news_page_without_table_is_a_problem_when_the_page_is_real() -> None:
    page = parse_news_page(f"<html><body>{PAD}no table here</body></html>", today_et=date(2026, 9, 26))
    assert page.headlines == []
    assert page.problem is not None and "news-table" in page.problem


def test_news_page_whose_headlines_all_fail_to_parse_is_a_problem() -> None:
    html = news_page([("yesterday-ish", "a"), ("25 Sep 2026 16:18", "b")])
    page = parse_news_page(html, today_et=date(2026, 9, 26))
    assert page.headlines == []
    assert page.problem is not None and "2 headline rows" in page.problem


def test_news_page_with_no_headline_rows_is_empty_not_a_problem() -> None:
    html = f'<html><body>{PAD}<table id="news-table"></table></body></html>'
    page = parse_news_page(html, today_et=date(2026, 9, 26))
    assert page.headlines == [] and page.problem is None


def test_news_page_with_some_unparseable_rows_keeps_the_good_ones() -> None:
    html = news_page([("Sep-25-26 04:18PM", "good"), ("garbage", "bad")])
    page = parse_news_page(html, today_et=date(2026, 9, 26))
    assert [h.title for h in page.headlines] == ["good"]
    assert page.problem is None
    assert page.headlines[0].url == "https://finviz.com/news/0"


# --- P2-T14 fix round: FinViz's "matched nothing" page ---


def test_live_zero_match_page_is_a_verified_empty_result() -> None:
    """Saved LIVE on 2026-09-27 (v=111, f=cap_mega,sh_price_u1): '0 Total' and no results table."""
    html = (FIX / "raw_screener_zero.html").read_text(encoding="utf-8")
    assert blocked_reason(200, html) is None
    page = parse_screener(html)
    assert page.total == 0 and page.rows == [] and page.header == []
    assert page.has_table is False and page.verified_empty


def _count_page(text: str, table: bool = False) -> str:
    t = '<table class="screener_table"><tr><th>No.</th><th>Ticker</th></tr></table>' if table else ""
    return f'<html><body><div class="count-text">{text}</div>{t}</body></html>'


def test_count_text_must_match_whole_forms() -> None:
    assert parse_screener(_count_page("0 Total")).total == 0
    assert parse_screener(_count_page("#1 / 0 Total")).total == 0
    assert parse_screener(_count_page("#21 / 1,234 Total")).total == 1234
    assert parse_screener(_count_page("  #1 /\n 7   Total ")).total == 7
    for bad in ("Filters: 5 Total", "0 Totally", "Total", "#1 / Total", "about 5 Total results", ""):
        assert parse_screener(_count_page(bad)).total is None, bad


def test_screener_total_id_wins_over_other_count_text() -> None:
    html = '<div class="count-text"><b>Refresh:</b></div><div id="screener-total">#1 / 12 Total</div>'
    assert parse_screener(html).total == 12


def test_verified_empty_needs_a_zero_count_no_rows_and_a_ticker_header_if_a_table() -> None:
    assert parse_screener(_count_page("0 Total", table=True)).verified_empty
    assert not parse_screener(_count_page("#1 / 5 Total")).verified_empty
    assert not parse_screener("<html><body>No results</body></html>").verified_empty
    no_ticker = (
        '<div class="count-text">0 Total</div><table class="screener_table"><tr><th>Symbol</th></tr></table>'
    )
    assert not parse_screener(no_ticker).verified_empty
