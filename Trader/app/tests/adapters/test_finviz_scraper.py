import logging
import os
import stat
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

import trader.adapters.finviz.scraper as scraper_mod
from trader.adapters.finviz.scraper import (
    BLOCK_BACKOFF_S,
    FinvizBlocked,
    FinvizFilterIgnored,
    FinvizHttpError,
    FinvizParseError,
    FinvizScraper,
)


def screener_html(total: int, tickers: list[str]) -> str:
    rows = "".join(
        f'<tr><td>{i}</td><td data-boxover-ticker="{t}">{t}</td><td>{t} Inc</td><td>Tech</td>'
        f"<td>Software</td><td>USA</td><td>1B</td><td>10</td><td>20.00</td><td>1.00%</td><td>2,000,000</td></tr>"
        for i, t in enumerate(tickers, start=1)
    )
    head = "".join(
        f"<th>{h}</th>"
        for h in [
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
    )
    pad = "<!--" + "x" * 2000 + "-->"
    return (
        f'<html><body>{pad}<div class="count-text">#1 / {total} Total</div>'
        f'<table class="screener_table"><tr>{head}</tr>{rows}</table></body></html>'
    )


class Timer:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def scraper(timer: Timer, tmp: Path | None = None) -> FinvizScraper:
    return FinvizScraper(
        min_interval_s=2.0,
        cache_dir=tmp,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
        wall=lambda: 1_000_000.0,
    )


@respx.mock
def test_pages_through_all_results_politely() -> None:
    tickers = [f"T{i}" for i in range(25)]
    unfiltered = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "", "r": "1"})
    unfiltered.mock(return_value=httpx.Response(200, text=screener_html(9000, ["Z"])))
    p1 = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "geo_usa", "r": "1"})
    p1.mock(return_value=httpx.Response(200, text=screener_html(25, tickers[:20])))
    p2 = respx.get("https://finviz.com/screener.ashx", params={"v": "111", "f": "geo_usa", "r": "21"})
    p2.mock(return_value=httpx.Response(200, text=screener_html(25, tickers[20:])))
    timer = Timer()
    rows = scraper(timer).universe("geo_usa")
    assert [r.ticker for r in rows] == tickers
    assert all(s >= 1.99 for s in timer.sleeps)  # ≥ 2 s between requests after the first
    assert len(timer.sleeps) == 2


@respx.mock
def test_filter_ignored_raises() -> None:
    respx.get("https://finviz.com/screener.ashx").mock(
        return_value=httpx.Response(200, text=screener_html(9000, ["A"]))
    )
    with pytest.raises(FinvizFilterIgnored):
        scraper(Timer()).screen("bogus_filter_code")


@respx.mock
def test_block_page_raises() -> None:
    respx.get("https://finviz.com/screener.ashx").mock(return_value=httpx.Response(403, text="denied"))
    with pytest.raises(FinvizBlocked):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_sends_browser_user_agent() -> None:
    route = respx.get("https://finviz.com/quote.ashx", params={"t": "AAPL"}).mock(
        return_value=httpx.Response(
            200, text=(Path(__file__).parents[1] / "fixtures/finviz/raw_quote_AAPL.html").read_text()
        )
    )
    items = scraper(Timer()).news("AAPL", today_et=date(2026, 9, 26))
    assert items
    assert "Mozilla/5.0" in route.calls.last.request.headers["User-Agent"]


@respx.mock
def test_cache_avoids_second_request(tmp_path: Path) -> None:
    html = (Path(__file__).parents[1] / "fixtures/finviz/raw_quote_AMD.html").read_text()
    route = respx.get("https://finviz.com/quote.ashx", params={"t": "AMD"}).mock(
        return_value=httpx.Response(200, text=html)
    )
    s = scraper(Timer(), tmp_path)
    s.news("AMD", today_et=date(2026, 9, 26))
    s.news("AMD", today_et=date(2026, 9, 26))
    assert route.call_count == 1


# --- gauntlet fix round (attempt 2): every failure raises; nothing that raises is cached ---

URL = "https://finviz.com/screener.ashx"
QUOTE = "https://finviz.com/quote.ashx"
HEADER = ["No.", "Ticker", "Company", "Sector", "Industry", "Country", "Market Cap", "P/E", "Price",
          "Change %", "Volume"]  # fmt: skip
PAD = "<!--" + "x" * 2000 + "-->"


def page_html(
    total: int | None, tickers: list[str], header: list[str] = HEADER, cells: int | None = None
) -> str:
    """A screener page. `total=None` drops the count text; `cells` overrides the cells per row."""
    n = len(header) if cells is None else cells
    rows = "".join(
        f'<tr><td>{i}</td><td data-boxover-ticker="{t}">{t}</td>' + "<td>20.00</td>" * (n - 2) + "</tr>"
        for i, t in enumerate(tickers, start=1)
    )
    head = "".join(f"<th>{h}</th>" for h in header)
    count = f'<div class="count-text">#1 / {total} Total</div>' if total is not None else ""
    table = f'<table class="screener_table"><tr>{head}</tr>{rows}</table>' if header else ""
    return f"<html><body>{PAD}{count}{table}</body></html>"


def news_html(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<tr><td>{when}</td><td><a class="tab-link-news" href="/n/{i}">{title}</a></td></tr>'
        for i, (when, title) in enumerate(rows)
    )
    return f'<html><body>{PAD}<table id="news-table">{body}</table></body></html>'


def serve(pages: dict[tuple[str, str], str], unfiltered_total: int | None = 9000) -> respx.Route:
    """Screener pages keyed by (f, r). Unlisted unfiltered pages report `unfiltered_total`; any other
    unlisted page is a valid page with no rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.url.params.get("f", ""), request.url.params.get("r", "1"))
        if key in pages:
            return httpx.Response(200, text=pages[key])
        if key[0] == "":
            return httpx.Response(200, text=page_html(unfiltered_total, ["ZZZ"]))
        return httpx.Response(200, text=page_html(None, []))

    return respx.get(URL).mock(side_effect=handler)


@respx.mock
def test_missing_count_text_is_a_parse_error() -> None:
    serve({("geo_usa", "1"): page_html(None, ["A", "B"])})
    with pytest.raises(FinvizParseError, match="count"):
        scraper(Timer()).screen("geo_usa")


@pytest.mark.parametrize("header", [[], ["No.", "Symbol", "Price"]], ids=["no-table", "no-ticker-column"])
@respx.mock
def test_header_without_ticker_is_a_parse_error(header: list[str]) -> None:
    serve({("geo_usa", "1"): page_html(3, ["A", "B", "C"], header=header)})
    with pytest.raises(FinvizParseError, match="Ticker"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_row_cell_count_mismatch_is_a_parse_error() -> None:
    serve({("geo_usa", "1"): page_html(2, ["A", "B"], cells=len(HEADER) + 1)})
    with pytest.raises(FinvizParseError, match="do not match"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_later_page_with_a_different_header_is_a_parse_error() -> None:
    serve({
        ("geo_usa", "1"): page_html(25, [f"T{i}" for i in range(20)]),
        ("geo_usa", "21"): page_html(25, [f"U{i}" for i in range(5)], header=[*HEADER[:-1], "Vol"]),
    })  # fmt: skip
    with pytest.raises(FinvizParseError, match="header changed"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_duplicate_tickers_across_pages_leave_a_short_universe_and_raise() -> None:
    """Rows shifting between page requests repeat one ticker and drop another."""
    serve({
        ("geo_usa", "1"): page_html(25, [f"T{i}" for i in range(20)]),
        ("geo_usa", "21"): page_html(25, ["T19", "U1", "U2", "U3", "U4"]),
    })  # fmt: skip
    with pytest.raises(FinvizParseError, match="24 unique"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_total_beyond_page_cap_is_a_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scraper_mod, "MAX_PAGES", 2)
    route = serve({("geo_usa", "1"): page_html(41, [f"T{i}" for i in range(20)])})
    with pytest.raises(FinvizParseError, match="cap"):
        scraper(Timer()).screen("geo_usa")
    assert route.call_count == 2  # page 1 and the unfiltered total, nothing more


@respx.mock
def test_hitting_the_page_cap_while_paging_is_a_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scraper_mod, "MAX_PAGES", 2)
    serve({
        ("geo_usa", "1"): page_html(30, [f"T{i}" for i in range(10)]),
        ("geo_usa", "21"): page_html(30, [f"U{i}" for i in range(10)]),
        ("geo_usa", "41"): page_html(30, [f"V{i}" for i in range(10)]),
    })  # fmt: skip
    with pytest.raises(FinvizParseError, match="MAX_PAGES"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_universe_requires_its_columns() -> None:
    header = [h for h in HEADER if h != "Volume"]
    serve({("geo_usa", "1"): page_html(2, ["A", "B"], header=header)})
    with pytest.raises(FinvizParseError, match="Volume"):
        scraper(Timer()).universe("geo_usa")


@respx.mock
def test_empty_screen_is_allowed_but_an_empty_universe_is_not() -> None:
    serve({("geo_usa", "1"): page_html(0, [])})
    page = scraper(Timer()).screen("geo_usa")
    assert page.total == 0 and page.rows == []
    with pytest.raises(FinvizParseError, match="empty"):
        scraper(Timer()).universe("geo_usa")


@respx.mock
def test_unreadable_totals_are_a_parse_error_not_filter_ignored() -> None:
    """The unfiltered page shows no count: we can't tell whether the filter was ignored, so it's a
    layout problem, not FinvizFilterIgnored. (Fix round P2-T14: a page without a count is itself a
    parse error, so a filtered page without one fails the same way.)"""
    serve({("geo_usa", "1"): page_html(3, ["A", "B", "C"]), ("", "1"): page_html(None, [])})
    with pytest.raises(FinvizParseError, match="count"):
        scraper(Timer()).screen("geo_usa")
    serve({("geo_usa", "1"): page_html(None, []), ("", "1"): page_html(None, [])})
    with pytest.raises(FinvizParseError, match="count"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_ignored_filter_guard_compares_against_the_same_signal() -> None:
    route = serve({
        ("geo_usa", "1"): page_html(3, ["A", "B", "C"]),
        ("", "1"): page_html(3, ["A", "B", "C"]),  # the signal alone gives the same 3
    })  # fmt: skip
    with pytest.raises(FinvizFilterIgnored):
        scraper(Timer()).screen("geo_usa", signal="n_majornews")
    baseline = route.calls[1].request.url.params
    assert baseline["f"] == "" and baseline["s"] == "n_majornews"


@respx.mock
def test_ignored_signal_without_filters_is_detected() -> None:
    route = serve({})  # a bogus signal returns the whole market (9000)
    with pytest.raises(FinvizFilterIgnored):
        scraper(Timer()).screen("", signal="bogus_signal")
    assert "s" not in route.calls[1].request.url.params


@pytest.mark.parametrize("status", [301, 404, 500, 502])
@respx.mock
def test_other_non_2xx_statuses_raise_http_error(status: int, tmp_path: Path) -> None:
    respx.get(QUOTE).mock(return_value=httpx.Response(status, text="<html>" + "x" * 5000 + "</html>"))
    with pytest.raises(FinvizHttpError, match=str(status)):
        scraper(Timer(), tmp_path).news("AMD", today_et=date(2026, 9, 26))
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_403_is_blocked_and_not_retried() -> None:
    timer = Timer()
    route = respx.get(URL).mock(return_value=httpx.Response(403, text="x" * 5000))
    with pytest.raises(FinvizBlocked, match="403"):
        scraper(timer).screen("geo_usa")
    assert route.call_count == 1
    assert timer.sleeps == []


@pytest.mark.parametrize("status", [429, 503])
@respx.mock
def test_screener_backs_off_30_then_90_seconds_then_raises(status: int) -> None:
    timer = Timer()
    route = respx.get(URL).mock(return_value=httpx.Response(status, text="x" * 5000))
    with pytest.raises(FinvizBlocked, match=str(status)):
        scraper(timer).screen("geo_usa")
    assert route.call_count == 3
    assert timer.sleeps == list(BLOCK_BACKOFF_S) == [30.0, 90.0]


@respx.mock
def test_screener_recovers_after_one_backoff() -> None:
    timer = Timer()
    ok = page_html(2, ["A", "B"])
    respx.get(URL, params={"f": "geo_usa"}).mock(
        side_effect=[httpx.Response(429, text="x" * 5000), httpx.Response(200, text=ok)]
    )
    respx.get(URL, params={"f": ""}).mock(return_value=httpx.Response(200, text=page_html(9000, ["Z"])))
    rows = scraper(timer).universe("geo_usa")
    assert [r.ticker for r in rows] == ["A", "B"]
    assert timer.sleeps[0] == 30.0
    assert all(s == 2.0 for s in timer.sleeps[1:])


@respx.mock
def test_news_429_is_not_retried() -> None:
    """Pre-market news is per ticker: raise at once so the caller can skip it (no 2-minute stall)."""
    timer = Timer()
    route = respx.get(QUOTE).mock(return_value=httpx.Response(429, text="x" * 5000))
    with pytest.raises(FinvizBlocked):
        scraper(timer).news("AMD", today_et=date(2026, 9, 26))
    assert route.call_count == 1 and timer.sleeps == []


@respx.mock
def test_transport_errors_are_wrapped_and_spacing_still_holds(tmp_path: Path) -> None:
    timer = Timer()
    route = respx.get(QUOTE).mock(
        side_effect=[
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "ok")])),
        ]
    )
    s = scraper(timer, tmp_path)
    with pytest.raises(FinvizHttpError, match="ConnectTimeout"):
        s.news("AMD", today_et=date(2026, 9, 26))
    assert list(tmp_path.iterdir()) == []
    assert [h.title for h in s.news("AMD", today_et=date(2026, 9, 26))] == ["ok"]
    assert route.call_count == 2
    assert timer.sleeps == [2.0]  # the failed request still counts for the 2 s spacing


@respx.mock
def test_news_page_layout_change_raises_and_is_not_cached(tmp_path: Path) -> None:
    respx.get(QUOTE).mock(return_value=httpx.Response(200, text=f"<html><body>{PAD}redesigned</body></html>"))
    with pytest.raises(FinvizParseError, match="news-table"):
        scraper(Timer(), tmp_path).news("AMD", today_et=date(2026, 9, 26))
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_news_with_headlines_that_all_fail_to_parse_raises() -> None:
    respx.get(QUOTE).mock(return_value=httpx.Response(200, text=news_html([("25/09 16:18", "a")])))
    with pytest.raises(FinvizParseError, match="headline rows"):
        scraper(Timer()).news("AMD", today_et=date(2026, 9, 26))


@respx.mock
def test_failed_screen_caches_none_of_its_pages(tmp_path: Path) -> None:
    serve({
        ("geo_usa", "1"): page_html(45, [f"T{i}" for i in range(20)]),
        ("geo_usa", "21"): page_html(45, []),
    })  # fmt: skip
    with pytest.raises(FinvizParseError):
        scraper(Timer(), tmp_path).screen("geo_usa")
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_successful_screen_is_cached(tmp_path: Path) -> None:
    route = serve({("geo_usa", "1"): page_html(2, ["A", "B"])})
    s = scraper(Timer(), tmp_path)
    s.screen("geo_usa")
    assert len(list(tmp_path.iterdir())) == 2  # page 1 and the unfiltered page
    scraper(Timer(), tmp_path).screen("geo_usa")
    assert route.call_count == 2


@respx.mock
def test_news_cache_is_keyed_by_the_et_fetch_date(tmp_path: Path) -> None:
    route = respx.get(QUOTE).mock(return_value=httpx.Response(200, text=news_html([("Today 09:00AM", "x")])))
    s = scraper(Timer(), tmp_path)
    assert s.news("AMD", today_et=date(2026, 9, 25))[0].ts.day == 25
    assert s.news("AMD", today_et=date(2026, 9, 25))[0].ts.day == 25
    assert route.call_count == 1
    assert s.news("AMD", today_et=date(2026, 9, 26))[0].ts.day == 26
    assert route.call_count == 2


@respx.mock
def test_cache_is_utf8_and_atomic(tmp_path: Path) -> None:
    respx.get(QUOTE).mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "Nestlé – €5")]))
    )
    s = scraper(Timer(), tmp_path)
    s.news("NSRGY", today_et=date(2026, 9, 26))
    (cached,) = list(tmp_path.iterdir())  # no temp file left behind
    assert cached.suffix == ".html"
    assert "Nestlé – €5" in cached.read_text(encoding="utf-8")
    assert [h.title for h in s.news("NSRGY", today_et=date(2026, 9, 26))] == ["Nestlé – €5"]


@respx.mock
def test_cached_block_page_is_not_trusted(tmp_path: Path) -> None:
    route = respx.get(QUOTE).mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "ok")]))
    )
    s = scraper(Timer(), tmp_path)
    s.news("AMD", today_et=date(2026, 9, 26))
    (cached,) = list(tmp_path.iterdir())
    cached.write_text("<title>Just a moment...</title>" + "x" * 5000, encoding="utf-8")
    assert [h.title for h in s.news("AMD", today_et=date(2026, 9, 26))] == ["ok"]
    assert route.call_count == 2


def test_cache_dir_is_created_private(tmp_path: Path) -> None:
    target = tmp_path / "a" / "finviz"
    scraper(Timer(), target)
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


@respx.mock
def test_cache_dir_owned_by_someone_else_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scraper_mod.os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    route = respx.get(QUOTE).mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "x")]))
    )
    with caplog.at_level(logging.WARNING):
        s = scraper(Timer(), tmp_path)
    assert "not using" in caplog.text
    s.news("AMD", today_et=date(2026, 9, 26))
    s.news("AMD", today_et=date(2026, 9, 26))
    assert route.call_count == 2
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_news_maps_questrade_tickers_to_finviz_form() -> None:
    route = respx.get(QUOTE).mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "x")]))
    )
    s = scraper(Timer())
    s.news("BRK.B", today_et=date(2026, 9, 26))
    assert route.calls.last.request.url.params["t"] == "BRK-B"
    s.news("BF.B", today_et=date(2026, 9, 26))
    assert route.calls.last.request.url.params["t"] == "BF-B"


def test_context_manager_closes_its_own_client() -> None:
    with FinvizScraper() as s:
        http = s._http
        assert not http.is_closed
    assert http.is_closed


@respx.mock
def test_caller_client_is_not_mutated_or_closed_but_gets_browser_headers() -> None:
    route = respx.get(QUOTE).mock(
        return_value=httpx.Response(200, text=news_html([("Sep-25-26 04:18PM", "x")]))
    )
    client = httpx.Client(headers={"X-Mine": "1"})
    before = dict(client.headers)
    with FinvizScraper(client, sleep=Timer().sleep, monotonic=Timer().monotonic) as s:
        s.news("AMD", today_et=date(2026, 9, 26))
    assert dict(client.headers) == before
    assert not client.is_closed
    sent = route.calls.last.request.headers
    assert "Mozilla/5.0" in sent["User-Agent"] and sent["X-Mine"] == "1"
    client.close()


# --- P2-T14 fix round: the "matched nothing" page, fresh screens, count() ---

ZERO_HTML = (Path(__file__).parents[1] / "fixtures/finviz/raw_screener_zero.html").read_text(encoding="utf-8")


@respx.mock
def test_live_zero_match_page_is_an_empty_screen() -> None:
    route = serve({("cap_mega,sh_price_u1", "1"): ZERO_HTML})
    page = scraper(Timer()).screen("cap_mega,sh_price_u1")
    assert page.total == 0 and page.rows == [] and page.verified_empty
    assert route.call_count == 1  # 0 can never equal the whole market: no baseline request


@respx.mock
def test_live_zero_match_page_is_still_an_error_for_the_universe() -> None:
    serve({("cap_mega,sh_price_u1", "1"): ZERO_HTML})
    with pytest.raises(FinvizParseError, match="empty"):
        scraper(Timer()).universe("cap_mega,sh_price_u1")


@respx.mock
def test_positive_count_without_a_table_raises() -> None:
    serve({("geo_usa", "1"): f'<html><body>{PAD}<div class="count-text">#1 / 5 Total</div></body></html>'})
    with pytest.raises(FinvizParseError, match="Ticker"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_no_count_and_no_table_raises() -> None:
    serve({("geo_usa", "1"): f"<html><body>{PAD}<p>No results found.</p></body></html>"})
    with pytest.raises(FinvizParseError, match="count"):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_zero_count_with_a_table_needs_a_ticker_column() -> None:
    serve({("geo_usa", "1"): page_html(0, [], header=["No.", "Symbol"])})
    with pytest.raises(FinvizParseError, match="Ticker"):
        scraper(Timer()).screen("geo_usa")
    serve({("geo_usa", "1"): page_html(0, [])})
    assert scraper(Timer()).screen("geo_usa").rows == []


@respx.mock
def test_zero_count_with_rows_raises() -> None:
    serve({("geo_usa", "1"): page_html(0, ["A"])})
    with pytest.raises(FinvizParseError):
        scraper(Timer()).screen("geo_usa")


@respx.mock
def test_uncached_screens_fetch_fresh_but_news_stays_cached(tmp_path: Path) -> None:
    route = serve({("geo_usa", "1"): page_html(2, ["A", "B"])})
    quote = respx.get(QUOTE).mock(return_value=httpx.Response(200, text=news_html([("Today 09:00AM", "x")])))
    timer = Timer()
    s = FinvizScraper(
        min_interval_s=2.0,
        cache_dir=tmp_path,
        sleep=timer.sleep,
        monotonic=timer.monotonic,
        wall=lambda: 1_000_000.0,
        cache_screens=False,
    )
    s.screen("geo_usa")
    s.screen("geo_usa")
    assert route.call_count == 3  # page 1 twice, the unfiltered baseline once (memoised per scraper)
    s.news("AMD", today_et=date(2026, 9, 25))
    s.news("AMD", today_et=date(2026, 9, 25))
    assert quote.call_count == 1
    assert len(list(tmp_path.iterdir())) == 1  # only the quote page was cached


@respx.mock
def test_count_reads_the_first_page_only() -> None:
    route = serve({("geo_usa", "1"): page_html(45, [f"T{i}" for i in range(20)])})
    assert scraper(Timer()).count("geo_usa") == 45
    assert route.call_count == 1
    serve({("x", "1"): page_html(None, ["A"])})
    with pytest.raises(FinvizParseError, match="count"):
        scraper(Timer()).count("x")


# --- Earnings window fix: custom view 152 with an explicit column list ---

EARN_FIX = Path(__file__).parents[1] / "fixtures/finviz"


@respx.mock
def test_screen_with_columns_sends_the_custom_view_and_reads_the_earnings_column() -> None:
    live = (EARN_FIX / "raw_screener_earnings_thisweek_p1.html").read_text(encoding="utf-8")
    live2 = (EARN_FIX / "raw_screener_earnings_thisweek_p2.html").read_text(encoding="utf-8")
    route = serve({("earningsdate_thisweek", "1"): live, ("earningsdate_thisweek", "21"): live2})
    page = scraper(Timer()).screen("earningsdate_thisweek", 152, columns="0,1,2,68")
    assert page.total == 38 and len(page.rows) == 38 and page.rows[0]["Earnings"] == "Sep 21/b"
    sent = [c.request.url.params for c in route.calls]
    screens = [p for p in sent if p.get("f")]
    assert all(p.get("v") == "152" and p.get("c") == "0,1,2,68" for p in screens)
    assert len(screens) == 2 and any(p.get("f") == "" for p in sent)  # both pages + the baseline


@respx.mock
def test_columns_are_part_of_the_cache_key(tmp_path: Path) -> None:
    route = serve({("geo_usa", "1"): page_html(2, ["A", "B"])})
    s = scraper(Timer(), tmp_path)
    s.screen("geo_usa", 152, columns="0,1")
    s.screen("geo_usa", 152, columns="0,1,68")
    s.screen("geo_usa", 152, columns="0,1")  # cached
    assert sum(1 for c in route.calls if c.request.url.params.get("f") == "geo_usa") == 2


@respx.mock
def test_live_earnings_zero_page_is_an_empty_screen_with_columns() -> None:
    zero = (EARN_FIX / "raw_screener_earnings_zero.html").read_text(encoding="utf-8")
    serve({("x,earningsdate_yesterdayafter", "1"): zero})
    page = scraper(Timer()).screen("x,earningsdate_yesterdayafter", 152, columns="0,1,2,68")
    assert page.total == 0 and page.rows == []
