"""Engine orchestrator (SPEC §6): strategy → intent → risk → proposal → broker → fill → on_fill.

Strategies get a StrategyContext and return intents. The engine saves every signal with the strategy config
revision that produced it, sizes and checks it, turns it into a proposal (auto-approved or waiting for
Stephen), and feeds each fill back to the strategy that owns the position. It also persists the candidates
and notes strategies record.

Isolation (P2-T13 fix round 1): one failing strategy, intent or fill follow-up never stops the others. Each
failure is logged loudly and the loop carries on. Every saved signal ends in exactly one of a rejection
(`evidence.rejection`), a proposal, or an error (`evidence.error`).
"""

import dataclasses
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, assert_never, cast

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.questrade.client import QuestradeApiError
from trader.adapters.questrade.models import QtQuote
from trader.bootstrap import Core
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import AccountState, FillEvent, PositionView
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.proposals import ProposalService, ProposalStatus
from trader.engine.risk import Rejection, RiskCheck, RiskContext, RiskManager, SizedOrder
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.data_service import MarketDataService, QuoteClient
from trader.market.types import Candle
from trader.settings_store import Market, RuntimeSettings, SettingsStore
from trader.strategies.base import (
    Cancel,
    CatalystSource,
    EnterLong,
    Exit,
    Intent,
    MarketDataView,
    Strategy,
    StrategyContext,
    intent_to_json,
)
from trader.strategies.registry import StrategyConfigView, StrategyRegistry

LIVE_ENTRY_STATUSES = ("pending", "approved", "auto_approved", "submitted")
UNDECIDED_STATUSES = ("pending", "approved", "auto_approved")  # waiting for, or on the way to, the broker
SOURCE = "engine"
# pg_advisory_xact_lock key prefix: the duplicate/entries_today checks and the proposal they allow are
# serialised per run, so two processes (worker + cron backup) can't both pass them.
INTENT_LOCK = "engine.intents"

_log = logging.getLogger(__name__)

# the orchestrator's own check (fix-round ruling), run under the intent lock before RiskManager.evaluate
DUPLICATE_SYMBOL: RiskCheck = "duplicate_symbol"
OutcomeStatus = ProposalStatus | Literal["rejected_by_risk", "skipped_duplicate", "error"]


def _jsonable(value: Any) -> Any:
    """Decimals, dates and other odd values become strings so JSONB accepts them."""
    return json.loads(json.dumps(value, default=str))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    """What became of one intent.

    `status` is the proposal's status, `rejected_by_risk` (the rejection is on the signal),
    `skipped_duplicate` (a re-fired Exit/Cancel whose live proposal or order already exists: no signal
    is saved), or `error` (`evidence.error` is on the signal when it was saved). `signal_id` is None only
    when no signal was saved.
    """

    signal_id: int | None
    intent: Intent
    status: OutcomeStatus
    proposal_id: int | None = None
    rejection: Rejection | None = None
    error: str | None = None


@dataclass
class EventResult:
    event_key: str
    session_date: date
    strategies: list[str] = field(default_factory=list)
    outcomes: list[IntentOutcome] = field(default_factory=list)
    # strategies whose on_event (or its handling) raised, or that own positions but could not start
    failed: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        *,
        factory: sessionmaker[Session],
        clock: Clock,
        calendar: SessionCalendar,
        settings: SettingsStore,
        registry: StrategyRegistry,
        data: MarketDataView,
        catalysts: CatalystSource,
        broker: SimBroker,
        proposals: ProposalService,
        risk: RiskManager,
        killswitches: KillSwitches,
        run_id: int,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._cal = calendar
        self._settings = settings
        self.registry = registry
        self._data = data
        self._catalysts = catalysts
        self.broker = broker
        self.proposals = proposals
        self._risk = risk
        self.killswitches = killswitches
        self.run_id = run_id

    # --- public entry points ---------------------------------------------------------------------------
    async def run_event(self, event_key: str, session_date: date) -> EventResult:
        result = EventResult(event_key, session_date)
        for strategy, cfg, exits_only in self._event_strategies(event_key, result):
            try:
                event = next((e for e in strategy.schedule(self._cal) if e.key == event_key), None)
                if event is None:
                    continue
                ctx = await self._context(strategy, cfg, session_date)
                intents = await strategy.on_event(ctx, event)
                self._persist(ctx, strategy, session_date)
                if exits_only:  # a disabled owner may only wind down: no new entries
                    intents = [i for i in intents if not isinstance(i, EnterLong)]
            except Exception as exc:  # one broken plug-in must not stop the others (SPEC §5.1)
                result.failed.append(strategy.key)
                self._alert(
                    "error",
                    f"strategy {strategy.key} failed on event {event_key}: {_describe(exc)}",
                    {
                        "strategy": strategy.key,
                        "strategy_config_id": cfg.id,
                        "event_key": event_key,
                        "session_date": session_date,
                        "error": _describe(exc),
                    },
                )
                continue
            result.strategies.append(strategy.key)
            # _handle isolates every intent itself: it never raises
            result.outcomes += await self._handle(strategy, cfg, intents, session_date, event_key)
        return result

    def _event_strategies(
        self, event_key: str, result: EventResult
    ) -> list[tuple[Strategy, StrategyConfigView, bool]]:
        """Every enabled strategy, plus (exits only) any disabled one that still owns an open position or a
        working order: its flatten, stop and cancel events must still run (BR-42), or a strategy disabled
        mid-session would leave its position open overnight."""
        out = [(strategy, cfg, False) for strategy, cfg in self.registry.enabled()]
        running = {strategy.key for strategy, _, _ in out}
        owner_ids = {p.strategy_config_id for p in self.broker.open_positions()} | {
            o.strategy_config_id for o in self.broker.working_orders()
        }
        for config_id in sorted(i for i in owner_ids if i is not None):
            key: str | None = None
            try:
                key = self.registry.config_key(config_id)
                if key is None or key in running:
                    continue
                running.add(key)
                strategy, cfg = self.registry.instance(key)
            except Exception as exc:
                result.failed.append(key or f"config {config_id}")
                self._alert(
                    "critical",
                    f"strategy {key} (config {config_id}) owns open positions or orders but could not "
                    f"start for event {event_key}: {_describe(exc)}",
                    {"strategy": key, "strategy_config_id": config_id, "event_key": event_key},
                )
                continue
            out.append((strategy, cfg, True))
        return out

    async def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]:
        fills = self.broker.on_quotes(quotes, now)
        await self._follow_up(fills)
        return fills

    async def _follow_up(self, fills: Sequence[FillEvent]) -> None:
        """Each fill's follow-up (`_after_fill`), isolated: one failing follow-up never loses the others'."""
        for fill in fills:
            # the broker has already booked the fill: a failing follow-up must not lose the others'
            try:
                await self._after_fill(fill)
            except Exception as exc:
                owner = (
                    self._config_key_or_none(fill.strategy_config_id)
                    if fill.strategy_config_id is not None
                    else None
                )
                unprotected = " (its position may have no protective stop)" if fill.purpose == "entry" else ""
                self._alert(
                    "critical",
                    f"follow-up of {fill.purpose} fill {fill.fill_id} for position {fill.position_id} "
                    f"(strategy {owner}) failed{unprotected}: {_describe(exc)}",
                    {
                        "fill_id": fill.fill_id,
                        "position_id": fill.position_id,
                        "order_id": fill.order_id,
                        "purpose": fill.purpose,
                        "strategy": owner,
                        "strategy_config_id": fill.strategy_config_id,
                        "error": _describe(exc),
                    },
                )

    async def on_candles(self, candles: Mapping[int, Candle], now: datetime) -> list[FillEvent]:
        """Replay (P5-T4): the candle twin of `on_quotes` (`broker.on_candles`, each fill's follow-up), then
        the same-bar worst-case pass for this call's entry fills. Returns every fill in order.

        Same-bar worst case (SPEC §7.4): an entry filled in bar `b` gets its protective stop from the normal
        follow-up (submitted at `now` = `b.end`). That stop is then tried against `b` itself, reopened at the
        entry's fill price, so a bar that touched both the entry and the stop is counted as entered and then
        stopped out, never the favourable order."""
        fills = self.broker.on_candles(candles, now)
        await self._follow_up(fills)
        out = list(fills)
        entries = [f for f in fills if f.purpose == "entry" and f.symbol_id in candles]
        if not entries:
            return out
        stops = {p.id: p.stop_order_id for p in self.broker.open_positions()}
        working = {o.id for o in self.broker.working_orders()}
        for fill in entries:
            stop_id = stops.get(fill.position_id)
            if stop_id is None or stop_id not in working:
                continue
            reopened = dataclasses.replace(candles[fill.symbol_id], open=fill.price)
            same_bar = self.broker.on_candles({fill.symbol_id: reopened}, now, orders=[stop_id])
            await self._follow_up(same_bar)
            out += same_bar
        return out

    async def poll_quotes(self) -> list[FillEvent]:
        ids = self.broker.working_symbol_ids()
        if not ids:
            return []
        quotes = await self._data.quotes(ids)
        return await self.on_quotes(list(quotes.values()), self._clock.now())

    async def tick(self, now: datetime) -> None:
        self.proposals.expire_due(now)
        self.proposals.escalate_unprotected(now)

    async def end_of_session(self, session_date: date) -> list[PositionView]:
        self.broker.end_of_session(session_date)
        still_open = self.broker.open_positions()
        if still_open:
            n = len(still_open)
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "critical",
                    SOURCE,
                    f"{n} position{'' if n == 1 else 's'} still open at the end of the session (BR-42)",
                    {"position_ids": [p.id for p in still_open]},
                    run_id=self.run_id,
                )
        self.broker.snapshot_equity(self._clock.now(), await self._account(session_date))
        return still_open

    # --- fills ---------------------------------------------------------------------------------------------
    async def _after_fill(self, fill: FillEvent) -> None:
        session_date = et_date(fill.ts)
        settings = self._settings.load()
        account = await self._account(session_date, settings)
        self.broker.snapshot_equity(fill.ts, account)
        self._check_switches(session_date, account, settings)
        key = (
            self.registry.config_key(fill.strategy_config_id) if fill.strategy_config_id is not None else None
        )
        if key is None:
            if fill.purpose == "entry":
                self._alert(
                    "critical",
                    f"entry fill {fill.fill_id} for position {fill.position_id} has no owning strategy "
                    f"(config {fill.strategy_config_id}): no on_fill, so no protective stop",
                    {
                        "fill_id": fill.fill_id,
                        "position_id": fill.position_id,
                        "order_id": fill.order_id,
                        "strategy_config_id": fill.strategy_config_id,
                    },
                )
            return
        # even if disabled since: its open position still needs care
        strategy, cfg = self.registry.instance(key)
        ctx = await self._context(strategy, cfg, session_date, account)
        intents = await strategy.on_fill(ctx, fill)
        self._persist(ctx, strategy, session_date)
        await self._handle(strategy, cfg, intents, session_date, f"fill:{fill.fill_id}")

    def _config_key_or_none(self, config_id: int) -> str | None:
        """For error messages only: the owner's key, or None if even that lookup fails."""
        try:
            return self.registry.config_key(config_id)
        except Exception:
            return None

    def _alert(self, level: str, message: str, data: dict[str, Any]) -> None:
        """An engine event in its own transaction. Never raises: it is the last line of error reporting."""
        try:
            with session_scope(self._factory) as s:
                log_event(s, self._clock, level, SOURCE, message, _jsonable(data), run_id=self.run_id)
        except Exception:
            _log.exception("could not write the %s event: %s", level, message)

    # --- context and account --------------------------------------------------------------------------
    async def _account(self, session_date: date, settings: RuntimeSettings | None = None) -> AccountState:
        settings = settings or self._settings.load()
        positions = self.broker.open_positions()
        marks: dict[int, Decimal] = {}
        if positions:
            try:
                quotes = await self._data.quotes(sorted({p.symbol_id for p in positions}))
            except QuestradeApiError:
                quotes = {}
            marks = {sid: q.last for sid, q in quotes.items() if q.last is not None}
        return self.broker.account_state(session_date, marks, settings.cash_account_mode)

    def _check_switches(
        self, session_date: date, account: AccountState, settings: RuntimeSettings
    ) -> KillSwitchInputs:
        session_open = (
            self._cal.session_open(session_date) if self._cal.is_session(session_date) else self._clock.now()
        )
        inputs = self.killswitches.inputs(self.run_id, session_date, account, session_open)
        self.killswitches.evaluate(self.run_id, session_date, inputs, settings)
        return inputs

    def _entry_config_ids(self) -> set[int]:
        ids: set[int] = set()
        for key in self.registry.keys():
            if self.registry.plugin_class(key).kind == "entry":
                ids |= self.registry.config_ids(key)
        return ids

    def _entries_today(self, config_ids: set[int], session_date: date, s: Session | None = None) -> int:
        """Pass the intent-lock session `s` when the count gates a new entry (see `_decide_and_create`)."""
        if not config_ids:
            return 0
        if s is None:
            with self._factory() as own:
                return self._entries_today(config_ids, session_date, own)
        return int(
            s.execute(
                select(func.count(m.Proposal.id))
                .join(m.Signal, m.Signal.id == m.Proposal.signal_id)
                .where(
                    m.Proposal.run_id == self.run_id,
                    m.Proposal.kind == "entry",
                    m.Proposal.status.in_(LIVE_ENTRY_STATUSES),
                    m.Signal.session_date == session_date,
                    m.Signal.strategy_config_id.in_(config_ids),
                )
            ).scalar_one()
        )

    async def _context(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        session_date: date,
        account: AccountState | None = None,
    ) -> StrategyContext:
        own = self.registry.config_ids(strategy.key)
        visible_ids = self._entry_config_ids() if strategy.kind == "overlay" else own
        positions = [p for p in self.broker.open_positions() if p.strategy_config_id in visible_ids]
        position_ids = {p.id for p in positions}
        # An overlay also sees the working orders of the positions it watches (an exit already on its way
        # carries the entry strategy's config id), so it doesn't propose a second exit (SPEC §5.3).
        orders = [
            o
            for o in self.broker.working_orders()
            if o.strategy_config_id in own or (strategy.kind == "overlay" and o.position_id in position_ids)
        ]
        return StrategyContext(
            clock=self._clock,
            calendar=self._cal,
            session_date=session_date,
            data=self._data,
            catalysts=self._catalysts,
            params=strategy.params,
            strategy_config_id=cfg.id,
            positions=positions,
            working_orders=orders,
            account=account or await self._account(session_date),
            entries_today=self._entries_today(own, session_date),
        )

    def _persist(self, ctx: StrategyContext, strategy: Strategy, session_date: date) -> None:
        if not ctx.candidates and not ctx.notes:
            return
        now = self._clock.now()
        with session_scope(self._factory) as s:
            for c in ctx.candidates:
                stmt = pg_insert(m.Candidate).values(
                    run_id=self.run_id,
                    session_date=session_date,
                    strategy_key=strategy.key,
                    symbol_id=c.symbol_id,
                    rvol=c.rvol,
                    rank=c.rank,
                    candle=_jsonable(c.candle),
                    passed=c.passed,
                    reject_reason=c.reject_reason,
                    data=_jsonable(c.data),
                    created_at=now,
                )
                s.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_candidates_run_session_strategy_symbol",
                        set_={
                            k: stmt.excluded[k]
                            for k in (
                                "rvol",
                                "rank",
                                "candle",
                                "passed",
                                "reject_reason",
                                "data",
                                "created_at",
                            )
                        },
                    )
                )
            for n in ctx.notes:
                log_event(
                    s,
                    self._clock,
                    n.level,
                    f"strategy.{strategy.key}",
                    n.message,
                    _jsonable(n.data),
                    self.run_id,
                )

    # --- intents --------------------------------------------------------------------------------------
    async def _handle(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        intents: Sequence[Intent],
        session_date: date,
        event_key: str,
    ) -> list[IntentOutcome]:
        """Handle each intent on its own: a failing intent never stops its siblings, and this never raises."""
        if not intents:
            return []
        out: list[IntentOutcome] = []
        try:
            settings = self._settings.load()
        except Exception as exc:
            for intent in intents:
                out.append(self._intent_failed(strategy, intent, None, event_key, exc))
            return out
        for intent in intents:
            out.append(await self._handle_one(strategy, cfg, intent, session_date, event_key, settings))
        return out

    async def _handle_one(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        intent: Intent,
        session_date: date,
        event_key: str,
        settings: RuntimeSettings,
    ) -> IntentOutcome:
        """One intent → exactly one of: skipped (no signal), rejection, proposal, or error.

        The awaits (account marks, reference quote) happen first. Then, under a per-run transaction-level
        advisory lock, the duplicate checks, the `entries_today` count, the signal and the proposal happen
        with no await in between, so two processes can't both pass the checks for the same symbol or slot.
        """
        signal_id: int | None = None
        try:
            now = self._clock.now()
            ctx = await self._risk_context(strategy, cfg, intent, session_date, settings, now)
            with self._factory() as lock, lock.begin():
                lock.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                    {"k": f"{INTENT_LOCK}:{self.run_id}"},
                )
                live = self._live_duplicate(lock, intent)
                if live is not None:
                    self._alert(
                        "info",
                        f"strategy {strategy.key}: skipped a re-fired {type(intent).__name__} at "
                        f"{event_key}: {live['what']} already exists",
                        {"strategy": strategy.key, "event_key": event_key, "intent": repr(intent), **live},
                    )
                    return IntentOutcome(None, intent, "skipped_duplicate")
                signal_id = self._save_signal(cfg, intent, now, session_date, event_key)
                decision: SizedOrder | Rejection
                if isinstance(intent, EnterLong):
                    entries = self._entries_today(self.registry.config_ids(strategy.key), session_date, lock)
                    decision = self._duplicate_symbol(lock, intent) or self._risk.evaluate(
                        intent, dataclasses.replace(ctx, entries_today=entries)
                    )
                else:
                    decision = self._risk.evaluate(intent, ctx)
                if isinstance(decision, Rejection):
                    self._reject(signal_id, intent, decision)
                    return IntentOutcome(signal_id, intent, "rejected_by_risk", rejection=decision)
                if decision.sizing:
                    self._amend_signal(signal_id, "sizing", decision.sizing)
                proposal = self.proposals.create(signal_id, decision, decision.kind)
                return IntentOutcome(signal_id, intent, cast(ProposalStatus, proposal.status), proposal.id)
        except Exception as exc:
            return self._intent_failed(strategy, intent, signal_id, event_key, exc)

    def _intent_failed(
        self,
        strategy: Strategy,
        intent: Intent,
        signal_id: int | None,
        event_key: str,
        exc: Exception,
    ) -> IntentOutcome:
        """Record `evidence.error` on the signal (when saved) and log it.

        A failed stop/exit/cancel is critical: its position may be trapped or unprotected."""
        error = _describe(exc)
        if signal_id is not None:
            try:
                self._amend_signal(signal_id, "error", {"type": type(exc).__name__, "message": str(exc)})
            except Exception:
                _log.exception("could not record the error on signal %s", signal_id)
        kind = type(intent).__name__
        self._alert(
            "error" if isinstance(intent, EnterLong) else "critical",
            f"strategy {strategy.key}: {kind} intent at {event_key} failed (signal {signal_id}): {error}",
            {
                "strategy": strategy.key,
                "event_key": event_key,
                "signal_id": signal_id,
                "intent": repr(intent),
                "error": error,
            },
        )
        return IntentOutcome(signal_id, intent, "error", error=error)

    def _reject(self, signal_id: int, intent: Intent, rejection: Rejection) -> None:
        if isinstance(intent, EnterLong):
            level, why = "warning", ""
        else:  # a stop, exit or cancel that can't go in leaves a trapped or unprotected position
            level, why = "critical", " (the position may be trapped or unprotected)"
        self._amend_signal(
            signal_id,
            "rejection",
            {"check": rejection.check, "reason": rejection.reason, "detail": rejection.detail},
            level=level,
            message=f"risk rejected signal {signal_id}: {rejection.check}: {rejection.reason}{why}",
        )

    def _duplicate_symbol(self, s: Session, intent: EnterLong) -> Rejection | None:
        """Orchestrator ruling (fix round 1): one live entry per symbol per run, whatever the strategy."""
        sid = intent.symbol_id
        position_id = s.execute(
            select(m.Position.id)
            .where(
                m.Position.run_id == self.run_id, m.Position.symbol_id == sid, m.Position.closed_at.is_(None)
            )
            .limit(1)
        ).scalar_one_or_none()
        proposal_id = s.execute(
            select(m.Proposal.id)
            .join(m.Signal, m.Signal.id == m.Proposal.signal_id)
            .where(
                m.Proposal.run_id == self.run_id,
                m.Proposal.kind == "entry",
                m.Proposal.status.in_(UNDECIDED_STATUSES),
                m.Signal.symbol_id == sid,
            )
            .limit(1)
        ).scalar_one_or_none()
        order_id = s.execute(
            select(m.Order.id)
            .where(
                m.Order.run_id == self.run_id,
                m.Order.purpose == "entry",
                m.Order.status == "working",
                m.Order.symbol_id == sid,
            )
            .limit(1)
        ).scalar_one_or_none()
        found = {
            k: v
            for k, v in (("position_id", position_id), ("proposal_id", proposal_id), ("order_id", order_id))
            if v is not None
        }
        if not found:
            return None
        what = ", ".join(
            label
            for label, v in (
                ("an open position", position_id),
                ("a pending entry proposal", proposal_id),
                ("a working entry order", order_id),
            )
            if v is not None
        )
        return Rejection(intent, DUPLICATE_SYMBOL, f"symbol {sid} already has {what}", found)

    def _live_duplicate(self, s: Session, intent: Intent) -> dict[str, Any] | None:
        """A re-fired market Exit or Cancel whose earlier proposal or order is still live.

        Stop exits are never skipped: a new stop replaces the working one (trailing stops).
        """
        if isinstance(intent, Cancel):
            proposal_id = s.execute(
                select(m.Proposal.id)
                .where(
                    m.Proposal.run_id == self.run_id,
                    m.Proposal.kind == "cancel",
                    m.Proposal.cancel_order_id == intent.order_id,
                    m.Proposal.status.in_(LIVE_ENTRY_STATUSES),
                )
                .limit(1)
            ).scalar_one_or_none()
            if proposal_id is None:
                return None
            return {"what": f"cancel proposal {proposal_id}", "proposal_id": proposal_id}
        if not isinstance(intent, Exit) or intent.order_type != "market":
            return None
        proposal_id = s.execute(
            select(m.Proposal.id)
            .outerjoin(m.Order, m.Order.id == m.Proposal.order_id)
            .where(
                m.Proposal.run_id == self.run_id,
                m.Proposal.kind == "exit",
                m.Proposal.position_id == intent.position_id,
                or_(
                    m.Proposal.status.in_(UNDECIDED_STATUSES),
                    # submitted counts while its order is working or filled; a cancelled one can be retried
                    and_(m.Proposal.status == "submitted", m.Order.status.in_(("working", "filled"))),
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if proposal_id is not None:
            return {"what": f"exit proposal {proposal_id}", "proposal_id": proposal_id}
        order_id = s.execute(
            select(m.Order.id)
            .where(
                m.Order.run_id == self.run_id,
                m.Order.purpose == "exit",
                m.Order.position_id == intent.position_id,
                m.Order.status == "working",
            )
            .limit(1)
        ).scalar_one_or_none()
        if order_id is not None:
            return {"what": f"working exit order {order_id}", "order_id": order_id}
        return None

    def _save_signal(
        self, cfg: StrategyConfigView, intent: Intent, now: datetime, session_date: date, event_key: str
    ) -> int:
        with session_scope(self._factory) as s:
            symbol_id: int | None
            evidence: dict[str, Any]
            if isinstance(intent, EnterLong):
                symbol_id = intent.symbol_id
                evidence = _jsonable(dict(intent.evidence))
            elif isinstance(intent, Exit):
                pos = s.get(m.Position, intent.position_id)
                symbol_id = pos.symbol_id if pos else None
                evidence = {"reason": intent.reason}
            elif isinstance(intent, Cancel):
                order = s.get(m.Order, intent.order_id)
                symbol_id = order.symbol_id if order else None
                evidence = {"reason": intent.reason}
            else:
                assert_never(intent)
            sig = m.Signal(
                run_id=self.run_id,
                strategy_config_id=cfg.id,
                symbol_id=symbol_id,
                session_date=session_date,
                event_key=event_key,
                ts=now,
                intent=intent_to_json(intent),
                evidence=evidence,
            )
            s.add(sig)
            s.flush()
            return sig.id

    def _amend_signal(
        self,
        signal_id: int,
        key: str,
        value: dict[str, Any],
        level: str | None = None,
        message: str | None = None,
    ) -> None:
        with session_scope(self._factory) as s:
            sig = s.get(m.Signal, signal_id, with_for_update=True)
            if sig is None:
                raise LookupError(f"signal {signal_id} not found")
            sig.evidence = {**sig.evidence, key: _jsonable(value)}
            if level is not None and message is not None:
                log_event(
                    s,
                    self._clock,
                    level,
                    "risk",
                    message,
                    {"signal_id": signal_id, key: _jsonable(value)},
                    self.run_id,
                )

    def _market_of(self, symbol_id: int) -> Market | None:
        with self._factory() as s:
            currency = s.execute(
                select(m.Symbol.currency).where(m.Symbol.id == symbol_id)
            ).scalar_one_or_none()
        if currency == "USD":
            return "US"
        if currency == "CAD":
            return "TSX"
        return None

    async def _risk_context(
        self,
        strategy: Strategy,
        cfg: StrategyConfigView,
        intent: Intent,
        session_date: date,
        settings: RuntimeSettings,
        now: datetime,
    ) -> RiskContext:
        account = await self._account(session_date, settings)
        base = RiskContext(
            now=now,
            session_date=session_date,
            settings=settings,
            account=account,
            positions={p.id: p for p in self.broker.open_positions()},
            orders={o.id: o for o in self.broker.working_orders()},
            strategy_config_id=cfg.id,
        )
        if not isinstance(intent, EnterLong):
            return base  # exits, stops and cancels are never checked (Review Focus 5)
        inputs = self._check_switches(session_date, account, settings)
        reference = None
        if intent.order_type == "market":
            try:
                q = (await self._data.quotes([intent.symbol_id])).get(intent.symbol_id)
            except QuestradeApiError:
                q = None  # no reference price: risk rejects it as "no entry price to size from"
            reference = q.ask if q else None
        return dataclasses.replace(
            base,
            blocking_switch=self.killswitches.blocking(self.run_id, session_date),
            daily_pnl_pct=inputs.daily_pnl_pct,
            # entries_today is counted again under the intent lock in _handle_one; this is only a default
            entries_today=0,
            # Strategy.params is a plug-in's own BaseModel: max_positions is not part of the protocol (P2-T6)
            max_positions=int(getattr(strategy.params, "max_positions", 1)),
            symbol_market=self._market_of(intent.symbol_id),
            reference_price=reference,
        )


def build_engine(core: Core, client: QuoteClient, catalysts: CatalystSource) -> Engine:
    """The live engine. P3's worker builds one per session, so fill-model settings apply from the next.

    `client` is owned by the caller: pass ONE long-lived QuestradeClient per process (not one per session) so
    its access-token cache is shared by every engine, job and command in that process.
    """
    settings = core.settings.load()
    run = get_live_run(core.factory, core.clock, settings)
    registry = StrategyRegistry(core.factory, core.clock)
    registry.ensure_defaults()
    broker = SimBroker(
        core.factory,
        core.clock,
        Ledger(core.calendar),
        QuoteFillModel(FillParams.from_settings(settings)),
        run.id,
        currency=settings.account_currency,
        calendar=core.calendar,
        settings=core.settings.load,  # the entry cutoff follows no_entry_before_close_minutes live
    )
    killswitches = KillSwitches(core.factory, core.clock)
    return Engine(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=core.settings,
        registry=registry,
        data=MarketDataService(core.factory, core.clock, core.calendar, client),
        catalysts=catalysts,
        broker=broker,
        # an entry approved while a kill switch is tripped (or /pause is on) is never submitted (SPEC §6.3)
        proposals=ProposalService(
            core.factory,
            core.clock,
            core.settings,
            broker,
            run.id,
            entry_blocked=killswitches.entry_guard(),
        ),
        risk=RiskManager(core.calendar),
        killswitches=killswitches,
        run_id=run.id,
    )
