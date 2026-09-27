from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from trader.adapters.finviz.scraper import FinvizBlocked, FinvizFilterIgnored, FinvizScraper


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
