"""The strategy host (OPTSIM task plan T7): the one place that runs an option plug-in's hooks.

For each call it builds the plug-in's context (its own open structures, working orders, state and prompts;
the whole account), awaits the hook, turns the returned intents into broker requests, and records the
plug-in's notes (event_log, source `options.strategy.<key>`) and alerts (an event_log warning plus the
alert sink). It never names a plug-in: everything goes through the registry.

Rules it keeps:
- A plug-in only acts on its own structures and orders: an intent naming another source's is dropped with
  an error event. An unknown contract or a rejected order is a note, and the other intents still run.
- While `options.strategies_paused` is set or the plug-in is disabled, no event fires and intents returned
  by any hook are dropped with a warning (fills, lifecycle events and answers are still delivered, so the
  plug-in's own records stay true).
- `fire` runs inside `run_job_async` as job `opt_event:<strategy>:<event>` for the event's session date, so
  an event runs once per session whoever asks; `force` re-runs it.
- A hook that raises is logged as an error event and never stops the other plug-ins; an answer or a
  lifecycle event whose hook raised stays undelivered and is retried on the next call.
- `panel` and `action` build the same context and never submit orders.
"""

import json
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from trader.db import models as m
from trader.db.session import session_scope
from trader.events import Level, log_event
from trader.jobs.runner import JobOutcome, run_job_async
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, et_date
from trader.option_strategies.base import (
    KEY_PATTERN,
    CancelOrder,
    CloseStructure,
    OpenStructure,
    OptionEvent,
    OptionIntent,
    OptionStrategy,
    OptionStrategyConfigView,
    OptionStrategyContext,
    PanelActionRequest,
    PanelActionResult,
    Reprice,
    RollStructure,
    SellShares,
    StrategyPanel,
)
from trader.option_strategies.registry import OptionStrategyRegistry
from trader.option_strategies.state import DbStrategyState
from trader.options.account import lock_book
from trader.options.protocols import OptionBroker, OptionMarketView, PromptStore
from trader.options.settings import OptionSettingsStore
from trader.options.types import (
    LegSpec,
    LifecycleEvent,
    OptionFillEvent,
    OrderLeg,
    OrderRequest,
    PromptView,
    StructureView,
)

SOURCE_PREFIX = "options.strategy."  # + the strategy key: at most 37 characters (event_log.source is 50)
JOB_PREFIX = "opt_event"
MAX_ERROR = 500

AlertSink = Callable[[str, str, str, str], Awaitable[None]]  # (source, kind, message, dedupe_key)
Hook = Callable[[OptionStrategy, OptionStrategyContext], Awaitable[list[OptionIntent]]]

log = structlog.get_logger("options.strategy_host")
_KEY = re.compile(KEY_PATTERN)


class NoOptionsRun(LookupError):
    """`panel` or `action` was asked for while there is no active options run."""


class _Drop(Exception):
    """This intent can't become an order; the message says why. `level` is the note's level."""

    def __init__(self, message: str, level: Level = "warning") -> None:
        super().__init__(message)
        self.level = level


def job_name(strategy_key: str, event_key: str) -> str:
    """The job_runs name of one strategy event (at most 50 characters for keys matching KEY_PATTERN)."""
    return f"{JOB_PREFIX}:{strategy_key}:{event_key}"


def _error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR]


def _json(data: dict[str, Any]) -> dict[str, Any]:
    """A plug-in's note data as JSON values (a Decimal or a date becomes its text)."""
    out: dict[str, Any] = json.loads(json.dumps(data, default=str))
    return out


def _actor(strategy_key: str) -> str:
    return f"strategy:{strategy_key}"


def _closing_legs(structure: StructureView, qty: int | None) -> tuple[tuple[OrderLeg, ...], int]:
    """The structure's open positions reversed, and the number of units the order is for. A unit is one of
    the structure's `qty` sets of legs (when its positions divide evenly by it; else their greatest common
    divisor). `qty` closes that many units instead of all of them."""
    held = [p for p in structure.positions if p.qty != 0]
    if not held:
        raise _Drop(f"structure {structure.id} has nothing to close")
    sizes = [abs(p.qty) for p in held]
    units = structure.qty if all(size % structure.qty == 0 for size in sizes) else math.gcd(*sizes)
    legs = tuple(
        OrderLeg(
            leg_no=n,
            instrument=p.instrument,
            side="sell" if p.qty > 0 else "buy",
            effect="close",
            ratio=abs(p.qty) // units,
            underlying=p.underlying,
            contract_id=None if p.contract is None else p.contract.id,
        )
        for n, p in enumerate(held, start=1)
    )
    if qty is None:
        return legs, units
    if not 0 < qty <= units:
        raise _Drop(f"structure {structure.id} holds {units} unit(s): can't close {qty}")
    return legs, qty


class DefaultStrategyHost:
    """`StrategyHost`. `settings` is the option settings store (read at each call); `run_id` answers the
    active options run now; `alert_sink(source, kind, message, dedupe_key)` sends a plug-in's alert (the
    source is the strategy key). `usd_cad_rate` is read at each call for the context (1 when not given)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        registry: OptionStrategyRegistry,
        broker: OptionBroker,
        market: OptionMarketView,
        prompts: PromptStore,
        settings: OptionSettingsStore,
        run_id: Callable[[], int | None],
        alert_sink: AlertSink,
        *,
        usd_cad_rate: Callable[[], Decimal] | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._calendar = calendar
        self._registry = registry
        self._broker = broker
        self._market = market
        self._prompts = prompts
        self._settings = settings
        self._run_id = run_id
        self._alert_sink = alert_sink
        self._usd_cad_rate = usd_cad_rate

    # --- records --------------------------------------------------------------------------------------------

    def _event(
        self, run_id: int | None, key: str, level: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        """One event_log row for this plug-in. Never raises: a record that can't be written is logged."""
        try:
            with session_scope(self._factory) as s:
                log_event(s, self._clock, level, SOURCE_PREFIX + key, message, _json(data or {}), run_id)
        except Exception as exc:
            log.error("option_strategy.event_unrecorded", strategy=key, message=message, error=_error(exc))

    def _hook_failed(self, run_id: int | None, key: str, what: str, exc: BaseException) -> None:
        log.error("option_strategy.hook_failed", strategy=key, hook=what, error=_error(exc))
        self._event(run_id, key, "error", f"{key}: {what} failed", {"hook": what, "error": _error(exc)})

    async def _flush(self, ctx: OptionStrategyContext) -> None:
        """Write the context's notes and alerts, and send the alerts. Each list is emptied."""
        notes, alerts = list(ctx.notes), list(ctx.alerts)
        ctx.notes.clear()
        ctx.alerts.clear()
        for note in notes:
            self._event(ctx.run_id, ctx.strategy_key, note.level, note.message, note.data)
        for alert in alerts:
            self._event(
                ctx.run_id,
                ctx.strategy_key,
                "warning",
                alert.message,
                {**alert.data, "kind": alert.kind, "dedupe_key": alert.dedupe_key},
            )
            try:
                await self._alert_sink(ctx.strategy_key, alert.kind, alert.message, alert.dedupe_key)
            except Exception as exc:
                self._hook_failed(ctx.run_id, ctx.strategy_key, f"alert {alert.kind}", exc)

    # --- the context ----------------------------------------------------------------------------------------

    def _equity_on(self, run_id: int, day: date) -> Decimal | None:
        """The last account value recorded on that ET day (the post-close job writes one at the close)."""
        start = datetime.combine(day, time.min, tzinfo=ET)
        with self._factory() as s:
            return s.execute(
                select(m.EquitySnapshot.equity)
                .where(
                    m.EquitySnapshot.run_id == run_id,
                    m.EquitySnapshot.ts >= start,
                    m.EquitySnapshot.ts < start + timedelta(days=1),
                )
                .order_by(m.EquitySnapshot.ts.desc())
                .limit(1)
            ).scalar_one_or_none()

    async def _context(
        self, key: str, cfg: OptionStrategyConfigView, run_id: int, session_date: date | None = None
    ) -> OptionStrategyContext:
        def prompt(dedupe_key: str) -> PromptView | None:
            found = self._prompts.by_key(dedupe_key)
            return found if found is not None and found.source == key else None

        return OptionStrategyContext(
            clock=self._clock,
            calendar=self._calendar,
            session_date=session_date or et_date(self._clock.now()),
            run_id=run_id,
            strategy_key=key,
            config_id=cfg.id,
            params=self._registry.plugin_class(key).params_model.model_validate(cfg.params),
            settings=self._settings.load(),
            market=self._market,
            account=await self._broker.account(),
            structures=await self._broker.structures(source=key, open_only=True),
            orders=await self._broker.orders(status="working", source=key),
            state=DbStrategyState(self._factory, self._clock, key),
            factory=self._factory,
            usd_cad_rate=self._usd_cad_rate() if self._usd_cad_rate is not None else Decimal(1),
            prompt=prompt,
            equity_on=lambda day: self._equity_on(run_id, day),
        )

    async def _call(
        self,
        strategy: OptionStrategy,
        cfg: OptionStrategyConfigView,
        run_id: int,
        hook: Hook,
        session_date: date | None = None,
    ) -> dict[str, Any]:
        """Run one hook and apply its intents. The notes and alerts are recorded even when it raises."""
        ctx = await self._context(cfg.strategy_key, cfg, run_id, session_date)
        try:
            return await self._apply(ctx, cfg, await hook(strategy, ctx))
        finally:
            await self._flush(ctx)

    async def _deliver(self, key: str, run_id: int, what: str, hook: Hook) -> bool:
        """Run a delivery hook of plug-in `key`; False (with an error event) when it raised."""
        try:
            strategy, cfg = self._registry.instance(key)
            await self._call(strategy, cfg, run_id, hook)
        except Exception as exc:
            self._hook_failed(run_id, key, what, exc)
            return False
        return True

    # --- intents to orders ----------------------------------------------------------------------------------

    async def _opening_legs(self, specs: Sequence[LegSpec], first_leg_no: int = 1) -> tuple[OrderLeg, ...]:
        legs: list[OrderLeg] = []
        for n, spec in enumerate(specs, start=first_leg_no):
            contract_id: int | None = None
            if spec.instrument == "option":
                if spec.contract is None:
                    raise _Drop("an option leg names no contract")
                try:
                    contract = await self._market.find_contract(spec.contract)
                except LookupError:  # UnknownUnderlying
                    contract = None
                if contract is None:
                    k = spec.contract
                    raise _Drop(f"unknown contract {k.underlying} {k.expiry} {k.right} {k.strike}")
                contract_id = contract.id
            legs.append(
                OrderLeg(n, spec.instrument, spec.side, "open", spec.ratio, spec.underlying, contract_id)
            )
        if not legs:
            raise _Drop("no legs")
        return tuple(legs)

    @staticmethod
    def _own_structure(ctx: OptionStrategyContext, structure_id: int) -> StructureView:
        for structure in ctx.structures:
            if structure.id == structure_id:
                return structure
        raise _Drop(f"structure {structure_id} is not an open structure of {ctx.strategy_key}", "error")

    async def _own_order(self, ctx: OptionStrategyContext, order_id: int) -> None:
        order = await self._broker.order(order_id)
        if order is None or order.source != ctx.strategy_key:
            raise _Drop(f"order {order_id} is not an order of {ctx.strategy_key}", "error")

    async def _request(
        self, ctx: OptionStrategyContext, cfg: OptionStrategyConfigView, intent: OptionIntent
    ) -> OrderRequest | None:
        """The order an intent asks for; None when the intent was a broker call that is now done."""
        key = ctx.strategy_key
        actor = _actor(key)

        def request(**fields: Any) -> OrderRequest:
            return OrderRequest(source=key, strategy_config_id=cfg.id, submitted_by=actor, **fields)

        if isinstance(intent, OpenStructure):
            legs = await self._opening_legs(intent.legs)
            return request(
                intent="open",
                structure_id=None,
                underlying=legs[0].underlying,
                legs=legs,
                qty=intent.qty,
                order_type=intent.order_type,
                net_limit=intent.net_limit,
                tif=intent.tif,
                walk=intent.walk,
                take_profit_pct=intent.take_profit_pct,
                reason=intent.reason,
                evidence=dict(intent.evidence),
            )
        if isinstance(intent, CloseStructure):
            structure = self._own_structure(ctx, intent.structure_id)
            legs, units = _closing_legs(structure, intent.qty)
            return request(
                intent="close",
                structure_id=structure.id,
                underlying=structure.underlying,
                legs=legs,
                qty=units,
                order_type=intent.order_type,
                net_limit=intent.net_limit,
                tif=intent.tif,
                walk=intent.walk,
                take_profit_pct=None,
                reason=intent.reason,
                evidence={},
            )
        if isinstance(intent, RollStructure):
            structure = self._own_structure(ctx, intent.structure_id)
            closing, units = _closing_legs(structure, None)
            opening = await self._opening_legs(intent.open_legs, first_leg_no=len(closing) + 1)
            return request(
                intent="roll",
                structure_id=structure.id,
                underlying=structure.underlying,
                legs=closing + opening,
                qty=units,
                order_type=intent.order_type,
                net_limit=intent.net_limit,
                tif=intent.tif,
                walk=intent.walk,
                take_profit_pct=intent.take_profit_pct,
                reason=intent.reason,
                evidence=dict(intent.evidence),
            )
        if isinstance(intent, SellShares):
            structure = self._own_structure(ctx, intent.structure_id)
            if structure.kind != "shares":
                raise _Drop(f"structure {structure.id} is a {structure.kind}, not shares", "error")
            legs, units = _closing_legs(structure, None)
            return request(
                intent="close",
                structure_id=structure.id,
                underlying=structure.underlying,
                legs=legs,
                qty=units,
                order_type="market",
                net_limit=None,
                tif="day",
                walk=False,
                take_profit_pct=None,
                reason=intent.reason,
                evidence={},
            )
        if isinstance(intent, Reprice):
            await self._own_order(ctx, intent.order_id)
            if not await self._broker.reprice(intent.order_id, intent.net_limit, actor):
                raise _Drop(f"order {intent.order_id} is not working: not repriced")
            return None
        if isinstance(intent, CancelOrder):
            await self._own_order(ctx, intent.order_id)
            if not await self._broker.cancel(intent.order_id, intent.reason, actor):
                raise _Drop(f"order {intent.order_id} is not working: not cancelled")
            return None
        raise _Drop(f"{type(intent).__name__} is not an option intent", "error")

    async def _apply(
        self, ctx: OptionStrategyContext, cfg: OptionStrategyConfigView, intents: Sequence[OptionIntent]
    ) -> dict[str, Any]:
        """Apply each intent on its own: one that can't be applied becomes a note and the rest go on."""
        detail = {"intents": len(intents), "submitted": 0, "rejected": 0, "dropped": 0}
        if not intents:
            return detail
        blocked = (
            "strategies are paused"
            if ctx.settings.strategies_paused
            else None
            if cfg.enabled
            else f"{ctx.strategy_key} is disabled"
        )
        if blocked is not None:
            ctx.note(f"{len(intents)} intent(s) dropped: {blocked}", "warning")
            detail["dropped"] = len(intents)
            return detail
        for intent in intents:
            name = type(intent).__name__
            try:
                req = await self._request(ctx, cfg, intent)
                if req is None:
                    continue
                order = (await self._broker.submit(req)).order
            except _Drop as drop:
                ctx.note(f"{name} dropped: {drop}", drop.level)
                detail["dropped"] += 1
                continue
            except Exception as exc:
                ctx.note(f"{name} failed: {_error(exc)}", "error")
                detail["dropped"] += 1
                continue
            if order.status == "rejected":
                why = f"{order.reject_reason} {order.reject_detail or ''}".strip()
                ctx.note(
                    f"{name} order {order.id} rejected: {why}",
                    "warning",
                    order_id=order.id,
                    reject_reason=order.reject_reason,
                )
                detail["rejected"] += 1
            else:
                detail["submitted"] += 1
        return detail

    # --- StrategyHost ---------------------------------------------------------------------------------------

    async def ensure_defaults(self) -> None:
        self._registry.ensure_defaults()

    async def due_events(self, session_date: date, now: datetime) -> list[tuple[str, OptionEvent]]:
        """The enabled plug-ins' scheduled events whose time has passed and whose job has not succeeded for
        that session, earliest first. Nothing while there is no options run or strategies are paused."""
        run_id = self._run_id()
        if run_id is None or self._settings.load().strategies_paused:
            return []
        try:
            if not self._calendar.is_session(session_date):
                return []
        except ValueError:  # outside the calendar's range
            return []
        passed: list[tuple[datetime, str, str]] = []
        for strategy, cfg in self._registry.enabled():
            key = cfg.strategy_key
            try:
                for scheduled in strategy.schedule(self._calendar):
                    at = scheduled.at.resolve(self._calendar, session_date)
                    if at <= now:
                        passed.append((at, key, scheduled.key))
            except Exception as exc:
                self._hook_failed(run_id, key, "schedule", exc)
        if not passed:
            return []
        with self._factory() as s:
            done = set(
                s.execute(
                    select(m.JobRun.job).where(
                        m.JobRun.job.in_([job_name(key, event) for _, key, event in passed]),
                        m.JobRun.session_date == session_date,
                        m.JobRun.status == "succeeded",
                    )
                ).scalars()
            )
        return [
            (key, OptionEvent(event, session_date, True))
            for _, key, event in sorted(passed)
            if job_name(key, event) not in done
        ]

    async def fire(self, strategy_key: str, event: OptionEvent, *, force: bool = False) -> JobOutcome:
        self._registry.plugin_class(strategy_key)  # KeyError for an unknown strategy
        if not _KEY.fullmatch(event.key):
            raise ValueError(f"event key {event.key!r} does not match {KEY_PATTERN}")
        run_id = self._run_id()
        if run_id is None:
            skip: str | None = "there is no options run"
        elif self._settings.load().strategies_paused:
            skip = "strategies are paused"
        elif not self._registry.current(strategy_key).enabled:
            skip = f"{strategy_key} is disabled"
        else:
            skip = None
        if run_id is None or skip is not None:
            self._event(
                run_id,
                strategy_key,
                "info",
                f"{strategy_key} {event.key} for {event.session_date} skipped: {skip}",
                {"event": event.key, "reason": skip},
            )
            return JobOutcome("skipped", {"reason": skip})
        active_run = run_id

        async def body() -> dict[str, Any]:
            strategy, cfg = self._registry.instance(strategy_key)
            return await self._call(
                strategy, cfg, active_run, lambda st, ctx: st.on_event(ctx, event), event.session_date
            )

        return await run_job_async(
            self._factory, self._clock, job_name(strategy_key, event.key), event.session_date, body, force
        )

    async def deliver_fill(self, fill: OptionFillEvent) -> None:
        run_id = self._run_id()
        if run_id is None or fill.source not in self._registry.keys():
            return
        await self._deliver(
            fill.source, run_id, f"on_fill (order {fill.order_id})", lambda st, ctx: st.on_fill(ctx, fill)
        )

    def _lifecycle_done(self, run_id: int, event_id: int, *, mark: bool) -> bool:
        """Whether the event already has `delivered_at`; with `mark`, set it (under the book lock)."""
        row = m.OptLifecycleEvent
        if not mark:
            with self._factory() as s:
                at = s.execute(select(row.delivered_at).where(row.id == event_id)).scalar_one_or_none()
                return at is not None
        with session_scope(self._factory) as s:
            lock_book(s, run_id)
            s.execute(
                update(row)
                .where(row.id == event_id, row.delivered_at.is_(None))
                .values(delivered_at=self._clock.now())
            )
        return True

    async def deliver_lifecycle(self, event: LifecycleEvent) -> None:
        """To the plug-in that owns the structure, once: a delivered event gets `delivered_at` (so does one
        with no plug-in to tell); one whose hook raised keeps it empty and is retried on the next call."""
        run_id = self._run_id()
        if run_id is None or self._lifecycle_done(run_id, event.id, mark=False):
            return
        if event.source in self._registry.keys() and not await self._deliver(
            event.source,
            run_id,
            f"on_lifecycle ({event.kind}, event {event.id})",
            lambda st, ctx: st.on_lifecycle(ctx, event),
        ):
            return
        self._lifecycle_done(run_id, event.id, mark=True)

    async def deliver_answers(self) -> int:
        run_id = self._run_id()
        if run_id is None:
            return 0
        delivered = 0
        for _, cfg in self._registry.enabled():
            key = cfg.strategy_key
            for prompt in self._prompts.undelivered(key):
                if await self._deliver(
                    key,
                    run_id,
                    f"on_answer (prompt {prompt.id})",
                    lambda st, ctx, prompt=prompt: st.on_answer(ctx, prompt),  # type: ignore[misc]
                ):
                    self._prompts.mark_delivered(prompt.id)
                    delivered += 1
        return delivered

    async def sync_prompts(self) -> int:
        """Open every prompt an enabled plug-in asks for now and cancel its pending ones it no longer asks
        for. Returns the number of prompts opened or cancelled."""
        run_id = self._run_id()
        if run_id is None:
            return 0
        changed = 0
        for strategy, cfg in self._registry.enabled():
            key = cfg.strategy_key
            try:
                ctx = await self._context(key, cfg, run_id)
                try:
                    wanted = await strategy.prompts(ctx)
                finally:
                    await self._flush(ctx)
                for req in wanted:
                    if self._prompts.by_key(req.dedupe_key) is None:
                        changed += 1
                    self._prompts.ensure(run_id, key, req)
                keep = {req.dedupe_key for req in wanted}
                for pending in self._prompts.pending(key):
                    if pending.dedupe_key not in keep:
                        self._prompts.cancel(pending.dedupe_key)
                        changed += 1
            except Exception as exc:
                self._hook_failed(run_id, key, "prompts", exc)
        return changed

    async def watch_underlyings(self) -> set[str]:
        run_id = self._run_id()
        if run_id is None:
            return set()
        tickers: set[str] = set()
        for strategy, cfg in self._registry.enabled():
            try:
                ctx = await self._context(cfg.strategy_key, cfg, run_id)
                tickers |= await strategy.watch_underlyings(ctx)
            except Exception as exc:
                self._hook_failed(run_id, cfg.strategy_key, "watch_underlyings", exc)
        return tickers

    async def panel(self, strategy_key: str) -> StrategyPanel:
        """KeyError for an unknown strategy, NoOptionsRun without a run. Notes are not recorded here: the
        panel is read on every page load."""
        strategy, cfg = self._registry.instance(strategy_key)
        run_id = self._run_id()
        if run_id is None:
            raise NoOptionsRun("there is no active options run")
        try:
            return await strategy.panel(await self._context(strategy_key, cfg, run_id))
        except Exception as exc:
            self._hook_failed(run_id, strategy_key, "panel", exc)
            raise

    async def action(self, strategy_key: str, req: PanelActionRequest, actor: str) -> PanelActionResult:
        """An owner action from the panel: the plug-in may change its own tables; no order is submitted."""
        strategy, cfg = self._registry.instance(strategy_key)
        run_id = self._run_id()
        if run_id is None:
            raise NoOptionsRun("there is no active options run")
        ctx = await self._context(strategy_key, cfg, run_id)
        try:
            result = await strategy.on_action(ctx, req)
        except Exception as exc:
            self._hook_failed(run_id, strategy_key, f"on_action ({req.action})", exc)
            result = PanelActionResult(False, f"The action failed ({type(exc).__name__})")
        finally:
            await self._flush(ctx)
        self._event(
            run_id,
            strategy_key,
            "info",
            f"{strategy_key} action {req.action}: {result.message}",
            {"action": req.action, "row_id": req.row_id, "ok": result.ok, "actor": actor},
        )
        return result
