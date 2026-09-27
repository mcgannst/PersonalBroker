"""Polite FinViz client (SPEC §4.2): ≥ 2 s between requests, browser User-Agent, 12-hour cache."""

import hashlib
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx

from trader.adapters.finviz.parser import (
    BASE,
    Headline,
    ScreenerPage,
    UniverseRow,
    blocked_reason,
    parse_news,
    parse_screener,
    parse_universe_row,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PAGE_SIZE = 20
MAX_PAGES = 100


class FinvizError(Exception):
    pass


class FinvizBlocked(FinvizError):
    pass


class FinvizFilterIgnored(FinvizError):
    """FinViz silently ignores unknown filter codes and returns everything (spike S5)."""


class FinvizScraper:
    def __init__(
        self,
        http: httpx.Client | None = None,
        *,
        min_interval_s: float = 2.0,
        cache_dir: Path | None = None,
        cache_ttl_s: float = 43200,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._http = http or httpx.Client(timeout=20, follow_redirects=True)
        self._http.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._min_interval = min_interval_s
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl_s
        self._sleep, self._monotonic, self._wall = sleep, monotonic, wall
        self._last: float | None = None
        self._unfiltered_totals: dict[int, int] = {}

    def close(self) -> None:
        self._http.close()

    def _cache_path(self, url: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".html")

    def _get(self, path: str, params: dict[str, str]) -> str:
        url = str(httpx.URL(BASE + path, params=params))
        cached = self._cache_path(url)
        if cached and cached.exists() and self._wall() - cached.stat().st_mtime < self._cache_ttl:
            return cached.read_text()
        if self._last is not None:
            wait = self._min_interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        response = self._http.get(BASE + path, params=params)
        self._last = self._monotonic()
        reason = blocked_reason(response.status_code, response.text)
        if reason:
            raise FinvizBlocked(f"{path}?{params}: {reason}")
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(response.text)
        return response.text

    def _page(self, filters: str, view: int, start: int, signal: str | None) -> ScreenerPage:
        params = {"v": str(view), "f": filters, "r": str(start)}
        if signal:
            params["s"] = signal
        return parse_screener(self._get("/screener.ashx", params))

    def _unfiltered_total(self, view: int) -> int:
        if view not in self._unfiltered_totals:
            self._unfiltered_totals[view] = self._page("", view, 1, None).total
        return self._unfiltered_totals[view]

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        first = self._page(filters, view, 1, signal)
        if filters and first.total == self._unfiltered_total(view):
            raise FinvizFilterIgnored(f"filters {filters!r} returned the whole market ({first.total})")
        rows = list(first.rows)
        start = 1 + PAGE_SIZE
        while len(rows) < first.total and start <= PAGE_SIZE * MAX_PAGES:
            page = self._page(filters, view, start, signal)
            if not page.rows:
                break
            rows.extend(page.rows)
            start += PAGE_SIZE
        return ScreenerPage(first.total, first.header, rows)

    def universe(self, filters: str) -> list[UniverseRow]:
        return [parse_universe_row(r) for r in self.screen(filters).rows]

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        return parse_news(self._get("/quote.ashx", {"t": ticker}), today_et)
