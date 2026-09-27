"""The 16:15 ET post-close job: end-of-day cancels (BR-42 safety net), the journal row, the candle archive
and the daily summary with the Rules-followed buttons (BR-60, BR-33; SPEC §8, §9).

Idempotent: a forced re-run upserts the archive by primary key, never overwrites a journal answer, and sends
its summary with the dedupe key `summary:<date>`, so the notifier sends it once. Missing market data is
listed in the result (and alerted when it matters), never a job failure (Review Focus 4).
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

import structlog
from sqlalchemy import column, func, select, table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.types import CallbackIssuer
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.events import log_event
from trader.logging_setup import redact_text
from trader.market import repository as repo
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, FixedClock
from trader.market.indicators import regular_hours
from trader.market.types import Candle, Interval, OpeningBars, UniverseMember
from trader.notify.types import (
    Button,
    Buttons,
    DailySummaryView,
    Notifier,
    PositionLine,
    Renderer,
    TradeLine,
)
from trader.settings_store import OVERLAY_SYMBOL, RuntimeSettings
from trader.worker import WorkerEngine

log = structlog.get_logger("jobs.postclose")

SOURCE = "job.postclose"
OPENING_BAR_CODE: Literal["5m"] = "5m"  # INTERVAL_CODES["FiveMinutes"]
MINUTE_CODE: Literal["1m"] = "1m"  # INTERVAL_CODES["OneMinute"]
MAX_MISSING_OPEN_FRACTION = Decimal("0.05")  # more opening bars missing than this is an error event
MAX_REASON_CHARS = 200
JOURNAL_ACTIONS = ("y", "n")
# The realized P&L view (migration 0002): one row per (run, session) with trades.
V_DAILY_PNL = table(
    "v_daily_pnl",
    column("run_id"),
    column("session_date"),
    column("realized_pnl"),
    column("fees"),
    schema=m.SCHEMA,
)


class ArchiveData(Protocol):
    """The market data the archive reads (P2's MarketDataService satisfies it)."""

    async def universe(self, session_date: date) -> list[UniverseMember]: ...

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]: ...

    async def opening_bars(
        self, session_date: date, symbol_ids: Sequence[int] | None = None
    ) -> OpeningBars: ...


@dataclass(frozen=True)
class PostcloseDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    engine: WorkerEngine
    data: ArchiveData
    notifier: Notifier
    render: Renderer
    issuer: CallbackIssuer
    chat_id: int
    run_id: int


def _reason(exc: BaseException) -> str:
    """An exception as one masked, capped line (no traceback, no repr)."""
    flat = " ".join(redact_text(f"{type(exc).__name__}: {exc}").split())
    return flat if len(flat) <= MAX_REASON_CHARS else flat[: MAX_REASON_CHARS - 1] + "…"


def _et_day(d: date) -> tuple[datetime, datetime]:
    """[00:00 ET of d, 00:00 ET of the next day)."""
    return datetime.combine(d, time(0), tzinfo=ET), datetime.combine(
        d + timedelta(days=1), time(0), tzinfo=ET
    )


async def run_postclose(deps: PostcloseDeps, session_date: date) -> dict[str, Any]:
    """Returns {"open_positions", "cancelled", "archive", "summary_sent", "summary"}.

    `cancelled` is a safety-net count (BR-42): the orders this run's `end_of_session` cancelled. It is
    usually 0, because the worker already ended the session at 16:00 and cancelled them then.

    `archive` is the `archive_candles` result, or `{"error": <exception type>}` when the archive raised (an
    `error` event is written and the summary still goes out, BR-60).

    `summary` says what happened to the daily summary, as far as the `notifications` table shows it
    (`Notifier.send` returns nothing): `sent` (the row is `sent`), `handed_off` (sent to a notifier that keeps
    no `notifications` row, such as a test fake), `duplicate` (a `summary:<date>` row already existed before
    this run, so nothing was issued or sent), `failed` (the row is `failed` or still `sending`), or `error`
    (building or sending raised). `summary_sent` is True only for `sent` and `handed_off`."""
    if not deps.calendar.is_session(session_date):
        return {"skipped": "not a session"}
    started = deps.clock.now()
    still_open = await deps.engine.end_of_session(session_date)
    cancelled = _count_cancelled(deps, since=started)
    _ensure_journal(deps, session_date)
    archive: dict[str, Any]
    counts: dict[str, int]
    try:
        archive = await archive_candles(deps, session_date)
    except Exception as exc:  # the archive is for replay; the summary and journal (BR-60) must still happen
        error = type(exc).__name__
        log.error("postclose.archive_failed", error=error)
        archive, counts = {"error": error}, {}
        _log_error(deps, f"candle archive for {session_date} failed ({error})", session_date, error)
    else:
        counts = {
            OPENING_BAR_CODE: int(archive[OPENING_BAR_CODE]),
            MINUTE_CODE: int(archive[MINUTE_CODE]),
            "missing": len(archive["missing"]),
        }
    summary = await _send_summary(deps, session_date, counts)
    return {
        "open_positions": [int(p.id) for p in still_open],
        "cancelled": cancelled,
        "archive": archive,
        "summary_sent": summary in ("sent", "handed_off"),
        "summary": summary,
    }


def _log_error(deps: PostcloseDeps, message: str, session_date: date, error: str) -> None:
    """An `error` event (source job.postclose, which the relay alerts). A database failure is only logged."""
    try:
        with session_scope(deps.factory) as s:
            log_event(
                s,
                deps.clock,
                "error",
                SOURCE,
                message,
                {"session_date": session_date.isoformat(), "error": error},
                deps.run_id,
            )
    except Exception as db_exc:
        log.error("postclose.event_log_failed", error=type(db_exc).__name__)


def _summary_status(deps: PostcloseDeps, dedupe_key: str) -> str | None:
    """The `notifications` status of the summary's dedupe key, or None when there is no row."""
    with deps.factory() as s:
        return s.execute(
            select(m.Notification.status).where(m.Notification.dedupe_key == dedupe_key)
        ).scalar_one_or_none()


async def _send_summary(deps: PostcloseDeps, session_date: date, counts: Mapping[str, int]) -> str:
    """Send the daily summary once per session (see `run_postclose` for the returned status)."""
    dedupe_key = f"summary:{session_date.isoformat()}"
    try:
        if _summary_status(deps, dedupe_key) is not None:
            # A forced re-run: the notifier would drop the message, so issue no journal nonce for it.
            log.info("postclose.summary_already_recorded", dedupe_key=dedupe_key)
            return "duplicate"
        view = daily_summary_view(deps.factory, deps.run_id, session_date, deps.clock.now(), counts)
        buttons: Buttons = ()
        try:
            _nonce, data = deps.issuer.issue(
                "journal", session_date.strftime("%Y%m%d"), JOURNAL_ACTIONS, deps.chat_id, None
            )
            buttons = ((Button("Yes", data["y"]), Button("No", data["n"])),)
        except (
            Exception
        ) as exc:  # the summary goes out without buttons; the journal page still takes the answer
            log.error("postclose.journal_buttons_failed", error=type(exc).__name__)
        msg = deps.render.daily_summary(view, buttons)
        await deps.notifier.send(dataclasses.replace(msg, dedupe_key=dedupe_key))
        status = _summary_status(deps, dedupe_key)
    except Exception as exc:
        error = type(exc).__name__
        log.error("postclose.summary_failed", error=error)
        _log_error(deps, f"daily summary for {session_date} not sent ({error})", session_date, error)
        return "error"
    if status is None:
        return "handed_off"
    return "sent" if status == "sent" else "failed"


def _count_cancelled(deps: PostcloseDeps, since: datetime) -> int:
    """Orders the end of session just cancelled (SimBroker marks them cancel_reason "end_of_session")."""
    with deps.factory() as s:
        return int(
            s.execute(
                select(func.count())
                .select_from(m.Order)
                .where(
                    m.Order.run_id == deps.run_id,
                    m.Order.cancel_reason == "end_of_session",
                    m.Order.closed_at >= since,
                )
            ).scalar_one()
        )


def _ensure_journal(deps: PostcloseDeps, session_date: date) -> None:
    """The day's journal row, unanswered; an existing row (and its answer) is left alone."""
    with session_scope(deps.factory) as s:
        s.execute(
            insert(m.Journal)
            .values(run_id=deps.run_id, session_date=session_date, rules_followed=None)
            .on_conflict_do_nothing(index_elements=[m.Journal.run_id, m.Journal.session_date])
        )


def _missing(sid: int, ticker: str, interval: str, reason: str) -> dict[str, Any]:
    return {"symbol_id": sid, "ticker": ticker, "interval": interval, "reason": reason}


async def archive_candles(deps: PostcloseDeps, session_date: date) -> dict[str, Any]:
    """(a) the opening 5-minute bar of every universe symbol; (b) 1-minute regular-hours candles for the
    top `postclose.archive_top_n` candidates of the run plus SPY. Returns {"5m": rows, "1m": rows,
    "missing": [{"symbol_id", "ticker", "interval", "reason"}]}."""
    cal = deps.calendar
    open_, close = cal.session_open(session_date), cal.session_close(session_date)
    universe = await deps.data.universe(session_date)
    tickers = {u.symbol_id: u.ticker for u in universe}
    missing: list[dict[str, Any]] = []

    # (a) opening bars: the 9:35 scan's cache first, one batched fetch for the rest.
    ids = list(tickers)
    with deps.factory() as s:
        cached = s.execute(
            select(m.IntradayCandle).where(
                m.IntradayCandle.interval == OPENING_BAR_CODE,
                m.IntradayCandle.ts == open_,
                m.IntradayCandle.symbol_id.in_(ids),
            )
        ).scalars()
        bars: dict[int, Candle] = {
            r.symbol_id: Candle(
                r.ts, r.ts + timedelta(minutes=5), r.open, r.high, r.low, r.close, r.volume, r.vwap
            )
            for r in cached
        }
    need = [sid for sid in ids if sid not in bars]
    if need:
        reasons: dict[int, str] = {}
        try:
            fetched = await deps.data.opening_bars(session_date, need)
        except Exception as exc:  # a failed batch: every symbol in it is missing, the job carries on
            reasons = {sid: _reason(exc) for sid in need}
        else:
            for sid in need:
                found = fetched.bars.get(sid)
                if found is not None and found.start == open_:
                    bars[sid] = found
                else:
                    reasons[sid] = fetched.missing.get(sid, "no_bar_at_open")
        missing += [_missing(sid, tickers[sid], OPENING_BAR_CODE, why) for sid, why in reasons.items()]
    with session_scope(deps.factory) as s:
        n5 = repo.upsert_candle_archive_bars(s, OPENING_BAR_CODE, bars.items())  # one statement
    missing_open = len(missing)

    # (b) 1-minute candles for the top candidates and SPY.
    targets, spy_id = _minute_targets(deps, session_date, universe)
    n1 = 0
    for sid, ticker in targets.items():
        try:
            candles = await deps.data.candles(sid, open_, close, "OneMinute")
        except Exception as exc:  # one symbol's failure never sinks the archive
            missing.append(_missing(sid, ticker, MINUTE_CODE, _reason(exc)))
            continue
        rth = regular_hours(candles, cal, session_date)
        if not rth:
            missing.append(_missing(sid, ticker, MINUTE_CODE, "no_candles"))
            continue
        with session_scope(deps.factory) as s:
            n1 += repo.upsert_candle_archive(s, sid, MINUTE_CODE, rth)

    spy_missing = spy_id is None or any(
        x["symbol_id"] == spy_id and x["interval"] == MINUTE_CODE for x in missing
    )
    _alert_gaps(deps, session_date, len(universe), missing_open, spy_missing, missing)
    return {OPENING_BAR_CODE: n5, MINUTE_CODE: n1, "missing": missing}


def _minute_targets(
    deps: PostcloseDeps, session_date: date, universe: Sequence[UniverseMember]
) -> tuple[dict[int, str], int | None]:
    """symbol_id -> ticker for the top-N distinct candidate symbols (by rank, any strategy) plus SPY, and
    SPY's id (None when there is no SPY symbol)."""
    top_n = deps.settings().postclose_archive_top_n
    with deps.factory() as s:
        rows = s.execute(
            select(m.Candidate.symbol_id, m.Symbol.ticker)
            .join(m.Symbol, m.Symbol.id == m.Candidate.symbol_id)
            .where(
                m.Candidate.run_id == deps.run_id,
                m.Candidate.session_date == session_date,
                m.Candidate.rank.is_not(None),
            )
            .order_by(m.Candidate.rank, m.Candidate.id)
        ).all()
        spy_id = next((u.symbol_id for u in universe if u.ticker == OVERLAY_SYMBOL), None)
        if spy_id is None:
            spy_id = s.execute(
                select(m.Symbol.id)
                .where(m.Symbol.ticker == OVERLAY_SYMBOL, m.Symbol.exchange.not_like("STALE-%"))
                .order_by(m.Symbol.id)
                .limit(1)
            ).scalar_one_or_none()
    targets: dict[int, str] = {}
    for sid, ticker in rows:
        if len(targets) >= top_n:
            break
        targets.setdefault(sid, ticker)
    if spy_id is not None:
        targets.setdefault(spy_id, OVERLAY_SYMBOL)
    return targets, spy_id


def _alert_gaps(
    deps: PostcloseDeps,
    session_date: date,
    universe_size: int,
    missing_open: int,
    spy_missing: bool,
    missing: list[dict[str, Any]],
) -> None:
    """Error events (the relay alerts them) for gaps that hurt replay: >5% of opening bars, or SPY."""
    too_many = universe_size > 0 and Decimal(missing_open) > MAX_MISSING_OPEN_FRACTION * universe_size
    if not too_many and not spy_missing:
        return
    with session_scope(deps.factory) as s:
        if too_many:
            log_event(
                s,
                deps.clock,
                "error",
                SOURCE,
                f"candle archive for {session_date}: {missing_open} of {universe_size} opening bars missing",
                {
                    "session_date": session_date.isoformat(),
                    "missing": [x for x in missing if x["interval"] == OPENING_BAR_CODE][:50],
                },
                deps.run_id,
            )
        if spy_missing:
            log_event(
                s,
                deps.clock,
                "error",
                SOURCE,
                f"candle archive for {session_date}: {OVERLAY_SYMBOL} 1-minute candles missing",
                {
                    "session_date": session_date.isoformat(),
                    "missing": [
                        x for x in missing if x["ticker"] == OVERLAY_SYMBOL and x["interval"] == MINUTE_CODE
                    ],
                },
                deps.run_id,
            )


def daily_summary_view(
    factory: sessionmaker[Session],
    run_id: int,
    session_date: date,
    now: datetime,
    archive: Mapping[str, int],
) -> DailySummaryView:
    """The day in numbers: trades, realized P&L and fees (v_daily_pnl), the latest equity snapshot, open
    positions, human decisions and their average time (BR-33; auto approvals take no decision time and are
    not counted), unprotected time of the day's positions (BR-33), blocking kill switches, archive counts."""
    day_start, day_end = _et_day(session_date)
    with factory() as s:
        trades = tuple(
            TradeLine(ticker, t.qty, t.entry_price, t.exit_price, t.pnl, t.pnl_r, t.exit_reason)
            for t, ticker in s.execute(
                select(m.Trade, m.Symbol.ticker)
                .join(m.Symbol, m.Symbol.id == m.Trade.symbol_id)
                .where(m.Trade.run_id == run_id, m.Trade.session_date == session_date)
                .order_by(m.Trade.closed_at, m.Trade.id)
            ).all()
        )
        pnl = s.execute(
            select(V_DAILY_PNL.c.realized_pnl, V_DAILY_PNL.c.fees).where(
                V_DAILY_PNL.c.run_id == run_id, V_DAILY_PNL.c.session_date == session_date
            )
        ).one_or_none() or (None, None)
        snap = s.execute(
            select(m.EquitySnapshot)
            .where(m.EquitySnapshot.run_id == run_id, m.EquitySnapshot.ts <= now)
            .order_by(m.EquitySnapshot.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if snap is not None:
            equity, drawdown = snap.equity, snap.drawdown_pct
        else:
            start_cash = s.execute(
                select(m.SimAccount.starting_cash).where(m.SimAccount.run_id == run_id)
            ).scalar_one_or_none()
            equity, drawdown = (start_cash if start_cash is not None else Decimal(0)), Decimal(0)
        decisions, avg_ms = s.execute(
            select(func.count(m.Proposal.id), func.avg(m.Proposal.decision_latency_ms)).where(
                m.Proposal.run_id == run_id,
                m.Proposal.created_at >= day_start,
                m.Proposal.created_at < day_end,
                m.Proposal.decision_latency_ms.is_not(None),
                m.Proposal.decided_via.is_distinct_from("auto"),
            )
        ).one()
        todays = s.execute(
            select(m.Position).where(m.Position.run_id == run_id, m.Position.session_date == session_date)
        ).scalars()
        unprotected = sum(_unprotected(p, now) for p in todays)
        open_positions = _open_lines(s, run_id, now)
    blocking = KillSwitches(factory, FixedClock(now)).active(run_id, session_date)
    return DailySummaryView(
        session_date=session_date,
        trades=trades,
        realized_pnl=pnl[0] if pnl[0] is not None else Decimal(0),
        fees=pnl[1] if pnl[1] is not None else Decimal(0),
        equity=equity,
        drawdown_pct=drawdown,
        open_positions=open_positions,
        decisions=int(decisions),
        avg_decision_seconds=float(avg_ms) / 1000 if avg_ms is not None else None,
        unprotected_seconds=unprotected,
        blocking_switches=tuple(dict.fromkeys(a.switch for a in blocking)),
        archive=dict(archive),
    )


def _unprotected(p: m.Position, now: datetime) -> int:
    """Recorded unprotected time plus the running interval of an open, unprotected position."""
    running = 0
    if p.closed_at is None and p.unprotected_since is not None and now > p.unprotected_since:
        running = int((now - p.unprotected_since).total_seconds())
    return p.unprotected_seconds + running


def _open_lines(s: Session, run_id: int, now: datetime) -> tuple[PositionLine, ...]:
    rows = s.execute(
        select(m.Position, m.Symbol.ticker)
        .join(m.Symbol, m.Symbol.id == m.Position.symbol_id)
        .where(m.Position.run_id == run_id, m.Position.closed_at.is_(None))
        .order_by(m.Position.id)
    ).all()
    lines: list[PositionLine] = []
    for p, ticker in rows:
        stop, working = p.stop_loss, False
        if p.stop_order_id is not None:
            order = s.get(m.Order, p.stop_order_id)
            if order is not None and order.status == "working" and order.stop_price is not None:
                stop, working = order.stop_price, True
        lines.append(
            PositionLine(
                position_id=p.id,
                ticker=ticker,
                qty=p.qty,
                entry=p.avg_price,
                last=None,  # no quotes after the close
                stop=stop,
                unrealized_pnl=None,
                unprotected_seconds=_unprotected(p, now),
                stop_working=working,
            )
        )
    return tuple(lines)
