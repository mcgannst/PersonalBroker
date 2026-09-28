"""Pre-market scan (SPEC §4.2, §4.3, §9 at 08:00 ET; BR-03, BR-05).

Candidates: universe names on the FinViz news or earnings screens, or gapping at least premarket.gap_min_pct
on Questrade's pre-market quotes. Each screen setting may hold several "|"-separated filter lists, one screen
each, unioned. The default earnings setting is the session window (Stephen, 2026-09-27): reported after the
previous TRADING session's close (or on a weekend/holiday since) OR before today's open, computed from the
exchange calendar. FinViz's "yesterday" is the previous calendar day, so the window reads FinViz's Earnings
column ("Sep 25/a" after the close, "/b" before the open) on two screens, earningsdate_prevdays5 and
earningsdate_today, and keeps rows by date and mark. An unreadable Earnings value, a row outside the filter's
own date range, and an empty catalyst screen that is empty market-wide too are reported in screen_errors;
the detail's "screens" lists every screen that ran. The biggest movers (by |gap|, up to
claude.premarket_max_candidates) get headlines and a Claude classification; the rest are stored as
"not classified (over cap)".

Nothing external can sink the scan on its own:
- a failed FinViz screen is reported in the brief and the gaps still count. FinViz's verified "0 Total"
  page is an empty screen, not a failure. A screen whose count equals the universe filters' own count (or
  the universe size, for a screener that can't count) means FinViz ignored the extra filter: a failure.
- failed Questrade quotes or prior closes leave every gap unknown ("gap n/a"); the screens still count.
- a quote whose last trade is older than the prior session's close is stale: its gap is unknown.

The brief goes to Telegram (P3), so every interpolated text is collapsed to one line and error texts are
capped at MAX_ERROR_CHARS: an upstream error body can't add or forge lines.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.catalyst import (
    BUDGET_EXCEEDED,
    OVER_CAP,
    CatalystRequest,
    CatalystService,
    StoredCatalyst,
)
from trader.adapters.finviz.parser import (
    EARNINGS_COLUMN,
    EARNINGS_COLUMNS,
    EARNINGS_VIEW,
    EarningsTime,
    ScreenerPage,
    parse_earnings,
    to_questrade_ticker,
)
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.models import QtQuote
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import UniverseMember
from trader.settings_store import (
    EARNINGS_SESSION_WINDOW,
    LEGACY_EARNINGS_FILTER,
    OVERLAY_SYMBOL,
    RuntimeSettings,
)

Q4 = Decimal("0.0001")
SOURCE = "job.premarket"
MAX_ERROR_CHARS = 200
MAX_LISTED = 10  # rows named in one screen error line
# The session window's FinViz filters (live 2026-09-27, see SPEC §4.2). earningsdate_prevdays5 is the 5
# calendar days up to today (on Sunday Sep 27 it returned Sep 23..25 reporters and nothing from Sep 22), so
# it reaches a previous session up to PREVDAYS5_REACH days back (Friday from a Tuesday after a holiday).
PREVDAYS5 = "earningsdate_prevdays5"
PREVDAYS5_REACH = 4
TODAY_EARNINGS = "earningsdate_today"


class PremarketScreens(Protocol):
    def screen(
        self, filters: str, view: int = 111, signal: str | None = None, *, columns: str | None = None
    ) -> ScreenerPage: ...


@runtime_checkable
class CountingScreens(Protocol):
    """A screener that can report a filter set's result count (the real FinvizScraper can)."""

    def count(self, filters: str, view: int = 111) -> int: ...


class PremarketData(Protocol):
    async def universe(self, session_date: date) -> list[UniverseMember]: ...
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]: ...
    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]: ...


@dataclass(frozen=True)
class PremarketDeps:
    factory: sessionmaker[Session]
    clock: Clock
    finviz: PremarketScreens
    data: PremarketData
    catalysts: CatalystService
    settings: RuntimeSettings
    calendar: SessionCalendar = field(default_factory=SessionCalendar)


@dataclass(frozen=True, slots=True)
class PremarketCandidate:
    symbol_id: int
    ticker: str
    company: str
    gap_pct: Decimal | None
    sources: tuple[str, ...]


def one_line(text: object, limit: int | None = None) -> str:
    """Collapse all whitespace (newlines included) to single spaces; cap at `limit` characters."""
    flat = " ".join(str(text).split())
    if limit is not None and len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


def _err(exc: BaseException) -> str:
    return one_line(f"{type(exc).__name__}: {exc}", MAX_ERROR_CHARS)


def _rank_key(c: PremarketCandidate) -> tuple[int, Decimal, str]:
    return (0, -abs(c.gap_pct), c.ticker) if c.gap_pct is not None else (1, Decimal(0), c.ticker)


def format_brief(
    session_date: date,
    top: Sequence[PremarketCandidate],
    catalysts: Mapping[int, StoredCatalyst],
    over: Sequence[PremarketCandidate],
    screen_errors: Sequence[str],
) -> str:
    """One line per candidate. Every interpolated text is collapsed to one line; errors are capped."""
    lines = [f"Pre-market brief for {session_date.isoformat()}: {len(top) + len(over)} candidates"]
    for c in top:
        gap = f"{c.gap_pct * 100:+.2f}%" if c.gap_pct is not None else "gap n/a"
        cat = catalysts.get(c.symbol_id)
        if cat is not None and cat.classified:
            desc = (
                f"{one_line(cat.catalyst_type)}, {one_line(cat.direction)}, quality {cat.quality}: "
                f"{one_line(cat.reason or '', MAX_ERROR_CHARS)}"
            )
        else:
            why = cat.reason if cat is not None and cat.reason else "not classified"
            desc = f"unknown ({one_line(why, MAX_ERROR_CHARS)})"
        sources = ", ".join(one_line(s) for s in c.sources)
        lines.append(f"{one_line(c.ticker)} {gap} [{sources}] {desc}")
    if over:
        lines.append("Not classified (over cap): " + ", ".join(one_line(c.ticker) for c in over))
    if screen_errors:
        lines.append(
            "FinViz screens failed: " + "; ".join(one_line(e, MAX_ERROR_CHARS) for e in screen_errors)
        )
    if not top and not over:
        lines.append("No gappers or news today.")
    return "\n".join(lines)


def brief_notes(notes: Sequence[str]) -> list[str]:
    """Extra brief lines (quote failure, budget, forced-run warning), each one capped line."""
    return [one_line(n, 2 * MAX_ERROR_CHARS) for n in notes if one_line(n)]


def _earnings_date(extra: str, session_date: date, calendar: SessionCalendar) -> date:
    """The report date an earnings screen implies: an `earningsdate_yesterday*` screen (reported after
    yesterday's close) means the previous session, any other the session itself."""
    if any(token.startswith("earningsdate_yesterday") for token in extra.split(",")):
        return calendar.previous_session(session_date)
    return session_date


@dataclass(frozen=True, slots=True)
class EarningsWindow:
    """Stephen's earnings window (2026-09-27): reported after the previous TRADING session's close, on a
    non-session day between it and today (a weekend or holiday), or before today's open."""

    previous_session: date
    session: date

    def accepts(self, reported: date, when: EarningsTime) -> bool:
        if reported == self.previous_session:
            return when == "a"
        if reported == self.session:
            return when == "b"
        return self.previous_session < reported < self.session


def earnings_window(session_date: date, calendar: SessionCalendar) -> EarningsWindow:
    return EarningsWindow(calendar.previous_session(session_date), session_date)


@dataclass(frozen=True, slots=True)
class _Screen:
    source: str  # "news" | "earnings"
    label: str
    extra: str  # the filter tokens added to the universe filters
    view: int = 111
    columns: str | None = None
    window: EarningsWindow | None = None  # set: rows are kept by their Earnings value (session window)
    expect: tuple[date, date] | None = None  # the report dates the filter itself should return
    problem: str | None = None  # a known gap in what this screen can see (reported; it still runs)


def _window_screens(session_date: date, calendar: SessionCalendar) -> list[_Screen]:
    """Two screens with FinViz's Earnings column, each row kept by EarningsWindow.accepts. FinViz's
    "yesterday" is the previous calendar day, so the previous session comes from `earningsdate_prevdays5`
    (the 5 calendar days up to today) and today from `earningsdate_today`."""
    w = earnings_window(session_date, calendar)
    back = (session_date - w.previous_session).days
    problem = None
    if back > PREVDAYS5_REACH:
        problem = (
            f"the previous session is {back} days back, beyond {PREVDAYS5!r} ({PREVDAYS5_REACH} days): "
            "its after-close reporters can't be screened"
        )
    return [
        _Screen(
            "earnings",
            f"earnings [after close {w.previous_session.isoformat()}]",
            PREVDAYS5,
            EARNINGS_VIEW,
            EARNINGS_COLUMNS,
            w,
            (session_date - timedelta(days=PREVDAYS5_REACH), session_date),
            problem,
        ),
        _Screen(
            "earnings",
            f"earnings [before open {session_date.isoformat()}]",
            TODAY_EARNINGS,
            EARNINGS_VIEW,
            EARNINGS_COLUMNS,
            w,
            (session_date, session_date),
        ),
    ]


def _plan(s: RuntimeSettings, session_date: date, calendar: SessionCalendar) -> list[_Screen]:
    """One screen per "|"-separated filter list of each setting. The earnings session window (the default,
    and the retired first default LEGACY_EARNINGS_FILTER) is the two computed window screens instead."""
    plan: list[_Screen] = []
    for source, setting in (("news", s.premarket_news_filter), ("earnings", s.premarket_earnings_filter)):
        if source == "earnings" and setting in (EARNINGS_SESSION_WINDOW, LEGACY_EARNINGS_FILTER):
            plan.extend(_window_screens(session_date, calendar))
            continue
        alternatives = setting.split("|")
        for extra in alternatives:
            plan.append(_Screen(source, source if len(alternatives) == 1 else f"{source} [{extra}]", extra))
    return plan


def _listed(items: Sequence[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    return shown + (", …" if len(items) > MAX_LISTED else "")


@dataclass
class _ScreenResult:
    flagged: dict[str, set[str]] = field(default_factory=dict)  # ticker -> sources
    earnings_dates: dict[str, date] = field(default_factory=dict)  # ticker -> latest report date
    errors: list[str] = field(default_factory=list)
    screens: list[dict[str, Any]] = field(default_factory=list)  # per screen: label, filter, rows, matched


def _fetch(deps: PremarketDeps, sc: _Screen) -> ScreenerPage:
    filters = f"{deps.settings.universe_finviz_filters},{sc.extra}"
    if sc.columns is None:
        return deps.finviz.screen(filters)
    return deps.finviz.screen(filters, sc.view, columns=sc.columns)


def _window_rows(
    sc: _Screen, window: EarningsWindow, page: ScreenerPage, session_date: date, errors: list[str]
) -> list[tuple[str, date]] | None:
    """(FinViz ticker, report date) of the rows inside the window, or None when the page can't be read.
    Unreadable Earnings values and rows outside the filter's own date range are reported, never dropped
    silently."""
    if page.rows and EARNINGS_COLUMN not in page.header:
        errors.append(f"{sc.label}: FinViz page has no {EARNINGS_COLUMN!r} column (header {page.header})")
        return None
    kept: list[tuple[str, date]] = []
    unreadable: list[str] = []
    outside: list[str] = []
    for row in page.rows:
        ticker, value = row.get("Ticker", ""), row.get(EARNINGS_COLUMN, "")
        parsed = parse_earnings(value, session_date)
        if parsed is None:
            unreadable.append(f"{ticker} {value!r}")
            continue
        reported, when = parsed
        if sc.expect is not None and not sc.expect[0] <= reported <= sc.expect[1]:
            outside.append(f"{ticker} {value!r}")
        if window.accepts(reported, when):
            kept.append((ticker, reported))
    if unreadable:
        errors.append(
            f"{sc.label}: {len(unreadable)} rows with an unreadable {EARNINGS_COLUMN} value "
            f"({_listed(unreadable)})"
        )
    if outside and sc.expect is not None:
        lo, hi = sc.expect
        errors.append(
            f"{sc.label}: {len(outside)} rows dated outside {lo.isoformat()}..{hi.isoformat()} "
            f"({_listed(outside)}): FinViz's filter may have changed meaning"
        )
    return kept


async def _cross_check(deps: PremarketDeps, sc: _Screen) -> str | None:
    """An empty catalyst screen is suspect: count the same filter market-wide (no universe filters). Zero
    there too means the filter itself may be broken: a warning in screen_errors, not a failure."""
    if not isinstance(deps.finviz, CountingScreens):
        return None
    try:
        market = await asyncio.to_thread(deps.finviz.count, sc.extra)
    except FinvizError as exc:
        return f"{sc.label}: cross-check failed: {one_line(exc, MAX_ERROR_CHARS)}"
    if market == 0:
        return (
            f"{sc.label}: cross-check: FinViz lists no {sc.extra!r} matches market-wide either, "
            "so the filter may be broken"
        )
    return None


async def _screens(
    deps: PremarketDeps,
    universe: Sequence[UniverseMember],
    by_ticker: Mapping[str, UniverseMember],
    session_date: date,
) -> _ScreenResult:
    """Every planned screen, matches unioned. A failed screen is reported and the others still count."""
    out = _ScreenResult()
    baseline: int | None = None
    for sc in _plan(deps.settings, session_date, deps.calendar):
        record: dict[str, Any] = {"label": sc.label, "filter": sc.extra, "rows": None, "matched": []}
        out.screens.append(record)
        if sc.problem:
            out.errors.append(f"{sc.label}: {sc.problem}")
        try:
            page = await asyncio.to_thread(_fetch, deps, sc)
        except FinvizError as exc:
            out.errors.append(f"{sc.label}: {one_line(exc, MAX_ERROR_CHARS)}")
            continue
        total = page.total if page.total is not None else len(page.rows)
        record["rows"] = total
        if total > 0:
            if baseline is None:
                baseline = await _universe_count(deps, len(universe))
            if total == baseline:
                out.errors.append(
                    f"{sc.label}: filter {sc.extra!r} ignored (matched all {total} universe-filter names)"
                )
                continue
        else:
            warning = await _cross_check(deps, sc)
            if warning:
                out.errors.append(warning)
        rows: list[tuple[str, date]] | None
        if sc.window is None:
            reported = _earnings_date(sc.extra, session_date, deps.calendar)
            rows = [(row.get("Ticker", ""), reported) for row in page.rows]
        else:
            rows = _window_rows(sc, sc.window, page, session_date, out.errors)
            if rows is None:
                continue
        matched: set[str] = set()
        for raw, reported in rows:
            ticker = to_questrade_ticker(raw)
            if ticker not in by_ticker:
                continue
            matched.add(ticker)
            out.flagged.setdefault(ticker, set()).add(sc.source)
            if sc.source == "earnings":
                out.earnings_dates[ticker] = max(reported, out.earnings_dates.get(ticker, reported))
        record["matched"] = sorted(matched)
    return out


async def _universe_count(deps: PremarketDeps, universe_size: int) -> int:
    """What a screen returns when FinViz ignores its extra filter: the universe filters' own count, or
    the universe size for a screener that can't count (or when counting fails)."""
    if isinstance(deps.finviz, CountingScreens):
        try:
            return await asyncio.to_thread(deps.finviz.count, deps.settings.universe_finviz_filters)
        except FinvizError:
            pass
    return universe_size


async def _gaps(
    deps: PremarketDeps, universe: Sequence[UniverseMember], session_date: date
) -> tuple[dict[int, Decimal], str | None]:
    """Unrounded gaps by symbol id, and a note when Questrade failed (then no gaps at all)."""
    ids = [u.symbol_id for u in universe]
    try:
        quotes = await deps.data.quotes(ids)
        closes = await deps.data.prior_closes(ids, session_date)
    except Exception as exc:  # any quote failure: gaps unknown, the screens still count
        return {}, f"Questrade quotes failed: {_err(exc)}"
    prior_close_at = deps.calendar.session_close(deps.calendar.previous_session(session_date))
    gaps: dict[int, Decimal] = {}
    for u in universe:
        q, prev = quotes.get(u.symbol_id), closes.get(u.symbol_id)
        if q is None or q.last is None or q.last <= 0 or prev is None or prev <= 0:
            continue
        if q.last_trade_time is not None and q.last_trade_time < prior_close_at:
            continue  # stale: no trade since the prior session's close
        gaps[u.symbol_id] = (q.last - prev) / prev
    return gaps, None


async def run_premarket(
    deps: PremarketDeps, session_date: date, warnings: Sequence[str] = ()
) -> dict[str, Any]:
    """`warnings` (e.g. a forced run outside the pre-market window) are logged and added to the brief."""
    s = deps.settings
    universe = [u for u in await deps.data.universe(session_date) if u.ticker != OVERLAY_SYMBOL]
    if not universe:
        raise RuntimeError(f"no universe for {session_date}: the nightly job must run first")
    by_ticker = {u.ticker: u for u in universe}
    screened = await _screens(deps, universe, by_ticker, session_date)
    flagged, screen_errors = screened.flagged, screened.errors

    raw_gaps, quote_error = await _gaps(deps, universe, session_date)
    gaps: dict[int, Decimal] = {}
    for u in universe:
        raw = raw_gaps.get(u.symbol_id)
        if raw is None:
            continue
        gaps[u.symbol_id] = raw.quantize(Q4, ROUND_HALF_UP)  # rounded for storage and display only
        if abs(raw) >= s.premarket_gap_min_pct:
            flagged.setdefault(u.ticker, set()).add("gap")

    candidates = sorted(
        (
            PremarketCandidate(
                by_ticker[t].symbol_id,
                t,
                by_ticker[t].name or "",
                gaps.get(by_ticker[t].symbol_id),
                tuple(sorted(src)),
            )
            for t, src in flagged.items()
        ),
        key=_rank_key,
    )
    cap = s.claude_premarket_max_candidates
    top, over = candidates[:cap], candidates[cap:]

    def request(c: PremarketCandidate) -> CatalystRequest:
        return CatalystRequest(
            c.symbol_id, c.ticker, c.company, c.gap_pct, screened.earnings_dates.get(c.ticker)
        )

    catalysts = await deps.catalysts.classify_many([request(c) for c in top], session_date)
    deps.catalysts.mark_unclassified([request(c) for c in over], session_date, OVER_CAP)
    budget_hit = [
        c.ticker
        for c in top
        if (cat := catalysts.get(c.symbol_id)) is not None
        and not cat.classified
        and (cat.reason or "").startswith(BUDGET_EXCEEDED)
    ]
    notes = [*warnings]
    if quote_error:
        notes.append(quote_error)
    if budget_hit:
        notes.append(
            f"Claude daily budget reached: {len(budget_hit)} not classified ({', '.join(budget_hit)})"
        )
    brief = "\n".join([format_brief(session_date, top, catalysts, over, screen_errors), *brief_notes(notes)])
    detail: dict[str, Any] = {
        "session_date": session_date.isoformat(),
        "candidates": len(candidates),
        "classified": sum(1 for c in catalysts.values() if c.classified),
        "over_cap": [c.ticker for c in over],
        "screen_errors": screen_errors,
        "screens": screened.screens,
        "quote_error": quote_error,
        "budget_hit": budget_hit,
        "warnings": [one_line(w) for w in warnings],
        "brief": brief,
    }
    with session_scope(deps.factory) as sess:
        log_event(
            sess,
            deps.clock,
            "warning" if warnings or quote_error else "info",
            SOURCE,
            f"pre-market brief for {session_date}",
            {k: v for k, v in detail.items() if k != "brief"},
        )
    return detail
