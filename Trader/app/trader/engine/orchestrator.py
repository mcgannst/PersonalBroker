"""Engine orchestrator (SPEC §6): strategy → intent → risk → proposal → broker → fill → on_fill.

Strategies get a StrategyContext and return intents. The engine saves every signal with the strategy config
revision that produced it, sizes and checks it, turns it into a proposal (auto-approved or waiting for
Stephen), and feeds each fill back to the strategy that owns the position. It also persists the candidates
and notes strategies record.
"""

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
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
from trader.engine.proposals import ProposalService
from trader.engine.risk import Rejection, RiskContext, RiskManager
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock, et_date
from trader.market.data_service import MarketDataService, QuoteClient
from trader.settings_store import Market, RuntimeSettings, SettingsStore
from trader.strategies.base import (
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
SOURCE = "engine"


def _jsonable(value: Any) -> Any:
    """Decimals, dates and other odd values become strings so JSONB accepts them."""
    return json.loads(json.dumps(value, default=str))


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    signal_id: int
    intent: Intent
    status: str
    proposal_id: int | None = None
    rejection: Rejection | None = None


@dataclass
class EventResult:
    event_key: str
    session_date: date
    strategies: list[str] = field(default_factory=list)
    outcomes: list[IntentOutcome] = field(default_factory=list)


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
        for strategy, cfg in self.registry.enabled():
            event = next((e for e in strategy.schedule(self._cal) if e.key == event_key), None)
            if event is None:
                continue
            ctx = await self._context(strategy, cfg, session_date)
            intents = await strategy.on_event(ctx, event)
            self._persist(ctx, strategy, session_date)
            result.strategies.append(strategy.key)
            result.outcomes += await self._handle(strategy, cfg, intents, session_date, event_key)
        return result

    async def on_quotes(self, quotes: Sequence[QtQuote], now: datetime) -> list[FillEvent]:
        fills = self.broker.on_quotes(quotes, now)
        for fill in fills:
            await self._after_fill(fill)
        return fills

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
            with session_scope(self._factory) as s:
                log_event(
                    s,
                    self._clock,
                    "critical",
                    SOURCE,
                    f"{len(still_open)} positions still open at the end of the session (BR-42)",
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
            return
        strategy, cfg = self.registry.instance(
            key
        )  # even if disabled since: its open position still needs care
        ctx = await self._context(strategy, cfg, session_date, account)
        intents = await strategy.on_fill(ctx, fill)
        self._persist(ctx, strategy, session_date)
        await self._handle(strategy, cfg, intents, session_date, f"fill:{fill.fill_id}")

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

    def _entries_today(self, config_ids: set[int], session_date: date) -> int:
        if not config_ids:
            return 0
        with self._factory() as s:
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
        orders = [o for o in self.broker.working_orders() if o.strategy_config_id in own]
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
        if not intents:
            return []
        settings = self._settings.load()
        out: list[IntentOutcome] = []
        for intent in intents:
            now = self._clock.now()
            signal_id = self._save_signal(cfg, intent, now, session_date, event_key)
            ctx = await self._risk_context(strategy, cfg, intent, session_date, settings, now)
            decision = self._risk.evaluate(intent, ctx)
            if isinstance(decision, Rejection):
                self._amend_signal(
                    signal_id,
                    "rejection",
                    {
                        "check": decision.check,
                        "reason": decision.reason,
                        "detail": decision.detail,
                    },
                    level="warning",
                    message=f"risk rejected signal {signal_id}: {decision.check}: {decision.reason}",
                )
                out.append(IntentOutcome(signal_id, intent, "rejected_by_risk", None, decision))
                continue
            if decision.sizing:
                self._amend_signal(signal_id, "sizing", decision.sizing)
            proposal = self.proposals.create(signal_id, decision, decision.kind)
            out.append(IntentOutcome(signal_id, intent, proposal.status, proposal.id))
        return out

    def _save_signal(
        self, cfg: StrategyConfigView, intent: Intent, now: datetime, session_date: date, event_key: str
    ) -> int:
        with session_scope(self._factory) as s:
            if isinstance(intent, EnterLong):
                symbol_id: int | None = intent.symbol_id
                evidence: dict[str, Any] = _jsonable(dict(intent.evidence))
            elif isinstance(intent, Exit):
                pos = s.get(m.Position, intent.position_id)
                symbol_id = pos.symbol_id if pos else None
                evidence = {"reason": intent.reason}
            else:
                order = s.get(m.Order, intent.order_id)
                symbol_id = order.symbol_id if order else None
                evidence = {"reason": intent.reason}
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
            assert sig is not None
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
            q = (await self._data.quotes([intent.symbol_id])).get(intent.symbol_id)
            reference = q.ask if q else None
        return dataclasses.replace(
            base,
            blocking_switch=self.killswitches.blocking(self.run_id, session_date),
            daily_pnl_pct=inputs.daily_pnl_pct,
            entries_today=self._entries_today(self.registry.config_ids(strategy.key), session_date),
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
    return Engine(
        factory=core.factory,
        clock=core.clock,
        calendar=core.calendar,
        settings=core.settings,
        registry=registry,
        data=MarketDataService(core.factory, core.clock, core.calendar, client),
        catalysts=catalysts,
        broker=broker,
        proposals=ProposalService(core.factory, core.clock, core.settings, broker, run.id),
        risk=RiskManager(core.calendar),
        killswitches=KillSwitches(core.factory, core.clock),
        run_id=run.id,
    )
