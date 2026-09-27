"""Pre-market scan (SPEC §4.2, §4.3, §9 at 08:00 ET; BR-03, BR-05).

Candidates: universe names on the FinViz news or earnings screen, or gapping at least premarket.gap_min_pct
on Questrade's pre-market quotes. The biggest movers (by |gap|, up to claude.premarket_max_candidates) get
headlines and a Claude classification; the rest are stored as "not classified (over cap)".
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.claude.catalyst import OVER_CAP, CatalystRequest, CatalystService, StoredCatalyst
from trader.adapters.finviz.parser import ScreenerPage, to_questrade_ticker
from trader.adapters.finviz.scraper import FinvizError
from trader.adapters.questrade.models import QtQuote
from trader.db.session import session_scope
from trader.events import log_event
from trader.market.clock import Clock
from trader.market.types import UniverseMember
from trader.settings_store import OVERLAY_SYMBOL, RuntimeSettings

Q4 = Decimal("0.0001")
SOURCE = "job.premarket"


class PremarketScreens(Protocol):
    def screen(self, filters: str, view: int = 111, signal: str | None = None) -> ScreenerPage: ...


class PremarketData(Protocol):
    async def universe(self, session_date: date) -> list[UniverseMember]: ...
    async def quotes(self, symbol_ids: Sequence[int]) -> dict[int, QtQuote]: ...
    async def prior_closes(self, symbol_ids: Sequence[int], session_date: date) -> dict[int, Decimal]: ...


@dataclass
class PremarketDeps:
    factory: sessionmaker[Session]
    clock: Clock
    finviz: PremarketScreens
    data: PremarketData
    catalysts: CatalystService
    settings: RuntimeSettings


@dataclass(frozen=True, slots=True)
class PremarketCandidate:
    symbol_id: int
    ticker: str
    company: str
    gap_pct: Decimal | None
    sources: tuple[str, ...]


def _rank_key(c: PremarketCandidate) -> tuple[int, Decimal, str]:
    return (0, -abs(c.gap_pct), c.ticker) if c.gap_pct is not None else (1, Decimal(0), c.ticker)


def format_brief(
    session_date: date,
    top: Sequence[PremarketCandidate],
    catalysts: Mapping[int, StoredCatalyst],
    over: Sequence[PremarketCandidate],
    screen_errors: Sequence[str],
) -> str:
    lines = [f"Pre-market brief for {session_date.isoformat()}: {len(top) + len(over)} candidates"]
    for c in top:
        gap = f"{c.gap_pct * 100:+.2f}%" if c.gap_pct is not None else "gap n/a"
        cat = catalysts.get(c.symbol_id)
        if cat is not None and cat.classified:
            desc = f"{cat.catalyst_type}, {cat.direction}, quality {cat.quality}: {cat.reason}"
        else:
            desc = f"unknown ({cat.reason if cat is not None else 'not classified'})"
        lines.append(f"{c.ticker} {gap} [{', '.join(c.sources)}] {desc}")
    if over:
        lines.append("Not classified (over cap): " + ", ".join(c.ticker for c in over))
    if screen_errors:
        lines.append("FinViz screens failed: " + "; ".join(screen_errors))
    if not top and not over:
        lines.append("No gappers or news today.")
    return "\n".join(lines)


async def run_premarket(deps: PremarketDeps, session_date: date) -> dict[str, Any]:
    s = deps.settings
    universe = [u for u in await deps.data.universe(session_date) if u.ticker != OVERLAY_SYMBOL]
    if not universe:
        raise RuntimeError(f"no universe for {session_date}: the nightly job must run first")
    by_ticker = {u.ticker: u for u in universe}
    flagged: dict[str, set[str]] = {}
    screen_errors: list[str] = []
    for source, extra in (("news", s.premarket_news_filter), ("earnings", s.premarket_earnings_filter)):
        try:
            page = await asyncio.to_thread(deps.finviz.screen, f"{s.universe_finviz_filters},{extra}")
        except FinvizError as exc:
            screen_errors.append(f"{source}: {exc}")
            continue
        for row in page.rows:
            ticker = to_questrade_ticker(row.get("Ticker", ""))
            if ticker in by_ticker:
                flagged.setdefault(ticker, set()).add(source)

    ids = [u.symbol_id for u in universe]
    quotes = await deps.data.quotes(ids)
    closes = await deps.data.prior_closes(ids, session_date)
    gaps: dict[int, Decimal] = {}
    for u in universe:
        q, prev = quotes.get(u.symbol_id), closes.get(u.symbol_id)
        price = q.last if q is not None else None
        if price is not None and price > 0 and prev is not None and prev > 0:
            gaps[u.symbol_id] = ((price - prev) / prev).quantize(Q4, ROUND_HALF_UP)
            if abs(gaps[u.symbol_id]) >= s.premarket_gap_min_pct:
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
    brief = format_brief(session_date, top, catalysts, over, screen_errors)
    detail: dict[str, Any] = {
        "session_date": session_date.isoformat(),
        "candidates": len(candidates),
        "classified": sum(1 for c in catalysts.values() if c.classified),
        "over_cap": [c.ticker for c in over],
        "screen_errors": screen_errors,
        "brief": brief,
    }
    with session_scope(deps.factory) as sess:
        log_event(
            sess,
            deps.clock,
            "info",
            SOURCE,
            f"pre-market brief for {session_date}",
            {k: v for k, v in detail.items() if k != "brief"},
        )
    return detail
