"""GET /api/candidates, /api/orders, /api/fills, /api/positions, /api/positions/{id}, /api/trades (SPEC §11,
§12: the Candidates and Trades pages). BR-50, BR-51, BR-13 and O2 (the signal -> proposal -> decision ->
order -> fill chain of a position), BR-33 (unprotected time).

Reads only, for the live run (`/trades` takes `run`); dates default to the current session. Open positions
come from `trader.notify.views.position_lines`, the builder behind Telegram's `/positions`, so the web and
Telegram show the same numbers. A symbol missing from the database shows ticker `?`.

Routes that need quotes or candles are `async` and run their database work in a worker thread; the quote
fetch itself runs on the event loop (`anyio.from_thread.run`), so the shared `CachedQuotes` stays on one
loop.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

import anyio
import anyio.from_thread
import anyio.to_thread
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from trader.api.deps import ApiServices, Services, current_user, live_run_id, resolve_run
from trader.api.errors import ApiError
from trader.api.schemas import (
    CandidateOut,
    CandidatesOut,
    CandleOut,
    CatalystOut,
    FillOut,
    HeadlineOut,
    Items,
    OrderOut,
    PositionDetailOut,
    PositionOut,
    ProposalOut,
    SignalOut,
    TradeOut,
)
from trader.db import models as m
from trader.market.clock import ET
from trader.market.sessions import current_session
from trader.notify import views
from trader.notify.types import PositionLine

log = structlog.get_logger("api.trading")

# Every route needs a signed-in session (SPEC §14).
router = APIRouter(tags=["trading"], dependencies=[Depends(current_user)])

UNKNOWN_TICKER = "?"
CANDLE_INTERVAL = "5m"
Limit = Annotated[int, Query(ge=1, le=500)]
RunId = Annotated[int, Depends(resolve_run)]


def today(services: ApiServices) -> date:
    """The current session (today's ET date on a session day, else the next session)."""
    core = services.core
    return current_session(core.calendar, core.clock.now())


# --- row -> response mappings (shared with the dashboard) ---------------------------------------------------


def tickers(s: Session, symbol_ids: Iterable[int]) -> dict[int, str]:
    ids = sorted(set(symbol_ids))
    if not ids:
        return {}
    return dict(s.execute(select(m.Symbol.id, m.Symbol.ticker).where(m.Symbol.id.in_(ids))).tuples().all())


def strategy_keys(s: Session, config_ids: Iterable[int | None]) -> dict[int, str]:
    ids = sorted({i for i in config_ids if i is not None})
    if not ids:
        return {}
    return dict(
        s.execute(
            select(m.StrategyConfig.id, m.StrategyConfig.strategy_key).where(m.StrategyConfig.id.in_(ids))
        )
        .tuples()
        .all()
    )


def candidate_out(c: m.Candidate, ticker: str | None) -> CandidateOut:
    return CandidateOut(
        id=c.id,
        session_date=c.session_date,
        strategy_key=c.strategy_key,
        symbol_id=c.symbol_id,
        ticker=ticker or UNKNOWN_TICKER,
        rvol=c.rvol,
        rank=c.rank,
        passed=c.passed,
        reject_reason=c.reject_reason,
        candle=c.candle if isinstance(c.candle, dict) else None,
        data=c.data if isinstance(c.data, dict) else None,
    )


def order_out(o: m.Order, ticker: str | None) -> OrderOut:
    return OrderOut(
        id=o.id,
        proposal_id=o.proposal_id,
        position_id=o.position_id,
        symbol_id=o.symbol_id,
        ticker=ticker or UNKNOWN_TICKER,
        side=o.side,
        order_type=o.order_type,
        purpose=o.purpose,
        qty=o.qty,
        stop_price=o.stop_price,
        limit_price=o.limit_price,
        stop_loss=o.stop_loss,
        tif=o.tif,
        status=o.status,
        reason=o.reason,
        session_date=o.session_date,
        submitted_at=o.submitted_at,
        closed_at=o.closed_at,
        cancel_reason=o.cancel_reason,
    )


def fill_out(f: m.Fill, o: m.Order, ticker: str | None) -> FillOut:
    return FillOut(
        id=f.id,
        order_id=f.order_id,
        ticker=ticker or UNKNOWN_TICKER,
        side=o.side,
        purpose=o.purpose,
        ts=f.ts,
        qty=f.qty,
        price=f.price,
        fees=f.fees if isinstance(f.fees, dict) else {},
        quote_snapshot=f.quote_snapshot if isinstance(f.quote_snapshot, dict) else {},
        slippage=f.slippage,
    )


def trade_out(t: m.Trade, ticker: str | None, strategy_key: str | None) -> TradeOut:
    return TradeOut(
        id=t.id,
        position_id=t.position_id,
        symbol_id=t.symbol_id,
        ticker=ticker or UNKNOWN_TICKER,
        strategy_key=strategy_key or "",
        session_date=t.session_date,
        entry_price=t.entry_price,
        exit_price=t.exit_price,
        qty=t.qty,
        pnl=t.pnl,
        pnl_r=t.pnl_r,
        planned_risk=t.planned_risk,
        exit_reason=t.exit_reason,
        slippage_total=t.slippage_total,
        fees_total=t.fees_total,
        opened_at=t.opened_at,
        closed_at=t.closed_at,
    )


def stored_position_out(p: m.Position, ticker: str | None, strategy_key: str | None) -> PositionOut:
    """A position from its row alone (no quote): a closed one, or an open one Telegram's builder did not
    return. The stop shown is the position's stop loss."""
    return PositionOut(
        id=p.id,
        symbol_id=p.symbol_id,
        ticker=ticker or UNKNOWN_TICKER,
        strategy_key=strategy_key or "",
        status="closed" if p.closed_at is not None else "open",
        qty=p.qty,
        entry=p.avg_price,
        last=None,
        stop=p.stop_loss,
        stop_working=False,
        unrealized_pnl=None,
        unprotected_seconds=p.unprotected_seconds,
        opened_at=p.opened_at,
        closed_at=p.closed_at,
    )


# --- open positions (the Telegram builder) ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenPositions:
    """The run's open positions as `/positions` shows them, and the lines they came from (for the P&L)."""

    lines: tuple[PositionLine, ...]
    positions: tuple[PositionOut, ...]


def _open_positions_blocking(services: ApiServices, run_id: int) -> OpenPositions:
    core = services.core
    lines = anyio.from_thread.run(views.position_lines, core.factory, core.clock, run_id, services.quotes)
    with core.factory() as s:
        rows = {
            p.id: p
            for p in s.execute(
                select(m.Position).where(m.Position.id.in_([ln.position_id for ln in lines]))
            ).scalars()
        }
        keys = strategy_keys(s, (p.strategy_config_id for p in rows.values()))
    out = []
    for ln in lines:
        p = rows.get(ln.position_id)
        if p is None:  # deleted in between: never a 500
            continue
        key = keys.get(p.strategy_config_id, "") if p.strategy_config_id is not None else ""
        out.append(PositionOut.from_line(ln, symbol_id=p.symbol_id, strategy_key=key, opened_at=p.opened_at))
    return OpenPositions(lines, tuple(out))


async def open_positions(services: ApiServices, run_id: int) -> OpenPositions:
    """Open positions of the run with their last prices (quotes may be missing or fail: `last` null)."""
    return await anyio.to_thread.run_sync(_open_positions_blocking, services, run_id)


# --- candidates ---------------------------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def headlines_out(stored: Any) -> list[HeadlineOut]:
    """The catalyst's stored headlines (`[{ts, title, source, url}]`); malformed items are skipped."""
    out: list[HeadlineOut] = []
    for h in stored if isinstance(stored, list) else []:
        if not isinstance(h, Mapping) or not isinstance(h.get("title"), str):
            continue
        source, url = h.get("source"), h.get("url")
        out.append(
            HeadlineOut(
                ts=_parse_ts(h.get("ts")),
                title=h["title"],
                source=source if isinstance(source, str) else None,
                url=url if isinstance(url, str) else None,
            )
        )
    return out


def premarket_brief(s: Session, session_date: date) -> str | None:
    """The newest `premarket` run's brief for the date, if any."""
    details = s.execute(
        select(m.JobRun.detail)
        .where(m.JobRun.job == "premarket", m.JobRun.session_date == session_date)
        .order_by(m.JobRun.started_at.desc(), m.JobRun.id.desc())
    ).scalars()
    for detail in details:
        if isinstance(detail, dict) and isinstance(detail.get("brief"), str):
            return str(detail["brief"])
    return None


@router.get("/candidates", response_model=CandidatesOut)
def get_candidates(
    services: Services, date_: Annotated[date | None, Query(alias="date")] = None
) -> CandidatesOut:
    run_id = live_run_id(services)
    day = date_ or today(services)
    with services.core.factory() as s:
        ranking = s.execute(
            select(m.Candidate, m.Symbol.ticker)
            .outerjoin(m.Symbol, m.Symbol.id == m.Candidate.symbol_id)
            .where(m.Candidate.run_id == run_id, m.Candidate.session_date == day)
            .order_by(m.Candidate.rank.asc().nulls_last(), m.Candidate.id)
        ).all()
        catalysts = s.execute(
            select(m.Catalyst, m.Symbol.ticker)
            .outerjoin(m.Symbol, m.Symbol.id == m.Catalyst.symbol_id)
            .where(m.Catalyst.session_date == day)
            .order_by(m.Catalyst.quality.desc().nulls_last(), m.Catalyst.id)
        ).all()
        brief = premarket_brief(s, day)
    return CandidatesOut(
        session_date=day,
        brief=brief,
        catalysts=[
            CatalystOut(
                symbol_id=c.symbol_id,
                ticker=ticker or UNKNOWN_TICKER,
                session_date=c.session_date,
                catalyst_type=c.catalyst_type,
                direction=c.direction,
                quality=c.quality,
                confirmed=c.confirmed,
                reason=c.reason,
                gap_pct=c.gap_pct,
                earnings_date=c.earnings_date,
                headlines=headlines_out(c.headlines),
                model=c.model,
                classified_at=c.classified_at,
            )
            for c, ticker in catalysts
        ],
        ranking=[candidate_out(c, ticker) for c, ticker in ranking],
    )


# --- orders and fills ---------------------------------------------------------------------------------------


@router.get("/orders", response_model=Items[OrderOut])
def get_orders(
    services: Services,
    date_: Annotated[date | None, Query(alias="date")] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    limit: Limit = 200,
) -> Items[OrderOut]:
    """The session's orders, newest first; `status` is working, filled or cancelled."""
    run_id = live_run_id(services)
    day = date_ or today(services)
    q = (
        select(m.Order, m.Symbol.ticker)
        .outerjoin(m.Symbol, m.Symbol.id == m.Order.symbol_id)
        .where(m.Order.run_id == run_id, m.Order.session_date == day)
    )
    if status:
        q = q.where(m.Order.status == status)
    with services.core.factory() as s:
        rows = s.execute(q.order_by(m.Order.submitted_at.desc(), m.Order.id.desc()).limit(limit)).all()
        return Items[OrderOut](items=[order_out(o, ticker) for o, ticker in rows])


@router.get("/fills", response_model=Items[FillOut])
def get_fills(
    services: Services,
    date_: Annotated[date | None, Query(alias="date")] = None,
    limit: Limit = 200,
) -> Items[FillOut]:
    """The session's fills (by their order's session), newest first."""
    run_id = live_run_id(services)
    day = date_ or today(services)
    with services.core.factory() as s:
        rows = s.execute(
            select(m.Fill, m.Order, m.Symbol.ticker)
            .join(m.Order, m.Order.id == m.Fill.order_id)
            .outerjoin(m.Symbol, m.Symbol.id == m.Order.symbol_id)
            .where(m.Fill.run_id == run_id, m.Order.session_date == day)
            .order_by(m.Fill.ts.desc(), m.Fill.id.desc())
            .limit(limit)
        ).all()
        return Items[FillOut](items=[fill_out(f, o, ticker) for f, o, ticker in rows])


# --- positions ----------------------------------------------------------------------------------------------


def _positions_blocking(
    services: ApiServices, status: str, day: date | None
) -> tuple[int, list[tuple[m.Position, str | None, str | None]]]:
    run_id = live_run_id(services)
    q = (
        select(m.Position, m.Symbol.ticker, m.StrategyConfig.strategy_key)
        .outerjoin(m.Symbol, m.Symbol.id == m.Position.symbol_id)
        .outerjoin(m.StrategyConfig, m.StrategyConfig.id == m.Position.strategy_config_id)
        .where(m.Position.run_id == run_id)
    )
    if status == "open":
        q = q.where(m.Position.closed_at.is_(None))
    elif status == "closed":
        q = q.where(m.Position.closed_at.is_not(None))
    if day is not None:
        q = q.where(m.Position.session_date == day)
    with services.core.factory() as s:
        rows = [(p, t, k) for p, t, k in s.execute(q.order_by(m.Position.opened_at, m.Position.id)).all()]
    return run_id, rows


@router.get("/positions", response_model=Items[PositionOut])
async def get_positions(
    services: Services,
    status: Literal["open", "closed", "all"] = "open",
    date_: Annotated[date | None, Query(alias="date")] = None,
) -> Items[PositionOut]:
    """Positions of the live run, oldest first. `date` defaults to the current session, except that the
    open positions (`status=open`) are all listed when no date is given. Open ones carry the last price."""
    day = date_ if date_ is not None or status == "open" else today(services)
    run_id, rows = await anyio.to_thread.run_sync(_positions_blocking, services, status, day)
    live: dict[int, PositionOut] = {}
    if any(p.closed_at is None for p, _, _ in rows):
        live = {p.id: p for p in (await open_positions(services, run_id)).positions}
    return Items[PositionOut](
        items=[live.get(p.id) or stored_position_out(p, ticker, key) for p, ticker, key in rows]
    )


@dataclass(frozen=True, slots=True)
class _Detail:
    position: m.Position
    ticker: str | None
    strategy_key: str | None
    trade: TradeOut | None
    signal: SignalOut | None
    proposals: list[ProposalOut]
    orders: list[OrderOut]
    fills: list[FillOut]
    candles: list[CandleOut]
    window: tuple[datetime, datetime]
    run_id: int


def session_window(services: ApiServices, day: date) -> tuple[datetime, datetime]:
    """The regular session's open and close (09:30-16:00 ET when the date is not a session)."""
    cal = services.core.calendar
    if cal.is_session(day):
        return cal.session_open(day), cal.session_close(day)
    return (
        datetime.combine(day, time(9, 30), tzinfo=ET).astimezone(UTC),
        datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(UTC),
    )


def _stored_candles(s: Session, symbol_id: int, start: datetime, end: datetime) -> list[CandleOut]:
    """5-minute candles of the window from `intraday_candles`, else from `candle_archive`."""
    intraday = s.execute(
        select(m.IntradayCandle)
        .where(
            m.IntradayCandle.symbol_id == symbol_id,
            m.IntradayCandle.interval == CANDLE_INTERVAL,
            m.IntradayCandle.ts >= start,
            m.IntradayCandle.ts < end,
        )
        .order_by(m.IntradayCandle.ts)
    ).scalars()
    out = [
        CandleOut(start=c.ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
        for c in intraday
    ]
    if out:
        return out
    archived = s.execute(
        select(m.CandleArchive)
        .where(
            m.CandleArchive.symbol_id == symbol_id,
            m.CandleArchive.interval == CANDLE_INTERVAL,
            m.CandleArchive.start_ts >= start,
            m.CandleArchive.start_ts < end,
        )
        .order_by(m.CandleArchive.start_ts)
    ).scalars()
    return [
        CandleOut(start=c.start_ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
        for c in archived
    ]


def _detail_blocking(services: ApiServices, position_id: int) -> _Detail:
    run_id = live_run_id(services)
    with services.core.factory() as s:
        pos = s.get(m.Position, position_id)
        if pos is None or pos.run_id != run_id:
            raise ApiError(404, "not_found", "Unknown position")
        ticker = tickers(s, [pos.symbol_id]).get(pos.symbol_id)
        key = strategy_keys(s, [pos.strategy_config_id]).get(pos.strategy_config_id or 0)
        trade_row = s.execute(
            select(m.Trade).where(m.Trade.run_id == run_id, m.Trade.position_id == pos.id)
        ).scalar_one_or_none()
        orders = list(
            s.execute(
                select(m.Order)
                .where(
                    m.Order.run_id == run_id,
                    or_(m.Order.position_id == pos.id, m.Order.id == pos.entry_order_id),
                )
                .order_by(m.Order.submitted_at, m.Order.id)
            ).scalars()
        )
        order_tickers = tickers(s, (o.symbol_id for o in orders))
        by_id = {o.id: o for o in orders}
        fills = list(
            s.execute(
                select(m.Fill).where(m.Fill.order_id.in_(list(by_id) or [0])).order_by(m.Fill.ts, m.Fill.id)
            ).scalars()
        )
        proposals = list(
            s.execute(
                select(m.Proposal)
                .where(
                    m.Proposal.run_id == run_id,
                    or_(m.Proposal.position_id == pos.id, m.Proposal.order_id == pos.entry_order_id),
                )
                .order_by(m.Proposal.created_at, m.Proposal.id)
            ).scalars()
        )
        signal: SignalOut | None = None
        entry = by_id.get(pos.entry_order_id)
        entry_proposal = s.get(m.Proposal, entry.proposal_id) if entry and entry.proposal_id else None
        sig = s.get(m.Signal, entry_proposal.signal_id) if entry_proposal is not None else None
        if sig is not None:
            cfg = s.get(m.StrategyConfig, sig.strategy_config_id)
            signal = SignalOut(
                id=sig.id,
                strategy_key=cfg.strategy_key if cfg is not None else "",
                config_revision=cfg.revision if cfg is not None else 0,
                config_version=cfg.version if cfg is not None else "",
                event_key=sig.event_key,
                ts=sig.ts,
                intent=sig.intent if isinstance(sig.intent, dict) else {},
                evidence=sig.evidence if isinstance(sig.evidence, dict) else {},
            )
        window = session_window(services, pos.session_date)
        detail = _Detail(
            position=pos,
            ticker=ticker,
            strategy_key=key,
            trade=trade_out(trade_row, ticker, key) if trade_row is not None else None,
            signal=signal,
            proposals=[ProposalOut.from_view(views.proposal_view(s, p), p) for p in proposals],
            orders=[order_out(o, order_tickers.get(o.symbol_id)) for o in orders],
            fills=[
                fill_out(f, by_id[f.order_id], order_tickers.get(by_id[f.order_id].symbol_id)) for f in fills
            ],
            candles=_stored_candles(s, pos.symbol_id, *window),
            window=window,
            run_id=run_id,
        )
    return detail


@router.get("/positions/{position_id}", response_model=PositionDetailOut)
async def get_position(services: Services, position_id: int) -> PositionDetailOut:
    """One position of the live run with its whole chain and the session's 5-minute chart."""
    d = await anyio.to_thread.run_sync(_detail_blocking, services, position_id)
    position = stored_position_out(d.position, d.ticker, d.strategy_key)
    if d.position.closed_at is None:
        for p in (await open_positions(services, d.run_id)).positions:
            if p.id == d.position.id:
                position = p
    candles, chart_error = d.candles, None
    if not candles and services.candles is not None:
        try:
            got = await services.candles(d.position.symbol_id, *d.window)
            candles = [
                CandleOut(start=c.start, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume)
                for c in got
            ]
        except Exception as exc:  # the chart is optional: show why, never fail the page
            chart_error = type(exc).__name__
            log.warning("api.position_candles_failed", position_id=d.position.id, error_type=chart_error)
    return PositionDetailOut(
        position=position,
        trade=d.trade,
        signal=d.signal,
        proposals=d.proposals,
        orders=d.orders,
        fills=d.fills,
        candles=candles,
        chart_error=chart_error,
    )


# --- trades -------------------------------------------------------------------------------------------------


@router.get("/trades", response_model=Items[TradeOut])
def get_trades(
    services: Services,
    run_id: RunId,
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: date | None = None,
    limit: Limit = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Items[TradeOut]:
    """Closed trades of the run (`live` or a run id), newest first, filtered by session date."""
    q = (
        select(m.Trade, m.Symbol.ticker, m.StrategyConfig.strategy_key)
        .outerjoin(m.Symbol, m.Symbol.id == m.Trade.symbol_id)
        .outerjoin(m.Position, m.Position.id == m.Trade.position_id)
        .outerjoin(m.StrategyConfig, m.StrategyConfig.id == m.Position.strategy_config_id)
        .where(m.Trade.run_id == run_id)
    )
    if from_ is not None:
        q = q.where(m.Trade.session_date >= from_)
    if to is not None:
        q = q.where(m.Trade.session_date <= to)
    q = q.order_by(m.Trade.session_date.desc(), m.Trade.closed_at.desc(), m.Trade.id.desc())
    with services.core.factory() as s:
        rows = s.execute(q.limit(limit).offset(offset)).all()
        return Items[TradeOut](items=[trade_out(t, ticker, key) for t, ticker, key in rows])
