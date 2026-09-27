"""Pre-market scan (SPEC §4.2, §4.3, §9 at 08:00 ET; BR-03, BR-05).

Candidates: universe names on the FinViz news or earnings screen, or gapping at least premarket.gap_min_pct
on Questrade's pre-market quotes. The biggest movers (by |gap|, up to claude.premarket_max_candidates) get
headlines and a Claude classification; the rest are stored as "not classified (over cap)".

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
from datetime import date
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
from trader.adapters.finviz.parser import ScreenerPage, to_questrade_ticker
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.models import QtQuote
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import UniverseMember
from trader.settings_store import OVERLAY_SYMBOL, RuntimeSettings

Q4 = Decimal("0.0001")
SOURCE = "job.premarket"
MAX_ERROR_CHARS = 200


class PremarketScreens(Protocol):
    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage: ...


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


async def _screens(
    deps: PremarketDeps, universe: Sequence[UniverseMember], by_ticker: Mapping[str, UniverseMember]
) -> tuple[dict[str, set[str]], list[str]]:
    s = deps.settings
    flagged: dict[str, set[str]] = {}
    errors: list[str] = []
    baseline: int | None = None
    for source, extra in (("news", s.premarket_news_filter), ("earnings", s.premarket_earnings_filter)):
        try:
            page = await asyncio.to_thread(deps.finviz.screen, f"{s.universe_finviz_filters},{extra}")
        except FinvizError as exc:
            errors.append(f"{source}: {one_line(exc, MAX_ERROR_CHARS)}")
            continue
        total = page.total if page.total is not None else len(page.rows)
        if total > 0:
            if baseline is None:
                baseline = await _universe_count(deps, len(universe))
            if total == baseline:
                errors.append(
                    f"{source}: filter {extra!r} ignored (matched all {total} universe-filter names)"
                )
                continue
        for row in page.rows:
            ticker = to_questrade_ticker(row.get("Ticker", ""))
            if ticker in by_ticker:
                flagged.setdefault(ticker, set()).add(source)
    return flagged, errors


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
    flagged, screen_errors = await _screens(deps, universe, by_ticker)

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
        earnings = session_date if "earnings" in c.sources else None
        return CatalystRequest(c.symbol_id, c.ticker, c.company, c.gap_pct, earnings)

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
