"""The options post-close job, 16:20 ET (OPTSIM task plan T13; job `options_postclose`).

In this order, each step safe to repeat:

1. expire the day orders of the session;
2. expiry, exercise and assignment against the official close; 3. early assignment; then the adjustment
   check (a frozen structure is alerted like any other lifecycle event);
4. each lifecycle event: its message, then delivery to the plug-in that owns the structure. Events of an
   earlier run that were never delivered are read back from `opt_lifecycle_events` and go out too;
5. end-of-day marks, the account at liquidation marks, one `equity_snapshots` row at the session close;
6. the built-in `postclose` event for every enabled plug-in (after the snapshot, so `ctx.equity_on(today)`
   answers; the host runs it once per session);
7. the plug-ins' prompts are synced and the due ones sent;
8. the summary message, once per session (dedupe key `opt:summary:<date>`).

A step that raises is noted and the later steps still run. Afterwards the job FAILS when a step raised, a
plug-in's post-close event failed, or an official close was missing (those structures were left unchanged),
so the runner's in-process retry and a later manual run pick it up. Nothing is done twice by that: orders
are already expired, lifecycle events are unique per position, kind and day, delivery is marked on the
event, the snapshot is one row per (run, close time), and the notifier drops a repeated dedupe key.

While an official close is missing, steps 6 and 8 wait: both go out once per session, so they are left to
the attempt that settles everything and then tell the whole day.

The official close (risk R8): for today, once the session has closed, the share quote's regular-hours last
trade; otherwise, and for any earlier day, that day's daily candle. None means "not known yet".
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.ledger import Ledger
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs.runner import JobOutcome, RetryPolicy, run_job_async
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.notify.types import Notifier
from trader.option_strategies.base import POSTCLOSE_EVENT, OptionEvent
from trader.options.account import options_account
from trader.options.book import DbBook, load_contracts
from trader.options.lifecycle import LifecycleEngine, MissingClose
from trader.options.protocols import (
    OptionBroker,
    OptionMarketView,
    OptionRenderer,
    OptionStrategyRegistryView,
    OptionSummaryView,
    PromptStore,
    StrategyHost,
)
from trader.options.settings import OptionSettings
from trader.options.types import ZERO, LifecycleEvent, LifecycleKind, OptionAccountState

log = structlog.get_logger("jobs.options_postclose")

POSTCLOSE_JOB = "options_postclose"
MAX_REASON_CHARS = 200
RATIO = Decimal("0.0001")

OfficialClose = Callable[[str, date], Awaitable[Decimal | None]]  # (ticker, day), as the engine calls it


class JobMarket(OptionMarketView, Protocol):
    """The market as the option jobs use it: the view plus two methods of T3's `OptionMarketService`."""

    async def refresh_chain(self, underlying: str, *, force: bool = False) -> int: ...

    async def record_marks(self, contract_ids: Sequence[int]) -> int: ...


class Lifecycle(Protocol):
    """T6's `LifecycleEngine`, as the jobs use it."""

    missing_closes: list[MissingClose]

    async def run_expiry(self, session_date: date) -> list[LifecycleEvent]: ...

    async def run_early_assignment(self, session_date: date) -> list[LifecycleEvent]: ...

    async def check_adjustments(self) -> list[LifecycleEvent]: ...


# The engine holds a settings VALUE and the run id, so the job builds one per run: (run id, settings, close).
LifecycleFactory = Callable[[int, OptionSettings, OfficialClose], Lifecycle]


class PromptSending(Protocol):
    """T10's `PromptSender`."""

    async def send_due(self, now: datetime) -> int: ...


class PostcloseIncomplete(RuntimeError):
    """Every step ran, but something is left to do (the message lists it). Retried by the runner."""


@dataclass(frozen=True)
class PostcloseDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], OptionSettings]
    run_id: Callable[[], int | None]  # the active options run, read when the job starts
    broker: OptionBroker
    market: JobMarket
    host: StrategyHost
    registry: OptionStrategyRegistryView
    prompts: PromptStore
    prompt_sender: PromptSending
    notifier: Notifier
    renderer: OptionRenderer
    lifecycle: LifecycleFactory | None = None  # None: `lifecycle_factory` (the engine over the database book)
    retry: RetryPolicy | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def reason(exc: BaseException) -> str:
    """An exception as one masked, capped line."""
    flat = " ".join(redact_text(f"{type(exc).__name__}: {exc}").split())
    return flat if len(flat) <= MAX_REASON_CHARS else flat[: MAX_REASON_CHARS - 1] + "…"


def not_a_session(calendar: SessionCalendar, session_date: date) -> bool:
    try:
        return not calendar.is_session(session_date)
    except ValueError:  # outside the calendar's range
        return True


# --- the official close ----------


async def official_close(
    market: OptionMarketView, calendar: SessionCalendar, clock: Clock, underlying: str, day: date
) -> Decimal | None:
    """The underlying's official close of session `day`, or None when it is not known (yet).

    Today, once the session has closed: the share quote's regular-hours last trade, else today's daily
    candle. An earlier day: its daily candle only (the live quote says nothing about it). A day that is not
    a session, lies ahead, or whose session is still open has no close."""
    now = clock.now()
    today = et_date(now)
    if day > today or not_a_session(calendar, day) or now < calendar.session_close(day):
        return None
    if day == today:
        quote = await market.underlying_quote(underlying)
        if quote is not None and quote.last_regular is not None and quote.last_regular > 0:
            return quote.last_regular
    for bar in await market.daily_bars(underlying, day, day):
        if et_date(bar.start) == day:
            return bar.close
    return None


def close_source(market: OptionMarketView, calendar: SessionCalendar, clock: Clock) -> OfficialClose:
    """`official_close` in the shape the lifecycle engine calls. A source that raises (an unknown ticker,
    Questrade down) counts as "no close yet": the structure is left alone and the job retries."""

    async def close(underlying: str, day: date) -> Decimal | None:
        try:
            return await official_close(market, calendar, clock, underlying, day)
        except Exception as exc:
            log.warning(
                "options.official_close_failed",
                underlying=underlying,
                day=day.isoformat(),
                error=type(exc).__name__,
            )
            return None

    return close


def lifecycle_factory(
    factory: sessionmaker[Session], clock: Clock, calendar: SessionCalendar, market: OptionMarketView
) -> LifecycleFactory:
    """The real engine over the database book."""
    ledger = Ledger(calendar)

    def build(run_id: int, settings: OptionSettings, close: OfficialClose) -> Lifecycle:
        return LifecycleEngine(
            factory,
            clock,
            calendar,
            market,
            settings,
            run_id,
            lambda s: DbBook(s, run_id, ledger, clock),
            close,
        )

    return build


# --- lifecycle events: read back, message, deliver ----------


def stored_events(
    factory: sessionmaker[Session],
    run_id: int,
    *,
    session_date: date | None = None,
    undelivered: bool = False,
) -> list[LifecycleEvent]:
    """The run's lifecycle events from the database, oldest first: those of one session, or those not yet
    delivered to their plug-in."""
    ev, st = m.OptLifecycleEvent, m.OptStructure
    stmt = (
        select(ev, st.source, st.strategy_config_id)
        .join(st, st.id == ev.structure_id)
        .where(ev.run_id == run_id)
        .order_by(ev.id)
    )
    if session_date is not None:
        stmt = stmt.where(ev.session_date == session_date)
    if undelivered:
        stmt = stmt.where(ev.delivered_at.is_(None))
    with factory() as s:
        rows = s.execute(stmt).all()
        contracts = load_contracts(s, sorted({r[0].contract_id for r in rows if r[0].contract_id}))
        return [
            LifecycleEvent(
                id=row.id,
                structure_id=row.structure_id,
                source=source,
                strategy_config_id=config_id,
                kind=cast(LifecycleKind, row.kind),
                session_date=row.session_date,
                ts=row.ts,
                contract=contracts.get(row.contract_id) if row.contract_id is not None else None,
                qty=row.qty,
                strike=row.strike,
                underlying_close=row.underlying_close,
                shares_delta=row.shares_delta,
                cash_delta=row.cash_delta,
                new_structure_id=row.new_structure_id,
            )
            for row, source, config_id in rows
        ]


def merged(*groups: Sequence[LifecycleEvent]) -> list[LifecycleEvent]:
    """The events of every group once each, by id."""
    return sorted({e.id: e for group in groups for e in group}.values(), key=lambda e: e.id)


async def announce(
    notifier: Notifier, renderer: OptionRenderer, host: StrategyHost, events: Sequence[LifecycleEvent]
) -> list[str]:
    """Each event: its message, then to the plug-in that owns the structure. Both are idempotent (the
    message by its dedupe key, the delivery by the event's `delivered_at`). A message that can't be sent
    never holds back the delivery. Returns what failed, one line each."""
    failed: list[str] = []
    for event in events:
        try:
            await notifier.send(renderer.lifecycle(event))
        except Exception as exc:
            failed.append(f"message for lifecycle event {event.id}: {reason(exc)}")
        try:
            await host.deliver_lifecycle(event)
        except Exception as exc:
            failed.append(f"delivery of lifecycle event {event.id}: {reason(exc)}")
    return failed


# --- the steps ----------


class _Steps:
    """Runs each step on its own: one that raises is noted in `problems` and the next still runs."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    async def run[T](self, name: str, step: Awaitable[T], default: T) -> T:
        try:
            return await step
        except Exception as exc:
            log.error("options_postclose.step_failed", step=name, error=reason(exc))
            self.problems.append(f"{name}: {reason(exc)}")
            return default


def _et_day(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0), tzinfo=ET)
    return start, start + timedelta(days=1)


def write_snapshot(
    factory: sessionmaker[Session], run_id: int, ts: datetime, account: OptionAccountState
) -> None:
    """One `equity_snapshots` row at `ts` (the session close): the account value at liquidation marks. The
    peak and the drawdown (a fraction of the peak) follow the run's earlier snapshots. A re-run rewrites
    the same row."""
    snap = m.EquitySnapshot
    with session_scope(factory) as s:
        before = s.execute(
            select(func.max(snap.peak_equity)).where(snap.run_id == run_id, snap.ts < ts)
        ).scalar_one()
        peak = account.equity if before is None else max(account.equity, before)
        drawdown = ((peak - account.equity) / peak).quantize(RATIO) if peak > 0 else ZERO
        values = {
            "equity": account.equity,
            "cash": account.cash,
            "settled_cash": account.cash,
            "peak_equity": peak,
            "drawdown_pct": drawdown,
        }
        s.execute(
            pg_insert(snap)
            .values(run_id=run_id, ts=ts, **values)
            .on_conflict_do_update(index_elements=[snap.run_id, snap.ts], set_=values)
        )


def _equity_before(factory: sessionmaker[Session], run_id: int, session_date: date) -> Decimal | None:
    """The account value the day started from: the last snapshot before the session's ET day, else the
    run's starting cash."""
    snap = m.EquitySnapshot
    with factory() as s:
        last = s.execute(
            select(snap.equity)
            .where(snap.run_id == run_id, snap.ts < _et_day(session_date)[0])
            .order_by(snap.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
    if last is not None:
        return last
    account = options_account(factory, run_id)
    return None if account is None else account.starting_cash


def _fills_on(factory: sessionmaker[Session], run_id: int, session_date: date) -> int:
    """The orders filled on that ET day."""
    start, end = _et_day(session_date)
    with factory() as s:
        return int(
            s.execute(
                select(func.count(func.distinct(m.OptFill.order_id))).where(
                    m.OptFill.run_id == run_id, m.OptFill.ts >= start, m.OptFill.ts < end
                )
            ).scalar_one()
        )


async def _fire_postclose(deps: PostcloseDeps, session_date: date) -> dict[str, str]:
    """The `postclose` event for every enabled plug-in: its key -> the outcome's status. The host runs an
    event once per session, so a repeat is `skipped`."""
    await deps.host.ensure_defaults()
    fired: dict[str, str] = {}
    for key in deps.registry.keys():
        try:
            if not deps.registry.current(key).enabled:
                continue
        except KeyError:  # a plug-in whose settings row could not be made: the registry logged why
            continue
        try:
            event = OptionEvent(POSTCLOSE_EVENT, session_date, scheduled=False)
            fired[key] = (await deps.host.fire(key, event)).status
        except Exception as exc:
            log.error("options_postclose.event_failed", strategy=key, error=reason(exc))
            fired[key] = "failed"
    return fired


async def _send_summary(
    deps: PostcloseDeps,
    run_id: int,
    session_date: date,
    account: OptionAccountState,
    open_structures: int,
    events: Sequence[LifecycleEvent],
) -> str:
    """`sent` (handed to the notifier) or `duplicate` (this session's summary is already recorded)."""
    start, end = _et_day(session_date)
    realized: dict[str, Decimal] = {}
    for st in await deps.broker.structures(open_only=False):
        if st.closed_at is not None and start <= st.closed_at < end:
            realized[st.source] = realized.get(st.source, ZERO) + st.realized_pnl
    before = _equity_before(deps.factory, run_id, session_date)
    msg = deps.renderer.summary(
        OptionSummaryView(
            session_date=session_date,
            account=account,
            day_change=None if before is None else account.equity - before,
            fills=_fills_on(deps.factory, run_id, session_date),
            lifecycle=tuple(events),
            open_structures=open_structures,
            pending_prompts=len(deps.prompts.pending()),
            by_source=realized,
        )
    )
    if msg.dedupe_key is not None:
        with deps.factory() as s:
            known = s.execute(
                select(m.Notification.id).where(m.Notification.dedupe_key == msg.dedupe_key).limit(1)
            ).scalar_one_or_none()
        if known is not None:
            return "duplicate"
    await deps.notifier.send(msg)
    return "sent"


async def run_postclose(deps: PostcloseDeps, run_id: int, session_date: date) -> dict[str, Any]:
    """The job's body (see the module text for the steps). Returns the detail, or raises
    `PostcloseIncomplete` once every step has run."""
    settings = deps.settings()
    steps = _Steps()
    build = deps.lifecycle or lifecycle_factory(deps.factory, deps.clock, deps.calendar, deps.market)
    engine = build(run_id, settings, close_source(deps.market, deps.calendar, deps.clock))
    detail: dict[str, Any] = {}

    # 1: the day's working orders
    detail["expired_orders"] = await steps.run(
        "expire day orders", deps.broker.expire_day_orders(session_date, deps.clock.now()), 0
    )

    # 2, 3: expiry and assignment, early assignment, then the adjustment check
    events: list[LifecycleEvent] = []
    events += await steps.run("expiry", engine.run_expiry(session_date), [])
    events += await steps.run("early assignment", engine.run_early_assignment(session_date), [])
    missing = list(engine.missing_closes)
    events += await steps.run("adjustment check", engine.check_adjustments(), [])

    # 4: message and deliver, including what an earlier run left undelivered
    queue = merged(stored_events(deps.factory, run_id, undelivered=True), events)
    steps.problems += await announce(deps.notifier, deps.renderer, deps.host, queue)
    detail["lifecycle"] = len(queue)

    # 5: marks, the account, the snapshot at the session close
    structures = await steps.run("read structures", deps.broker.structures(), [])
    held = sorted({p.contract.id for st in structures for p in st.positions if p.contract and p.qty})
    detail["marks"] = await steps.run("marks", deps.market.record_marks(held), 0) if held else 0
    account = await steps.run("account", deps.broker.account(), None)
    if account is not None:
        write_snapshot(deps.factory, run_id, deps.calendar.session_close(session_date), account)
        detail["equity"] = str(account.equity)
        detail["marks_complete"] = account.marks_complete

    # 6: the plug-ins' post-close event. Both it and the summary (8) go out once per session, so while an
    # official close is missing they wait: the attempt that settles everything sends them.
    if missing:
        detail["events"] = {}
    else:
        fired = await steps.run("post-close events", _fire_postclose(deps, session_date), {})
        detail["events"] = fired
        steps.problems += [
            f"{key}: the post-close event failed" for key, st in fired.items() if st == "failed"
        ]

    # 7: prompts
    detail["prompts_synced"] = await steps.run("sync prompts", deps.host.sync_prompts(), 0)
    detail["prompts_sent"] = await steps.run("send prompts", deps.prompt_sender.send_due(deps.clock.now()), 0)

    # 8: the summary
    if account is None:
        detail["summary"] = "no account"
    elif missing:
        detail["summary"] = "waiting for the official close"
    else:
        today = merged(
            stored_events(deps.factory, run_id, session_date=session_date),
            [e for e in events if e.session_date == session_date],
        )
        detail["summary"] = await steps.run(
            "summary", _send_summary(deps, run_id, session_date, account, len(structures), today), "error"
        )

    detail["missing_closes"] = [
        {"underlying": miss.underlying, "close_date": miss.close_date.isoformat()} for miss in missing
    ]
    steps.problems += [
        f"no official close for {miss.underlying} on {miss.close_date.isoformat()}" for miss in missing
    ]
    if steps.problems:
        log.warning("options_postclose.incomplete", session_date=session_date.isoformat(), **detail)
        raise PostcloseIncomplete("; ".join(steps.problems))
    return detail


async def postclose_job(deps: PostcloseDeps, session_date: date, *, force: bool = False) -> JobOutcome:
    """Job `options_postclose` for one session. Skipped (no `job_runs` row) on a day that is not a session,
    before that session's close, and when there is no options run. `force` re-runs a session that already
    succeeded; the plug-ins' events and the summary still go out once only."""
    if not_a_session(deps.calendar, session_date):
        return JobOutcome("skipped", {"reason": "not a session"})
    if deps.clock.now() < deps.calendar.session_close(session_date):
        return JobOutcome("skipped", {"reason": "the session has not closed"})
    run_id = deps.run_id()
    if run_id is None:
        return JobOutcome("skipped", {"reason": "no options run"})
    active = run_id

    async def body() -> dict[str, Any]:
        return await run_postclose(deps, active, session_date)

    return await run_job_async(
        deps.factory,
        deps.clock,
        POSTCLOSE_JOB,
        session_date,
        body,
        force,
        retry=deps.retry,
        sleep=deps.sleep,
    )
