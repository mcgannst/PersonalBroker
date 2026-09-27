"""P1-T5 gauntlet: try to make the FinViz parser/scraper return a silently wrong result.

Review Focus item 3: FinViz returns something unexpected (block page, empty body, layout change,
ignored filter). Expected: an error, never a silently wrong universe. All HTTP is mocked (respx).
"""

import os
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from trader.adapters.finviz.parser import parse_news
from trader.adapters.finviz.scraper import FinvizError, FinvizFilterIgnored, FinvizScraper

URL = "https://finviz.com/screener.ashx"
HEADER = [
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
PAD = "<!--" + "x" * 2000 + "-->"


def screener_html(
    total: int | None,
    tickers: list[str],
    *,
    header: list[str] = HEADER,
    start: int = 1,
) -> str:
    rows = "".join(
        f'<tr><td>{i}</td><td data-boxover-ticker="{t}">{t}</td><td>{t} Inc</td><td>Tech</td>'
        f"<td>Software</td><td>USA</td><td>1B</td><td>10</td><td>20.00</td><td>1.00%</td>"
        f"<td>2,000,000</td></tr>"
        for i, t in enumerate(tickers, start=start)
    )
    head = "".join(f"<th>{h}</th>" for h in header)
    count = f'<div class="count-text">#{start} / {total} Total</div>' if total is not None else ""
    return (
        f'<html><body>{PAD}{count}<table class="screener_table"><tr>{head}</tr>{rows}</table></body></html>'
    )


def news_html(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td>{when}</td><td><div class="news-link-left">'
        f'<a class="tab-link-news" href="https://example.com/{i}">{title}</a></div>'
        f'<div class="news-link-right"><span>(Src)</span></div></td></tr>'
        for i, (when, title) in enumerate(rows)
    )
    return f'<html><body>{PAD}<table id="news-table">{body}</table></body></html>'


class Timer:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Wall:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(cache: Path | None = None, wall: Wall | None = None) -> FinvizScraper:
    timer = Timer()
    return FinvizScraper(
        min_interval_s=2.0,
        cache_dir=cache,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
        wall=wall or Wall(1_000_000.0),
    )


def route_screener(pages: dict[tuple[str, str], str]) -> respx.Route:
    """Serve screener pages keyed by (f, r); the unfiltered market is always 9000 rows, and any
    other page not listed is a valid page with no rows (FinViz past the end)."""

    def handler(request: httpx.Request) -> httpx.Response:
        f = request.url.params.get("f", "")
        r = request.url.params.get("r", "1")
        if f == "" and ("", r) not in pages:
            return httpx.Response(200, text=screener_html(9000, ["ZZZ"]))
        return httpx.Response(200, text=pages.get((f, r), screener_html(None, [])))

    return respx.get(URL).mock(side_effect=handler)


@respx.mock
def test_table_without_count_text_is_an_error_not_a_truncated_universe() -> None:
    """A layout change that drops '#1 / N Total' makes total 0: the scraper must not return
    just page 1 (20 rows) as if it were the whole universe."""
    tickers = [f"T{i}" for i in range(20)]
    route_screener(
        {
            ("geo_usa", "1"): screener_html(None, tickers),
            ("geo_usa", "21"): screener_html(None, ["MORE1", "MORE2"], start=21),
        }
    )
    with pytest.raises(FinvizError):
        make().screen("geo_usa")


@respx.mock
def test_total_disagreeing_with_rows_is_an_error() -> None:
    """FinViz says 45 results but page 2 comes back empty: returning 20 of 45 rows is a silently
    wrong universe."""
    route_screener(
        {
            ("geo_usa", "1"): screener_html(45, [f"T{i}" for i in range(20)]),
            ("geo_usa", "21"): screener_html(45, [], start=21),
            ("geo_usa", "41"): screener_html(45, [], start=41),
        }
    )
    with pytest.raises(FinvizError):
        make().screen("geo_usa")


@pytest.mark.parametrize(
    "header",
    [
        [("Last" if h == "Price" else h) for h in HEADER],  # column renamed
        [h for h in HEADER if h != "Company"],  # header lost a column, rows did not
    ],
    ids=["price-renamed", "header-missing-column"],
)
@respx.mock
def test_layout_change_is_an_error_not_miskeyed_rows(header: list[str]) -> None:
    """If the header no longer matches what the parser expects, the universe must not come back
    with every price None (renamed column) or with zero rows (column count mismatch)."""
    tickers = [f"T{i}" for i in range(5)]
    route_screener({("geo_usa", "1"): screener_html(5, tickers, header=header)})
    with pytest.raises(FinvizError):
        make().universe("geo_usa")


def test_news_timestamps_year_boundary_midnight_noon_and_dst() -> None:
    html = news_html(
        [
            ("Today 12:05AM", "midnight today"),
            ("Jan-01-26 12:30PM", "new year noon"),
            ("12:01AM", "new year just after midnight"),
            ("Dec-31-25 11:59PM", "new year's eve"),
            ("Nov-02-26 09:30AM", "after fall-back (EST)"),
            ("Oct-30-26 09:30AM", "before fall-back (EDT)"),
            ("Mar-09-26 09:30AM", "after spring-forward (EDT)"),
            ("Mar-06-26 09:30AM", "before spring-forward (EST)"),
        ]
    )
    items = parse_news(html, today_et=date(2026, 1, 2))
    got = {h.title: h.ts for h in items}
    assert got == {
        "midnight today": datetime(2026, 1, 2, 5, 5, tzinfo=UTC),
        "new year noon": datetime(2026, 1, 1, 17, 30, tzinfo=UTC),
        "new year just after midnight": datetime(2026, 1, 1, 5, 1, tzinfo=UTC),
        "new year's eve": datetime(2026, 1, 1, 4, 59, tzinfo=UTC),
        "after fall-back (EST)": datetime(2026, 11, 2, 14, 30, tzinfo=UTC),
        "before fall-back (EDT)": datetime(2026, 10, 30, 13, 30, tzinfo=UTC),
        "after spring-forward (EDT)": datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        "before spring-forward (EST)": datetime(2026, 3, 6, 14, 30, tzinfo=UTC),
    }
    assert all(h.ts.tzinfo is not None for h in items)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (429, "<html>" + "x" * 5000 + "</html>"),
        (503, "<html>" + "x" * 5000 + "</html>"),
        (200, "<html><head><title>Just a moment...</title></head>" + "x" * 5000 + "</html>"),
        (502, "<html><head><title>502 Bad Gateway</title></head>" + "x" * 5000 + "</html>"),
    ],
    ids=["429", "503", "cloudflare-200", "502-long-body"],
)
@respx.mock
def test_blocked_or_error_responses_raise_and_are_not_cached(status: int, body: str, tmp_path: Path) -> None:
    route = respx.get("https://finviz.com/quote.ashx").mock(
        side_effect=[
            httpx.Response(status, text=body),
            httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "good")])),
        ]
    )
    s = make(tmp_path)
    with pytest.raises(FinvizError):
        s.news("AMD", today_et=date(2026, 9, 26))
    assert list(tmp_path.iterdir()) == []  # a block page must never be cached
    items = s.news("AMD", today_et=date(2026, 9, 26))
    assert [h.title for h in items] == ["good"]
    assert route.call_count == 2


@respx.mock
def test_stale_cache_beyond_ttl_is_refetched(tmp_path: Path) -> None:
    t0 = 2_000_000_000.0
    route = respx.get("https://finviz.com/quote.ashx").mock(
        side_effect=[
            httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "old")])),
            httpx.Response(200, text=news_html([("Sep-26-26 04:18PM", "new")])),
        ]
    )
    wall = Wall(t0)
    s = make(tmp_path, wall)
    assert [h.title for h in s.news("AMD", today_et=date(2026, 9, 26))] == ["old"]
    (cached,) = list(tmp_path.iterdir())
    os.utime(cached, (t0, t0))
    wall.t = t0 + 3600  # inside the 12 h TTL: served from cache
    assert [h.title for h in s.news("AMD", today_et=date(2026, 9, 26))] == ["old"]
    assert route.call_count == 1
    wall.t = t0 + 43_200 + 1  # past the TTL: refetched
    assert [h.title for h in s.news("AMD", today_et=date(2026, 9, 26))] == ["new"]
    assert route.call_count == 2


@respx.mock
def test_cached_news_page_read_after_midnight_keeps_its_real_date(tmp_path: Path) -> None:
    """A quote page cached at 22:30 ET on Sep 25 says 'Today 10:00PM'. Read from cache at 08:30 ET
    on Sep 26 (10 h later, inside the TTL), that headline is still from Sep 25, not Sep 26 (which
    would put it 14 h in the future)."""
    t0 = 2_000_000_000.0
    respx.get("https://finviz.com/quote.ashx").mock(
        side_effect=[
            httpx.Response(200, text=news_html([("Today 10:00PM", "late story")])),
            httpx.Response(200, text=news_html([("Sep-25-26 10:00PM", "late story")])),
        ]
    )
    wall = Wall(t0)
    s = make(tmp_path, wall)
    first = s.news("AMD", today_et=date(2026, 9, 25))
    assert first[0].ts == datetime(2026, 9, 26, 2, 0, tzinfo=UTC)
    (cached,) = list(tmp_path.iterdir())
    os.utime(cached, (t0, t0))
    wall.t = t0 + 10 * 3600
    second = s.news("AMD", today_et=date(2026, 9, 26))
    assert second[0].ts == datetime(2026, 9, 26, 2, 0, tzinfo=UTC)


@respx.mock
def test_empty_filters_skip_guard_and_share_class_tickers_map_both_ways() -> None:
    """Empty filters legitimately equal the unfiltered total: no FinvizFilterIgnored, no extra
    request. Dashed/dotted share classes map to Questrade form, and news() for a Questrade-form
    ticker asks FinViz for its dashed form."""
    page1 = ["BRK-B", "BF-B", "BRK.A", "AAPL"] + [f"T{i}" for i in range(16)]
    route = route_screener(
        {
            ("", "1"): screener_html(22, page1),
            ("", "21"): screener_html(22, ["HEI-A", "MOG-A"], start=21),
        }
    )
    s = make()
    try:
        rows = s.universe("")
    except FinvizFilterIgnored:  # pragma: no cover - this is the failure being tested
        pytest.fail("empty filters must not trip the ignored-filter guard")
    tickers = [r.ticker for r in rows]
    assert tickers[:4] == ["BRK.B", "BF.B", "BRK.A", "AAPL"]
    assert tickers[-2:] == ["HEI.A", "MOG.A"]
    assert len(tickers) == 22
    assert route.call_count == 2

    quote = respx.get("https://finviz.com/quote.ashx").mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "x")]))
    )
    s.news("BF.B", today_et=date(2026, 9, 26))
    assert quote.calls.last.request.url.params["t"] == "BF-B"
