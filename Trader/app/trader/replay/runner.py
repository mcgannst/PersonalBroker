"""The replay runner (SPEC §8, §16; P5-T6): create a replay run from a request (validated, settings snapshot
with automatic approvals, strategies pinned), run it session by session through the real engine with a
`ReplayClock`, record progress, honour cancellation, and settle abandoned runs.

One replay at a time: the session-level advisory lock `REPLAY_LOCK` is held by the running process. A replay
never uses `fire_event`, `run_job` or `job_runs`.

The loop of one session (decision "The replay loop"): the visited times are every planned event time and,
while any order is working, every minute boundary; at a visited time `t` the runner first sets the replay
clock to `t`, then hands the engine the 1-minute bar that ended at `t` for each working symbol (bars before
events: a bar ending at `t` is complete at `t`), then runs the events due at `t`, then `tick(t)`. At
`close - 1 minute` any position still open is force-closed (its working orders cancelled, a market exit
submitted with reason `replay_forced_close`), which the bar ending at the close fills. At the close the open
positions are checked again (fix round 1): one opened after the check (for example by the last bar, with
`no_entry_before_close_minutes = 0`) gets its forced exit then, and every forced exit still working (no bar,
or a zero-volume or bad bar) is filled by a synthetic bar at the last known close, so a replay never carries
a position into the next session. Then `end_of_session` at the close.

Determinism (Review Focus 1): simulated time only from the replay clock (set here, never read from the wall);
every collection iterated is sorted (symbols and orders by id, strategies by key, events by time then key);
no randomness; wall time (`started_at`, `updated_at`, `finished_at`, the data-mode decision) only from the
injected `wall` clock.
"""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol, cast

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, func, select, text, update
from sqlalchemy import Engine as DbEngine
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.auth import QuestradeAuth
from trader.adapters.questrade.client import QuestradeClient
from trader.bootstrap import Core
from trader.broker.types import FillEvent, OrderSpec
from trader.db import models as m
from trader.db.session import session_scope
from trader.decisions.recorder import record_day
from trader.decisions.types import SOURCE as DECISIONS_SOURCE
from trader.decisions.types import RecorderDeps, ScanData
from trader.engine.runs import ensure_sim_account
from trader.engine.scheduler import DayPlan, day_plan
from trader.events import log_event
from trader.logging_setup import redact_text
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, RealClock, et_date
from trader.market.data_service import QuoteClient
from trader.market.types import Candle, OpenBarStats, UniverseMember
from trader.replay.catalysts import ReplayCatalysts
from trader.replay.clock import ReplayClock
from trader.replay.data import ReplayData
from trader.replay.setup import PinnedRegistry, build_replay_engine, pinned_views
from trader.replay.types import (
    ACTIVE_STATUSES,
    REPLAY_OVERRIDE_KEYS,
    DataMode,
    PinnedStrategy,
    ReplayBusy,
    ReplayEngine,
    ReplayInvalid,
    ReplayMarket,
    ReplayProgress,
    ReplayRequest,
    ReplayRun,
    ReplayStatus,
    StrategyOverride,
    load_replay_run,
)
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import CatalystSource, Strategy
from trader.strategies.registry import StrategyConfigView, StrategyRegistry, load_all

log = structlog.get_logger("replay.runner")

REPLAY_LOCK = "trader.replay"
SOURCE = "replay"
FORCED_CLOSE = "replay_forced_close"
ABANDONED = "abandoned"
BIASED_SUFFIX = " (biased universe)"
MAX_LABEL = 200
MAX_ERROR_CHARS = 500
ABANDON_QUEUED_AFTER = timedelta(minutes=2)
# `date_to` may be today only once today's session has closed and its data has settled.
TODAY_AFTER_CLOSE = timedelta(minutes=15)
# A replay created in this ET window of a session day runs offline (never competes with the live worker).
OFFLINE_FROM = time(9, 15)
OFFLINE_UNTIL = time(16, 30)
ONE_MINUTE = timedelta(minutes=1)


def _lock_key(name: str) -> int:
    digest = hashlib.blake2b(name.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


REPLAY_LOCK_KEY = _lock_key(REPLAY_LOCK)


def _describe(exc: BaseException) -> str:
    """One masked line, capped (it is stored on the run and shown on the Replay page)."""
    one_line = " ".join(f"{type(exc).__name__}: {exc}".split())
    return redact_text(one_line)[:MAX_ERROR_CHARS]


# --- the advisory lock --------------------------------------------------------------------------------------
def _db_engine(factory: sessionmaker[Session]) -> DbEngine:
    bind = factory.kw.get("bind")
    if not isinstance(bind, DbEngine):
        raise TypeError("the replay runner needs a sessionmaker bound to an Engine")
    return bind


def _acquire(factory: sessionmaker[Session]) -> Connection | None:
    """Take `REPLAY_LOCK` on a dedicated connection; None when another process holds it."""
    conn = _db_engine(factory).connect()
    try:
        conn.execution_options(isolation_level="AUTOCOMMIT")
        granted = bool(
            conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": REPLAY_LOCK_KEY}).scalar_one()
        )
    except BaseException:
        conn.invalidate()  # the lock may have been granted before the error: never pool it
        conn.close()
        raise
    if not granted:
        conn.close()
        return None
    return conn


def _release(conn: Connection) -> None:
    """Release the lock and close its connection; a failed unlock discards the connection, so it never
    goes back to the pool still holding the lock."""
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REPLAY_LOCK_KEY})
    except BaseException as exc:
        log.warning("replay.unlock_failed", error=_describe(exc))
        conn.invalidate()
    finally:
        conn.close()


# A bigint advisory lock as pg_locks shows it: classid = the key's high 32 bits, objid = its low 32 bits
# (both as unsigned oids), objsubid = 1.
_LOCK_HELD_SQL = text(
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND classid = CAST(:hi AS oid) AND objid = CAST(:lo AS oid) AND objsubid = 1)"
)


def _lock_held(s: Session) -> bool:
    """Whether any session holds `REPLAY_LOCK`, read from pg_locks WITHOUT taking it (fix round 1: a check
    that took the lock for a moment made a runner starting at that moment fail with ReplayBusy)."""
    key = REPLAY_LOCK_KEY & 0xFFFFFFFFFFFFFFFF
    return bool(s.execute(_LOCK_HELD_SQL, {"hi": key >> 32, "lo": key & 0xFFFFFFFF}).scalar_one())


# --- validation and creation --------------------------------------------------------------------------------
def _sessions(calendar: SessionCalendar, date_from: date, date_to: date) -> list[date]:
    out: list[date] = []
    d = date_from
    while d <= date_to:
        if calendar.is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _pydantic_errors(prefix: str, exc: ValidationError) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        out.append((f"{prefix}.{loc}" if loc else prefix, str(err.get("msg", "invalid value"))))
    return out


def _check_dates(
    request: ReplayRequest,
    wall: Clock,
    calendar: SessionCalendar,
    max_sessions: int,
    errors: list[tuple[str, str]],
) -> list[date]:
    if request.date_from > request.date_to:
        errors.append(("date_from", "must be on or before date_to"))
        return []
    now = wall.now()
    today = et_date(now)
    if request.date_to > today:
        errors.append(("date_to", "is in the future"))
        return []
    try:
        if request.date_to == today and calendar.is_session(today):
            if now < calendar.session_close(today) + TODAY_AFTER_CLOSE:
                errors.append(("date_to", "today's session can be replayed from 15 minutes after its close"))
                return []
        sessions = _sessions(calendar, request.date_from, request.date_to)
    except ValueError:
        errors.append(("date_from", "is outside the market calendar"))
        return []
    if not sessions:
        errors.append(("date_to", "the range holds no trading session"))
    elif len(sessions) > max_sessions:
        errors.append(
            ("date_to", f"the range holds {len(sessions)} sessions, more than the {max_sessions} allowed")
        )
    return sessions


def _snapshot(
    live: RuntimeSettings, overrides: Mapping[str, Any], errors: list[tuple[str, str]]
) -> RuntimeSettings | None:
    for key in sorted(overrides):
        if key not in REPLAY_OVERRIDE_KEYS:
            errors.append((f"overrides.{key}", "is not a setting a replay may override"))
            continue
        try:
            RuntimeSettings.model_validate({key: overrides[key]})
        except ValidationError as exc:
            errors.extend((f"overrides.{key}", str(e.get("msg", "invalid value"))) for e in exc.errors())
    if errors:
        return None
    base = live.model_dump(mode="json", by_alias=True)
    return RuntimeSettings.model_validate({**base, **dict(overrides), "approval_mode": "auto"})


@dataclass(frozen=True, slots=True)
class _Pin:
    key: str
    live: StrategyConfigView
    params: dict[str, Any]  # validated, merged over the live params
    enabled: bool
    override: bool  # needs its own `replay`-scoped row


def _check_strategies(
    registry: StrategyRegistry, requested: Mapping[str, StrategyOverride], errors: list[tuple[str, str]]
) -> list[_Pin]:
    known = registry.keys()
    for key in sorted(requested):
        if key not in known:
            errors.append((f"strategies.{key}", "is not an installed strategy"))
    pins: list[_Pin] = []
    for key in known:
        o = requested.get(key)
        try:
            live = registry.current(key)
        except KeyError:
            if o is not None:
                errors.append((f"strategies.{key}", "has no live settings yet"))
            continue
        model: type[BaseModel] = registry.plugin_class(key).params_model
        merged = {**live.params, **dict((o.params if o is not None else None) or {})}
        try:
            params = model.model_validate(merged).model_dump(mode="json")
        except ValidationError as exc:
            if o is not None and o.params:
                errors.extend(_pydantic_errors(f"strategies.{key}.params", exc))
            else:
                errors.append((f"strategies.{key}", "its live settings no longer validate"))
            continue
        enabled = live.enabled if o is None or o.enabled is None else o.enabled
        override = o is not None and (bool(o.params) or enabled != live.enabled)
        pins.append(_Pin(key, live, params, enabled, override))
    if not errors and not any(p.enabled and registry.plugin_class(p.key).kind == "entry" for p in pins):
        errors.append(("strategies", "at least one entry strategy must be enabled"))
    return pins


def in_offline_window(calendar: SessionCalendar, now: datetime) -> bool:
    """True when a replay created at `now` runs offline: 09:15 (inclusive) to 16:30 (exclusive) ET on a
    session day, so it never competes with the live worker for Questrade. The one definition of the window,
    shared by `create_replay` and the API's `offline_now` (P5-T17)."""
    local = now.astimezone(ET)
    return calendar.is_session(local.date()) and OFFLINE_FROM <= local.time() < OFFLINE_UNTIL


def _data_mode(request: ReplayRequest, wall: Clock, calendar: SessionCalendar) -> DataMode:
    if request.offline or in_offline_window(calendar, wall.now()):
        return "offline"
    return "full"


def _lock_is_free(factory: sessionmaker[Session]) -> bool:
    with factory() as s:
        return not _lock_held(s)


def _active_replay(s: Session) -> int | None:
    return s.execute(
        select(m.Run.id).where(m.Run.mode == "replay", m.Run.status.in_(ACTIVE_STATUSES)).limit(1)
    ).scalar_one_or_none()


def create_replay(
    factory: sessionmaker[Session],
    wall: Clock,
    calendar: SessionCalendar,
    settings: SettingsStore,
    registry: StrategyRegistry,
    request: ReplayRequest,
    actor: str,
    *,
    config_writer: Callable[..., StrategyConfigView] | None = None,
    app_version: str = "dev",
) -> int:
    """Validate and store a `queued` replay run (with its `replay.start` audit row); returns its id. Raises
    `ReplayInvalid` (every problem, by field path) or `ReplayBusy`.

    `config_writer` (default `registry.create_replay_config`) writes the `replay`-scoped row of a strategy
    override. It commits on its own connection, so the run row (inserted first, for its id) and its audit
    row are committed together after it; an orphan override row left by a failure is `replay`-scoped and
    invisible to the live run."""
    live = settings.load()
    errors: list[tuple[str, str]] = []
    if request.label is not None and len(request.label) > MAX_LABEL:
        errors.append(("label", f"must be at most {MAX_LABEL} characters"))
    _check_dates(request, wall, calendar, live.replay_max_sessions, errors)
    snapshot = _snapshot(live, request.overrides, errors)
    pins = _check_strategies(registry, request.strategies, errors)
    if errors or snapshot is None:
        raise ReplayInvalid(errors)

    reconcile_abandoned(factory, wall)
    if not _lock_is_free(factory):
        raise ReplayBusy("a replay is running")
    writer = config_writer if config_writer is not None else registry.create_replay_config
    data_mode = _data_mode(request, wall, calendar)
    snapshot_json = snapshot.model_dump(mode="json", by_alias=True)
    overrides = {k: snapshot_json[k] for k in sorted(request.overrides)}
    label = request.label or f"replay {request.date_from.isoformat()}..{request.date_to.isoformat()}"
    now = wall.now()
    with session_scope(factory) as s:
        # serialise creators, then refuse while another replay is queued or running
        s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(f"{REPLAY_LOCK}.create")})
        busy = _active_replay(s)
        if busy is not None:
            raise ReplayBusy(f"replay {busy} is queued or running")
        run = m.Run(
            mode="replay",
            started_at=now,
            updated_at=now,
            params={"kind": "replay"},
            status="queued",
            label=label,
            progress=ReplayProgress().to_json(),
            cancel_requested=False,
        )
        s.add(run)
        s.flush()
        pinned: list[PinnedStrategy] = []
        for pin in pins:
            view = pin.live
            if pin.override:
                view = writer(
                    pin.key,
                    base=pin.live,
                    params=pin.params,
                    enabled=pin.enabled,
                    created_by=f"replay:{run.id}",
                )
            pinned.append(
                PinnedStrategy(
                    key=pin.key,
                    config_id=view.id,
                    revision=view.revision,
                    version=view.version,
                    scope="replay" if pin.override else "live",
                    enabled=view.enabled,
                    params=dict(view.params),
                )
            )
        strategies = [p.to_json() for p in pinned]
        run.params = {
            "kind": "replay",
            "date_from": request.date_from.isoformat(),
            "date_to": request.date_to.isoformat(),
            "label": request.label,
            "data_mode": data_mode,
            "catalyst_mode": snapshot.replay_catalyst_mode,
            "half_spread_bps": str(snapshot.replay_half_spread_bps),
            "settings": snapshot_json,
            "overrides": overrides,
            "strategies": strategies,
            "code_version": app_version,
        }
        s.add(
            m.AuditLog(
                ts=now,
                actor=actor,
                action="replay.start",
                before=None,
                after={
                    "run_id": run.id,
                    "date_from": request.date_from.isoformat(),
                    "date_to": request.date_to.isoformat(),
                    "overrides": overrides,
                    "strategies": strategies,
                    "data_mode": data_mode,
                },
            )
        )
        return run.id


# --- running ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ReplayDeps:
    factory: sessionmaker[Session]
    wall: Clock
    calendar: SessionCalendar
    market_factory: Callable[[ReplayRun, ReplayClock], ReplayMarket]
    catalysts_factory: Callable[[ReplayRun], CatalystSource]
    engine_factory: Callable[[ReplayRun, ReplayClock, ReplayMarket, CatalystSource], ReplayEngine]
    # P6-T10: records each replayed session's decision journal (final) under the replay's run id, after every
    # session, when the run's settings snapshot has `reports.decisions_enabled`. A failure is one warning
    # event and the replay carries on. None (the default): no journal.
    decisions: Callable[[ReplayRun, date, ScanData], Awaitable[None]] | None = None


class _ReplayScanData:
    """The recorder's `ScanData` over the replay market: the day's universe, stats and the opening bars
    `prepare_day` already loaded (every universe member's), so it never fetches anything."""

    def __init__(self, market: ReplayMarket) -> None:
        self._market = market

    async def universe(self, session_date: date) -> list[UniverseMember]:
        return await self._market.universe(session_date)

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]:
        return await self._market.open_bar_stats(session_date)

    async def stored_opening_bars(self, session_date: date, symbol_ids: list[int]) -> dict[int, Candle]:
        loaded = {u.symbol_id for u in await self._market.universe(session_date)}
        ids = sorted(set(symbol_ids) & loaded)
        return dict((await self._market.opening_bars(session_date, ids)).bars) if ids else {}


async def _record_decisions(
    deps: ReplayDeps, run: ReplayRun, day: date, market: ReplayMarket, clock: Clock
) -> None:
    """Run the decisions hook for one replayed session. Never raises (a cancellation still propagates): a
    failure writes one `warning` event with the replay's run id (source `decisions`)."""
    if deps.decisions is None or not run.settings.reports_decisions_enabled:
        return
    try:
        await deps.decisions(run, day, _ReplayScanData(market))
    except Exception as exc:
        error = _describe(exc)
        log.warning("replay.decisions_failed", run_id=run.id, day=day.isoformat(), error=error)
        try:
            with session_scope(deps.factory) as s:
                log_event(
                    s,
                    clock,
                    "warning",
                    DECISIONS_SOURCE,
                    f"replay {run.id}: decision log for {day} not recorded: {error}",
                    {"run_id": run.id, "session_date": day.isoformat(), "error": error},
                    run_id=run.id,
                )
        except Exception as db_exc:
            log.error("replay.decisions_failure_unrecorded", run_id=run.id, error=_describe(db_exc))


def _plan_strategies(run: ReplayRun, plugins: Mapping[str, type[Any]]) -> list[Strategy]:
    """The enabled pinned plug-ins, built from their pinned params, by key. One that can't be built is left
    out of the plan (the engine's pinned registry reports it with the run id)."""
    out: list[Strategy] = []
    for pin in sorted(run.strategies, key=lambda p: p.key):
        cls = plugins.get(pin.key)
        if not pin.enabled or cls is None:
            continue
        try:
            out.append(cls(cls.params_model.model_validate(dict(pin.params))))
        except Exception as exc:
            log.error("replay.plan_strategy_failed", run_id=run.id, strategy=pin.key, error=_describe(exc))
    return out


def _next_boundary(after: datetime) -> datetime:
    """The first whole minute strictly after `after`."""
    return after.replace(second=0, microsecond=0) + ONE_MINUTE


def _synthetic_bar(close: datetime, price: Decimal) -> Candle:
    return Candle(
        start=close - ONE_MINUTE,
        end=close,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1,
        vwap=None,
    )


@dataclass(frozen=True, slots=True)
class _Forced:
    order_id: int
    symbol_id: int
    fallback: Decimal  # the synthetic bar's price when the day has no bar (the position's average price)
    submitted_at: datetime


class _TargetedEngine(Protocol):
    """`Engine.on_candles_for` (fix round 1): a candle pass limited to the given orders that skips the
    "submitted before the bar ended" rule, for forced exits submitted at the close itself."""

    async def on_candles_for(
        self, candles: Mapping[int, Candle], now: datetime, orders: Collection[int]
    ) -> list[FillEvent]: ...


class _Day:
    """One session of the loop (see the module docstring)."""

    def __init__(self, engine: ReplayEngine, market: ReplayMarket, clock: ReplayClock, plan: DayPlan) -> None:
        if plan.open is None or plan.close is None:
            raise ValueError(f"{plan.session_date} is not a trading session")
        self.engine = engine
        self.market = market
        self.clock = clock
        self.day = plan.session_date
        self.open: datetime = plan.open
        self.close: datetime = plan.close
        self.forced_at = self.close - ONE_MINUTE
        self.events = [e for e in plan.events if e.at <= self.close]
        self.next_event = 0
        self.loaded: set[int] = set()
        self.forced: dict[int, _Forced] = {}  # position id -> its forced exit
        self.forced_done = False
        self.forced_closes = 0
        self.last: datetime | None = None

    def _candidates(self) -> list[datetime]:
        out: list[datetime] = []
        if self.next_event < len(self.events):
            out.append(max(self.events[self.next_event].at, self.last or self.events[self.next_event].at))
        if self.engine.broker.working_symbol_ids():
            out.append(_next_boundary(max(self.last or self.open, self.open)))
        if not self.forced_done and self.engine.broker.open_positions():
            out.append(max(self.forced_at, self.last or self.forced_at))
        return [t for t in out if t <= self.close]

    async def run(self) -> int:
        while True:
            candidates = self._candidates()
            if not candidates:
                break
            await self._visit(min(candidates))
        self._set(self.close)
        await self._close_out()
        await self.engine.end_of_session(self.day)
        return self.forced_closes

    def _set(self, t: datetime) -> None:
        if t > self.clock.now():
            self.clock.set(t)

    async def _visit(self, t: datetime) -> None:
        self._set(t)
        revisit = self.last is not None and t <= self.last
        if not revisit and t.second == 0 and t.microsecond == 0 and t > self.open:
            await self._bars(t)
        while self.next_event < len(self.events) and self.events[self.next_event].at <= t:
            await self.engine.run_event(self.events[self.next_event].key, self.day)
            self.next_event += 1
        if not revisit:
            await self.engine.tick(t)
        self.last = t
        if not self.forced_done and t >= self.forced_at:
            self.forced_done = True
            self._force_close(t)

    async def _bars(self, t: datetime) -> None:
        ids = sorted(self.engine.broker.working_symbol_ids())
        if not ids:
            return
        need = [sid for sid in ids if sid not in self.loaded]
        if need:
            await self.market.load_minute_bars(need, self.day)
            self.loaded.update(need)
        bars: dict[int, Candle] = {}
        for sid in ids:
            bar = self.market.bar_ending_at(sid, t)
            if bar is not None:
                bars[sid] = bar
        if bars:
            await self.engine.on_candles(bars, t)

    def _force_close(self, t: datetime, *, only_unforced: bool = False) -> None:
        """Cancel each open position's working orders and submit its market exit (`replay_forced_close`).
        `only_unforced`: skip positions whose forced exit is still working (the re-check at the close)."""
        broker = self.engine.broker
        positions = sorted(broker.open_positions(), key=lambda p: p.id)
        working = sorted(broker.working_orders(), key=lambda o: o.id)
        working_ids = {o.id for o in working}
        if only_unforced:
            covered = {pid for pid, f in self.forced.items() if f.order_id in working_ids}
            positions = [p for p in positions if p.id not in covered]
        if not positions:
            return
        for pos in positions:
            for order in working:
                if order.position_id == pos.id:
                    broker.cancel(order.id, FORCED_CLOSE)
            order_id = broker.submit(
                OrderSpec(
                    symbol_id=pos.symbol_id,
                    side="sell",
                    order_type="market",
                    qty=pos.qty,
                    purpose="exit",
                    position_id=pos.id,
                    strategy_config_id=pos.strategy_config_id,
                    reason=FORCED_CLOSE,
                )
            )
            if pos.id not in self.forced:
                self.forced_closes += 1
            self.forced[pos.id] = _Forced(order_id, pos.symbol_id, pos.avg_price, t)
        log.info("replay.forced_close", at=t.isoformat(), positions=[p.id for p in positions])

    async def _close_out(self) -> None:
        """At the close (fix round 1): force-close any position the check at `close - 1 minute` did not
        cover, then fill every forced exit still working with a synthetic bar at the last known close (the
        position's average price when there is none), so no position outlives the session."""
        close = self.close
        broker = self.engine.broker
        if not broker.open_positions():
            return
        self._force_close(close, only_unforced=True)
        working = {o.id for o in broker.working_orders()}
        pending = [f for _, f in sorted(self.forced.items()) if f.order_id in working]
        if not pending:
            return

        def bars_for(forced: list[_Forced]) -> dict[int, Candle]:
            out: dict[int, Candle] = {}
            for f in forced:
                last = self.market.last_close(f.symbol_id, close)
                out.setdefault(f.symbol_id, _synthetic_bar(close, last if last is not None else f.fallback))
            return out

        # exits submitted before the close: the synthetic bar ends after them, the normal rule applies
        early = [f for f in pending if f.submitted_at < close]
        if early:
            await self.engine.on_candles(bars_for(early), close)
        # exits submitted AT the close (the re-check): no bar ends after them, so the pass names them
        late = [f for f in pending if f.submitted_at >= close]
        if late:
            await cast(_TargetedEngine, self.engine).on_candles_for(
                bars_for(late), close, [f.order_id for f in late]
            )
        left = sorted(p.id for p in broker.open_positions())
        if left:
            log.error("replay.position_left_open", at=close.isoformat(), positions=left)


def _set_status(
    factory: sessionmaker[Session],
    wall: Clock,
    run_id: int,
    status: ReplayStatus,
    *,
    error: str | None = None,
    label: str | None = None,
    progress: ReplayProgress | None = None,
) -> None:
    now = wall.now()
    with session_scope(factory) as s:
        row = s.get(m.Run, run_id, with_for_update=True)
        if row is None:
            return
        row.status = status
        row.updated_at = now
        if status in ("completed", "failed", "cancelled"):
            row.finished_at = now
        if error is not None:
            row.error = error
        if label is not None:
            row.label = label[:MAX_LABEL]
        if progress is not None:
            row.progress = progress.to_json()


def _write_progress(
    factory: sessionmaker[Session], wall: Clock, run_id: int, progress: ReplayProgress
) -> None:
    with session_scope(factory) as s:
        s.execute(
            update(m.Run).where(m.Run.id == run_id).values(progress=progress.to_json(), updated_at=wall.now())
        )


def _cancel_requested(factory: sessionmaker[Session], run_id: int) -> bool:
    with factory() as s:
        return bool(s.execute(select(m.Run.cancel_requested).where(m.Run.id == run_id)).scalar_one())


def _trades(factory: sessionmaker[Session], run_id: int) -> int:
    with factory() as s:
        return int(s.execute(select(func.count(m.Trade.id)).where(m.Trade.run_id == run_id)).scalar_one())


def _progress(
    base: ReplayProgress, market: ReplayMarket, done: int, day: date, trades: int, forced: int
) -> ReplayProgress:
    counts = market.progress_counts()
    return ReplayProgress(
        sessions_total=base.sessions_total,
        sessions_done=done,
        current_date=day,
        trades=trades,
        forced_closes=forced,
        biased_days=tuple(sorted(market.biased_days)),
        missing_opening_bars=int(counts.get("missing_opening_bars", 0)),
        missing_minute_bars=int(counts.get("missing_minute_bars", 0)),
        questrade_requests=int(counts.get("questrade_requests", 0)),
    )


def _start_time(
    strategies: list[Strategy],
    cal: SessionCalendar,
    sessions: list[date],
    settings: RuntimeSettings,
    wall: Clock,
) -> datetime:
    """The replay clock's start: the first session's open - 1 minute, or its first planned event when that
    is earlier (fix round 1: a pre-open event of day 1 then runs at its own time, not after it)."""
    if not sessions:
        return wall.now()
    start = cal.session_open(sessions[0]) - ONE_MINUTE
    events = day_plan(strategies, cal, sessions[0], settings).events
    return min([start, *(e.at for e in events)])


async def run_replay(deps: ReplayDeps, run_id: int) -> ReplayRun:
    """Run a `queued` replay to its end under `REPLAY_LOCK`; returns the final state (`completed`,
    `cancelled` or `failed`). `ReplayBusy` when another replay holds the lock. A run that is no longer
    `queued` (cancelled before it started, or already settled) is returned as it is."""
    conn = _acquire(deps.factory)
    if conn is None:
        raise ReplayBusy("another replay is running")
    try:
        run = load_replay_run(deps.factory, run_id)
        if run.status != "queued":
            log.warning("replay.not_queued", run_id=run_id, status=run.status)
            return run
        await _run_locked(deps, run)
        return load_replay_run(deps.factory, run_id)
    finally:
        _release(conn)


async def _run_locked(deps: ReplayDeps, run: ReplayRun) -> None:
    factory, wall, cal = deps.factory, deps.wall, deps.calendar
    sessions = _sessions(cal, run.date_from, run.date_to)
    progress = ReplayProgress(sessions_total=len(sessions))
    with session_scope(factory) as s:
        claimed = s.execute(
            update(m.Run)
            .where(m.Run.id == run.id, m.Run.status == "queued")
            .values(status="running", updated_at=wall.now(), progress=progress.to_json())
            .returning(m.Run.id)
        ).scalar_one_or_none()
    if claimed is None:  # cancelled between the read and now
        return
    clock: ReplayClock | None = None
    try:
        strategies = _plan_strategies(run, load_all())
        clock = ReplayClock(_start_time(strategies, cal, sessions, run.settings, wall))
        market = deps.market_factory(run, clock)
        catalysts = deps.catalysts_factory(run)
        engine = deps.engine_factory(run, clock, market, catalysts)
        with session_scope(factory) as s:
            ensure_sim_account(s, run.id, run.settings, clock.now())
        forced = 0
        final: ReplayStatus = "completed"
        for done, day in enumerate(sessions, start=1):
            if _cancel_requested(factory, run.id):
                final = "cancelled"
                break
            await market.prepare_day(day)
            plan = day_plan(strategies, cal, day, run.settings)
            forced += await _Day(engine, market, clock, plan).run()
            await _record_decisions(deps, run, day, market, clock)
            progress = _progress(progress, market, done, day, _trades(factory, run.id), forced)
            _write_progress(factory, wall, run.id, progress)
        label = run.label or ""
        if market.biased_days and not label.endswith(BIASED_SUFFIX):
            label = label[: MAX_LABEL - len(BIASED_SUFFIX)] + BIASED_SUFFIX
        _set_status(factory, wall, run.id, final, label=label or None, progress=progress)
    except BaseException as exc:
        _fail(deps, run.id, clock, exc)
        if not isinstance(exc, Exception):
            raise


def _fail(deps: ReplayDeps, run_id: int, clock: Clock | None, exc: BaseException) -> None:
    """Settle the run `failed` with one masked line and write ONE `error` event with the run's id (the
    relay ignores other runs' events). Never raises."""
    error = _describe(exc)
    log.error("replay.failed", run_id=run_id, error=error)
    try:
        _set_status(deps.factory, deps.wall, run_id, "failed", error=error)
        with session_scope(deps.factory) as s:
            log_event(
                s,
                clock or deps.wall,
                "error",
                SOURCE,
                f"replay {run_id} failed: {error}",
                {"run_id": run_id, "error": error},
                run_id=run_id,
            )
    except Exception as db_exc:
        log.error("replay.failure_unrecorded", run_id=run_id, error=_describe(db_exc))


# --- cancel and abandoned runs ------------------------------------------------------------------------------
def request_cancel(factory: sessionmaker[Session], wall: Clock, run_id: int, actor: str) -> bool:
    """Ask a `queued` or `running` replay to stop (audit `replay.cancel`); False when it isn't either. A
    `queued` run is cancelled at once; a `running` one stops before its next session."""
    now = wall.now()
    with session_scope(factory) as s:
        row = s.get(m.Run, run_id, with_for_update=True)
        if row is None or row.mode != "replay" or row.status not in ACTIVE_STATUSES:
            return False
        before = {"status": row.status, "cancel_requested": bool(row.cancel_requested)}
        row.cancel_requested = True
        row.updated_at = now
        if row.status == "queued":
            row.status = "cancelled"
            row.finished_at = now
        s.add(
            m.AuditLog(
                ts=now,
                actor=actor,
                action="replay.cancel",
                before=before,
                after={"run_id": run_id, "status": row.status, "cancel_requested": True},
            )
        )
        return True


def reconcile_abandoned(factory: sessionmaker[Session], wall: Clock) -> list[int]:
    """When `REPLAY_LOCK` is free, settle every `running` replay and every `queued` one older than 2 minutes
    as `failed` ("abandoned"); returns their ids.

    It never takes the lock itself (fix round 1): it locks the active rows, then reads pg_locks. A runner
    takes the lock before it claims its row, and the claim waits for these row locks, so a lock seen free
    here means no live runner owns any of them."""
    now = wall.now()
    settled: list[int] = []
    with session_scope(factory) as s:
        rows = (
            s.execute(
                select(m.Run)
                .where(m.Run.mode == "replay", m.Run.status.in_(ACTIVE_STATUSES))
                .order_by(m.Run.id)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        if not rows or _lock_held(s):
            return []
        for row in rows:
            if row.status == "queued" and row.started_at > now - ABANDON_QUEUED_AFTER:
                continue
            row.status, row.error, row.finished_at, row.updated_at = "failed", ABANDONED, now, now
            settled.append(row.id)
    if settled:
        log.warning("replay.abandoned", run_ids=settled)
    return settled


# --- the real composition -----------------------------------------------------------------------------------
@asynccontextmanager
async def open_replay_deps(core: Core, *, data_mode: DataMode) -> AsyncIterator[ReplayDeps]:
    """The real composition: `ReplayData` (a rate-limited `QuestradeClient` in `full` mode, none offline),
    `ReplayCatalysts`, and `build_replay_engine` over a `PinnedRegistry`.

    The Questrade client gets a `RealClock` (never the replay clock) and the run snapshot's
    `replay.questrade_rps` (fix round 1: not the live setting); it is built with the run's data source and
    closed when this context exits. The auth is built as `trader.runtime.questrade_auth` builds it;
    `trader.runtime` itself is not imported, because it imports the Telegram adapters (replay isolation)."""
    wall = core.clock
    async with AsyncExitStack() as stack:

        def market_factory(run: ReplayRun, clock: ReplayClock) -> ReplayMarket:
            client: QuoteClient | None = None
            if data_mode == "full" and run.data_mode == "full":
                auth = QuestradeAuth(core.factory, core.crypto, core.clock)
                qt = QuestradeClient(auth, RealClock(), market_rps=run.settings.replay_questrade_rps)
                stack.push_async_exit(qt)  # its __aenter__ does nothing: closing is all that is needed
                client = qt
            return ReplayData(
                core.factory,
                clock,
                wall,
                core.calendar,
                client,
                run_id=run.id,
                date_from=run.date_from,
                date_to=run.date_to,
                half_spread_bps=run.half_spread_bps,
                questrade_window_days=run.settings.replay_questrade_window_days,
                lookback_sessions=run.settings.open_bar_lookback_sessions,
                created_at=run.created_at,
                quiet_sleep=asyncio.sleep if client is not None else None,
            )

        def catalysts_factory(run: ReplayRun) -> CatalystSource:
            return ReplayCatalysts(core.factory, run.catalyst_mode)

        def engine_factory(
            run: ReplayRun, clock: ReplayClock, market: ReplayMarket, catalysts: CatalystSource
        ) -> ReplayEngine:
            registry = PinnedRegistry(core.factory, clock, pinned_views(core.factory, run), run.id)
            return build_replay_engine(core.factory, clock, core.calendar, run, market, catalysts, registry)

        async def decisions(run: ReplayRun, day: date, scan: ScanData) -> None:
            # the replay's settings snapshot (enabled flag, scan detail), never the live settings
            recorder = RecorderDeps(core.factory, wall, core.calendar, lambda: run.settings, scan)
            await record_day(recorder, run.id, day, final=True)

        yield ReplayDeps(
            core.factory, wall, core.calendar, market_factory, catalysts_factory, engine_factory, decisions
        )
