"""QUOTEBAR: the ~09:47 ET shadow check of the 9:35 opening bars built from live quotes (cron `trader
openbar-check`).

The official 09:30-09:35 FiveMinutes candle is published ~10 minutes late on Stephen's market-data package, so
by 09:47 it can be fetched. For a sample of at most MAX_SAMPLE symbols (every symbol that passed or entered
first, then the ranked candidates, then the largest opening volumes) the job fetches it (cached as an ordinary
candle: it IS official), compares it with the quote-built bar and stores the result on `opening_bar_quotes`:
the official OHLCV, `check_status` ("compared" or the missing reason) and `decision_differs` (whether the
bar-level ORB screen, rvol and candle shape and price band, decides otherwise on the official bar). The job
detail and the post-close summary line carry the totals. When nothing could be compared and every request
was refused with HTTP 401 (the candle is not published yet), the job raises OpenbarNotReady so its retries
run again later (nothing is written).
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

import structlog
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import MISSING_PREFIX, reason_key
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.indicators import is_bearish, is_doji, rvol
from trader.market.quote_bars import compare_bars
from trader.market.types import Candle, OpeningBars
from trader.notify.types import QuoteBarsLineView
from trader.strategies.orb_sip import OrbSipParams

log = structlog.get_logger("jobs.openbar_check")

JOB = "openbar_check"
MAX_SAMPLE = 100
COMPARED = "compared"
OPENING_BAR = timedelta(minutes=5)
MAX_DIFFERING_SHOWN = 20
NOT_READY_REASON = MISSING_PREFIX + "HTTP 401"


class OpenbarNotReady(RuntimeError):
    """Every official candle was refused (HTTP 401): not published yet. The job fails so its retry runs."""


class OfficialBars(Protocol):
    """A candles-mode MarketDataService: cached official candles first, Questrade for the rest."""

    async def opening_bars(
        self, session_date: date, symbol_ids: Sequence[int] | None = None
    ) -> OpeningBars: ...


@dataclass(frozen=True)
class OpenbarCheckDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    data: OfficialBars
    run_id: int | None  # the live run whose candidates and positions go first in the sample
    params: Callable[[], OrbSipParams]  # orb_sip's current parameters, for `decision_differs`


def sample_ids(
    factory: sessionmaker[Session], run_id: int | None, session_date: date, limit: int = MAX_SAMPLE
) -> list[int]:
    """At most `limit` symbols with a quote-built bar: passed candidates, entered positions, the other ranked
    candidates (by rank), then the rest by candle-scale opening volume (largest first)."""
    with factory() as s:
        quoted = (
            s.execute(
                select(m.OpeningBarQuote.symbol_id)
                .where(m.OpeningBarQuote.session_date == session_date)
                .order_by(m.OpeningBarQuote.volume.desc(), m.OpeningBarQuote.symbol_id)
            )
            .scalars()
            .all()
        )
        passed: list[int] = []
        ranked: list[int] = []
        entered: list[int] = []
        if run_id is not None:
            cands = s.execute(
                select(m.Candidate.symbol_id, m.Candidate.passed)
                .where(
                    m.Candidate.run_id == run_id,
                    m.Candidate.session_date == session_date,
                    m.Candidate.strategy_key == "orb_sip",
                )
                .order_by(m.Candidate.rank.is_(None), m.Candidate.rank, m.Candidate.id)
            ).all()
            passed = [sid for sid, ok in cands if ok]
            ranked = [sid for sid, ok in cands if not ok]
            entered = list(
                s.execute(
                    select(m.Position.symbol_id)
                    .where(m.Position.run_id == run_id, m.Position.session_date == session_date)
                    .order_by(m.Position.id)
                ).scalars()
            )
    have = set(quoted)
    ordered = [sid for sid in dict.fromkeys([*passed, *entered, *ranked, *quoted]) if sid in have]
    return ordered[:limit]


def _passes(bar: Candle, avg_open_vol: Decimal | None, p: OrbSipParams) -> bool:
    """orb_sip's bar-level screen: rvol >= rvol_min, not bearish, not a doji, close in the price band."""
    r = rvol(bar.volume, avg_open_vol)
    if r is None or r < p.rvol_min:
        return False
    try:
        if is_bearish(bar) or is_doji(bar, p.doji_body_pct_max):
            return False
    except ValueError:
        return False
    return p.price_min <= bar.close <= p.price_max


async def run_openbar_check(deps: OpenbarCheckDeps, session_date: date) -> dict[str, Any]:
    ids = sample_ids(deps.factory, deps.run_id, session_date)
    if not ids:
        return {"session_date": session_date.isoformat(), "skipped": "no quote-built opening bars"}
    official = await deps.data.opening_bars(session_date, ids)
    open_ = deps.calendar.session_open(session_date)
    try:
        params = deps.params()
    except Exception as exc:  # the comparison still runs; only decision_differs uses the parameters
        log.warning("openbar_check.params_unavailable", error=type(exc).__name__)
        params = OrbSipParams()
    with deps.factory() as s:
        rows = {
            r.symbol_id: r
            for r in s.execute(
                select(m.OpeningBarQuote).where(
                    m.OpeningBarQuote.session_date == session_date, m.OpeningBarQuote.symbol_id.in_(ids)
                )
            ).scalars()
        }
        stats: dict[int, Decimal | None] = {
            int(sid): avg
            for sid, avg in s.execute(
                select(m.OpenBarStat.symbol_id, m.OpenBarStat.avg_open_vol_14d).where(
                    m.OpenBarStat.session_date == session_date, m.OpenBarStat.symbol_id.in_(ids)
                )
            ).all()
        }
        tickers: dict[int, str] = {
            int(sid): str(t)
            for sid, t in s.execute(select(m.Symbol.id, m.Symbol.ticker).where(m.Symbol.id.in_(ids))).all()
        }
    missing: dict[int, str] = {}
    updates: list[dict[str, Any]] = []
    exact = within = differs = 0
    differing: list[str] = []
    errors: list[Decimal] = []
    now = deps.clock.now()
    for sid in ids:
        row = rows.get(sid)
        if row is None:
            continue
        bar = official.bars.get(sid)
        if bar is None or bar.start != open_:
            missing[sid] = official.missing.get(sid, "no_bar_at_open")
            updates.append({"symbol_id": sid, "checked_at": now, "check_status": missing[sid][:200]})
            continue
        quote = Candle(open_, open_ + OPENING_BAR, row.open, row.high, row.low, row.close, row.volume, None)
        cmp = compare_bars(quote, bar)
        avg = stats.get(sid)
        differ = _passes(quote, avg, params) != _passes(bar, avg, params)
        exact += cmp.prices_exact
        within += cmp.volume_within
        differs += differ
        if cmp.volume_error is not None:
            errors.append(cmp.volume_error)
        if differ:
            differing.append(tickers.get(sid, str(sid)))
        updates.append(
            {
                "symbol_id": sid,
                "checked_at": now,
                "check_status": COMPARED,
                "official_open": bar.open,
                "official_high": bar.high,
                "official_low": bar.low,
                "official_close": bar.close,
                "official_volume": bar.volume,
                "decision_differs": differ,
            }
        )
    compared = len(ids) - len(missing)
    if compared == 0 and missing and all(reason_key(r) == NOT_READY_REASON for r in missing.values()):
        raise OpenbarNotReady(
            f"{len(missing)} of {len(ids)} official opening candles refused (HTTP 401): not published yet"
        )
    if updates:
        with session_scope(deps.factory) as s:
            for u in updates:
                sid = u.pop("symbol_id")
                s.execute(
                    update(m.OpeningBarQuote)
                    .where(m.OpeningBarQuote.session_date == session_date, m.OpeningBarQuote.symbol_id == sid)
                    .values(**u)
                )
    errors.sort()
    detail: dict[str, Any] = {
        "session_date": session_date.isoformat(),
        "source": "quotes",
        "sample": len(ids),
        "compared": compared,
        "prices_exact": exact,
        "volume_within_10pct": within,
        "median_volume_error": str(errors[len(errors) // 2]) if errors else None,
        "decision_differs": differs,
        "differing": sorted(differing)[:MAX_DIFFERING_SHOWN],
        "missing": len(missing),
        "missing_reasons": dict(Counter(reason_key(r) for r in missing.values())),
    }
    log.info("openbar_check.done", **detail)
    return detail


def quote_bars_line(factory: sessionmaker[Session], session_date: date) -> QuoteBarsLineView | None:
    """The post-close line's counts for the session; None when it has no quote-built bar."""
    compared = m.OpeningBarQuote.check_status == COMPARED
    exact = (
        compared
        & (m.OpeningBarQuote.open == m.OpeningBarQuote.official_open)
        & (m.OpeningBarQuote.high == m.OpeningBarQuote.official_high)
        & (m.OpeningBarQuote.low == m.OpeningBarQuote.official_low)
    )
    within = (
        compared
        & (m.OpeningBarQuote.official_volume > 0)
        & (
            func.abs(m.OpeningBarQuote.volume - m.OpeningBarQuote.official_volume)
            <= m.OpeningBarQuote.official_volume * Decimal("0.10")
        )
    )

    def count_if(cond: Any) -> Any:
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    with factory() as s:
        total, n_compared, n_exact, n_within, n_differs = s.execute(
            select(
                func.count(),
                count_if(compared),
                count_if(exact),
                count_if(within),
                count_if(compared & m.OpeningBarQuote.decision_differs.is_(True)),
            ).where(m.OpeningBarQuote.session_date == session_date)
        ).one()
    if not total:
        return None
    return QuoteBarsLineView(
        quote_bars=int(total),
        compared=int(n_compared),
        prices_exact=int(n_exact),
        volume_within=int(n_within),
        decision_differs=int(n_differs),
    )
