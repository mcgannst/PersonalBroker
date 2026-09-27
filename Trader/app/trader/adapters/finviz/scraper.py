"""Polite FinViz client (SPEC §4.2): ≥ 2 s between requests, browser User-Agent, 12-hour cache,
backoff when blocked.

Every failure raises a FinvizError. The nightly job falls back to the previous universe only on
FinvizError, so an empty, short or mis-keyed result must never be returned silently. Nothing that
raises is ever cached.
"""

import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import urlencode

import httpx

from trader.adapters.finviz.parser import (
    BASE,
    UNIVERSE_COLUMNS,
    Headline,
    ScreenerPage,
    UniverseRow,
    blocked_reason,
    parse_news_page,
    parse_screener,
    parse_universe_row,
    to_finviz_ticker,
)

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
PAGE_SIZE = 20
MAX_PAGES = 100
BLOCK_STATUSES = frozenset({403, 429, 503})
RETRY_STATUSES = frozenset({429, 503})  # 403 is not retried
BLOCK_BACKOFF_S = (30.0, 90.0)  # waits before the 1st and 2nd retry of a blocked screener request

type SaveToCache = Callable[[], None]


class FinvizError(Exception):
    """Any FinViz failure. Callers fall back (e.g. to the previous universe) on this."""


class FinvizBlocked(FinvizError):
    """HTTP 403/429/503, an empty body, or a bot-check interstitial."""


class FinvizHttpError(FinvizError):
    """Any other non-2xx status, or a transport error (timeout, connection failure)."""


class FinvizParseError(FinvizError):
    """The page was served but doesn't read as expected (layout change, short or empty result)."""


class FinvizFilterIgnored(FinvizError):
    """FinViz silently ignores unknown filter codes and returns everything (spike S5)."""


def _usable_cache_dir(path: Path | None) -> Path | None:
    """Create the cache dir private (0o700). Refuse one we don't own: another user could plant pages."""
    if path is None:
        return None
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner = path.stat().st_uid
    except OSError as exc:
        log.warning("FinViz cache: not using %s: %s", path, exc)
        return None
    if owner != os.getuid():
        log.warning("FinViz cache: not using %s: owned by uid %s, not uid %s", path, owner, os.getuid())
        return None
    return path


def _checked_body(where: str, response: httpx.Response) -> str:
    status = response.status_code
    if status in BLOCK_STATUSES:
        raise FinvizBlocked(f"{where}: HTTP {status}")
    if not response.is_success:
        raise FinvizHttpError(f"{where}: HTTP {status}")
    reason = blocked_reason(status, response.text)
    if reason:
        raise FinvizBlocked(f"{where}: {reason}")
    return response.text


def _checked_screener(html: str) -> ScreenerPage:
    page = parse_screener(html)
    if "Ticker" not in page.header:
        raise FinvizParseError(f"screener table missing or its header has no 'Ticker' column: {page.header}")
    if page.bad_rows:
        raise FinvizParseError(
            f"{page.bad_rows} screener rows do not match the {len(page.header)}-column header"
        )
    if page.total == 0 and page.rows:
        raise FinvizParseError("screener result count ('#1 / N Total') is missing")
    return page


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
        self._owns_http = http is None
        self._http = http if http is not None else httpx.Client(timeout=20, follow_redirects=True)
        self._min_interval = min_interval_s
        self._cache_dir = _usable_cache_dir(cache_dir)
        self._cache_ttl = cache_ttl_s
        self._sleep, self._monotonic, self._wall = sleep, monotonic, wall
        self._last: float | None = None
        self._unfiltered_totals: dict[tuple[int, str | None], int] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP client, but only one this scraper created."""
        if self._owns_http:
            self._http.close()

    # --- cache ---

    def _cache_path(self, key: str) -> Path | None:
        if self._cache_dir is None:
            return None
        return self._cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".html")

    def _cache_read(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            if self._wall() - path.stat().st_mtime >= self._cache_ttl:
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        reason = blocked_reason(200, text)
        if reason:
            log.warning("FinViz cache: ignoring %s: %s", path.name, reason)
            return None
        return text

    def _cache_write(self, path: Path, text: str) -> None:
        """Atomic: write a temp file in the same dir, then rename it over the target."""
        tmp: str | None = None
        try:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("FinViz cache: could not write %s: %s", path.name, exc)
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # --- HTTP ---

    def _request(self, path: str, params: dict[str, str]) -> httpx.Response:
        if self._last is not None:
            wait = self._min_interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        try:
            return self._http.get(BASE + path, params=params, headers=BROWSER_HEADERS)
        except httpx.HTTPError as exc:
            raise FinvizHttpError(f"{path}?{urlencode(params)}: {type(exc).__name__}: {exc}") from exc
        finally:
            self._last = self._monotonic()  # a failed request still counts for the spacing

    def _download(self, path: str, params: dict[str, str], retry_blocked: bool) -> str:
        where = f"{path}?{urlencode(params)}"
        response = self._request(path, params)
        for wait in BLOCK_BACKOFF_S if retry_blocked else ():
            if response.status_code not in RETRY_STATUSES:
                break
            log.warning("FinViz %s: HTTP %s, retrying in %.0f s", where, response.status_code, wait)
            self._sleep(wait)
            response = self._request(path, params)
        return _checked_body(where, response)

    def _fetch[T](
        self,
        path: str,
        params: dict[str, str],
        parse: Callable[[str], T],
        *,
        cache_tag: str = "",
        retry_blocked: bool = False,
    ) -> tuple[T, SaveToCache]:
        """Return the parsed page and a callback that caches it. The caller calls the callback only
        once the whole result has passed validation, so nothing that raises is ever cached."""
        cache = self._cache_path(str(httpx.URL(BASE + path, params=params)) + cache_tag)
        cached = self._cache_read(cache)
        if cached is not None:
            try:
                return parse(cached), lambda: None
            except FinvizError as exc:
                log.warning("FinViz cache: ignoring %s: %s", path, exc)
        text = self._download(path, params, retry_blocked)
        value = parse(text)

        def save() -> None:
            if cache is not None:
                self._cache_write(cache, text)

        return value, save

    # --- screener ---

    def _page(
        self, filters: str, view: int, start: int, signal: str | None, saves: list[SaveToCache]
    ) -> ScreenerPage:
        params = {"v": str(view), "f": filters, "r": str(start)}
        if signal:
            params["s"] = signal
        page, save = self._fetch("/screener.ashx", params, _checked_screener, retry_blocked=True)
        saves.append(save)
        return page

    def _check_filters_applied(
        self, total: int, filters: str, view: int, signal: str | None, saves: list[SaveToCache]
    ) -> None:
        """FinViz ignores unknown codes. Compare with the same request minus the filters (or, with no
        filters, minus the signal): an equal count means the filters did nothing."""
        base_signal = signal if filters else None
        key = (view, base_signal)
        if key not in self._unfiltered_totals:
            baseline = self._page("", view, 1, base_signal, saves).total
            if baseline == 0:
                raise FinvizParseError("could not read the unfiltered result count")
            self._unfiltered_totals[key] = baseline
        if total == self._unfiltered_totals[key]:
            what = f"filters {filters!r}" if filters else f"signal {signal!r}"
            raise FinvizFilterIgnored(f"{what} returned the whole market ({total})")

    def _screen(
        self,
        filters: str,
        view: int,
        signal: str | None,
        required: tuple[str, ...] = ("Ticker",),
        allow_empty: bool = True,
    ) -> ScreenerPage:
        saves: list[SaveToCache] = []
        first = self._page(filters, view, 1, signal, saves)
        missing = [c for c in required if c not in first.header]
        if missing:
            raise FinvizParseError(f"screener header lacks {missing}: {first.header}")
        if filters or signal:
            self._check_filters_applied(first.total, filters, view, signal, saves)
        if first.total > PAGE_SIZE * MAX_PAGES:
            raise FinvizParseError(f"{first.total} results exceed the page cap ({MAX_PAGES} x {PAGE_SIZE})")
        rows = list(first.rows)
        pages, start = 1, 1 + PAGE_SIZE
        while len(rows) < first.total:
            if pages >= MAX_PAGES:
                raise FinvizParseError(f"hit the MAX_PAGES cap ({MAX_PAGES}) with {len(rows)}/{first.total}")
            page = self._page(filters, view, start, signal, saves)
            if page.header != first.header:
                raise FinvizParseError(f"screener header changed on row {start}: {page.header}")
            if not page.rows:
                break
            rows.extend(page.rows)
            pages, start = pages + 1, start + PAGE_SIZE
        unique: dict[str, dict[str, str]] = {}
        for rec in rows:
            unique.setdefault(rec["Ticker"], rec)
        if len(unique) != first.total:
            raise FinvizParseError(
                f"got {len(unique)} unique tickers ({len(rows)} rows) but FinViz reported {first.total}"
            )
        if not unique and not allow_empty:
            raise FinvizParseError(f"FinViz returned an empty result for {filters!r}")
        for save in saves:
            save()
        return ScreenerPage(first.total, first.header, list(unique.values()))

    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage:
        """All pages merged and de-duplicated by ticker. Raises FinvizError unless the rows are
        exactly the count FinViz reports."""
        return self._screen(filters, view, signal)

    def universe(self, filters: str) -> list[UniverseRow]:
        """The nightly universe. An empty universe is an error, never a valid answer."""
        page = self._screen(filters, 111, None, required=UNIVERSE_COLUMNS, allow_empty=False)
        return [parse_universe_row(r) for r in page.rows]

    # --- news ---

    def news(self, ticker: str, today_et: date) -> list[Headline]:
        """Headlines from the quote page. `ticker` may be in Questrade form (BF.B). `today_et` must be
        today's ET date: it resolves FinViz's "Today" and is part of the cache key, so a page
        fetched on an earlier ET day is never reused."""

        def parse(html: str) -> list[Headline]:
            page = parse_news_page(html, today_et)
            if page.problem:
                raise FinvizParseError(f"quote page for {ticker}: {page.problem}")
            return page.headlines

        params = {"t": to_finviz_ticker(ticker)}
        headlines, save = self._fetch("/quote.ashx", params, parse, cache_tag=f"#today_et={today_et}")
        save()
        return headlines
