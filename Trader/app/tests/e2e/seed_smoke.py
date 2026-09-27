"""Seed the throwaway smoke database for the local Playwright smoke (P4-T19, run by `docker/smoke.sh`).

    python -m tests.e2e.seed_smoke --url postgresql+psycopg://trader_smoke_app:...@127.0.0.1:15432/trader_smoke

Through the P1-P3 services where they exist (the live run and its sim account, the strategy defaults, the
proposal service), it ensures: the live run, the strategy defaults, a symbol `AAA`, a signal and ONE pending
entry proposal expiring 10 minutes from now, one closed trade (signal, proposals, orders, fills, position,
trade) on the previous session, and a few events. Idempotent: a second run refreshes the pending proposal's
expiry instead of adding another, and adds no second trade or events.

It must never run against `trader_dev`: it refuses a URL whose database is not `trader_smoke` (or a test
database passed in by pytest through `seed`). It prints ids only, never the URL.
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import OrderSpec
from trader.db import models as m
from trader.db.session import make_engine, make_session_factory
from trader.engine.proposals import ProposalService
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, Clock, RealClock
from trader.settings_store import SettingsStore
from trader.strategies.base import EnterLong
from trader.strategies.registry import StrategyRegistry

TICKER = "AAA"
TRADE_TICKER = "BBB"
PENDING_MINUTES = 10
SMOKE_DATABASE = "trader_smoke"
SOURCE = "smoke"


@dataclass(frozen=True)
class SeedResult:
    run_id: int
    proposal_id: int
    position_id: int
    trade_id: int


def _symbol(s: Session, ticker: str, questrade_id: int) -> int:
    existing = s.execute(select(m.Symbol).where(m.Symbol.ticker == ticker)).scalar_one_or_none()
    if existing is not None:
        return existing.id
    sym = m.Symbol(
        ticker=ticker, exchange="NASDAQ", questrade_id=questrade_id, currency="USD", name=f"{ticker} Inc"
    )
    s.add(sym)
    s.flush()
    return sym.id


def _config_id(s: Session, key: str = "orb_sip") -> int:
    row = (
        s.execute(
            select(m.StrategyConfig)
            .where(m.StrategyConfig.strategy_key == key)
            .order_by(m.StrategyConfig.revision.desc())
        )
        .scalars()
        .first()
    )
    if row is None:
        raise RuntimeError(f"no strategy config for {key}: the registry defaults were not ensured")
    return row.id


def _signal(s: Session, run_id: int, cfg: int, sym: int, day: date, ts: datetime) -> int:
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=day,
        event_key="orb_open",
        ts=ts,
        intent={"side": "buy", "reason": "orb_breakout"},
        evidence={"rvol": "3.2", "orb_high": "20.00", "source": SOURCE},
    )
    s.add(sig)
    s.flush()
    return sig.id


def _ensure_pending(
    factory: sessionmaker[Session], clock: Clock, settings: SettingsStore, run_id: int, broker: SimBroker
) -> int:
    """The one pending AAA entry, created by the engine's own ProposalService (creating is never guarded,
    only deciding); a re-run moves its expiry to 10 minutes from now instead of adding another."""
    now = clock.now()
    with factory() as s:
        sym = _symbol(s, TICKER, 900001)
        existing = (
            s.execute(
                select(m.Proposal)
                .join(m.Signal, m.Signal.id == m.Proposal.signal_id)
                .where(m.Proposal.run_id == run_id, m.Proposal.status == "pending", m.Signal.symbol_id == sym)
                .order_by(m.Proposal.id)
            )
            .scalars()
            .all()
        )
        if existing:
            keep = existing[0]
            keep.expires_at = now + timedelta(minutes=PENDING_MINUTES)
            for extra in existing[1:]:  # never leave more than one
                extra.status, extra.expired_at = "expired", now
            s.commit()
            return keep.id
        cfg = _config_id(s)
        signal_id = _signal(s, run_id, cfg, sym, now.astimezone(ET).date(), now)
        s.commit()
    intent = EnterLong(sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        sym, "buy", "stop", 10, stop=Decimal("20.01"), stop_loss=Decimal("19.91"),
        strategy_config_id=cfg, reason="orb_breakout",
    )  # fmt: skip
    sized = SizedOrder(intent, "entry", 10, spec, sizing={"shares": "10", "per_share_risk": "0.10"})
    service = ProposalService(factory, clock, settings, broker, run_id)
    proposal = service.create(signal_id, sized, "entry")
    with factory() as s:
        row = s.get(m.Proposal, proposal.id)
        assert row is not None
        if row.status != "pending":
            raise RuntimeError(
                f"the seeded proposal is {row.status}, not pending (approval mode must be manual)"
            )
        row.expires_at = clock.now() + timedelta(minutes=PENDING_MINUTES)
        s.commit()
    return proposal.id


def _at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=ET)


def _order(s: Session, run_id: int, sym: int, cfg: int, day: date, **kw: Any) -> m.Order:
    values: dict[str, Any] = {
        "run_id": run_id,
        "symbol_id": sym,
        "strategy_config_id": cfg,
        "tif": "day",
        "status": "filled",
        "session_date": day,
        "stale_alerted": False,
        "qty": 10,
        **kw,
    }
    values.setdefault("reason", values["purpose"])
    values.setdefault("closed_at", values["submitted_at"] + timedelta(seconds=30))
    o = m.Order(**values)
    s.add(o)
    s.flush()
    return o


def _fill(s: Session, run_id: int, order: m.Order, price: str, ts: datetime) -> None:
    s.add(
        m.Fill(
            run_id=run_id,
            order_id=order.id,
            ts=ts,
            qty=order.qty,
            price=Decimal(price),
            fees={"commission": "0.00", "total": "0.00"},
            quote_snapshot={"bid": price, "ask": price, "last": price, "last_trade_time": ts.isoformat()},
            slippage=Decimal("0.0100"),
        )
    )
    s.flush()


def _ensure_closed_trade(
    factory: sessionmaker[Session], clock: Clock, calendar: SessionCalendar, run_id: int
) -> m.Trade:
    """One closed BBB trade on the previous session with its whole chain; a re-run finds it."""
    with factory() as s:
        sym = _symbol(s, TRADE_TICKER, 900002)
        found = (
            s.execute(select(m.Trade).where(m.Trade.run_id == run_id, m.Trade.symbol_id == sym))
            .scalars()
            .first()
        )
        if found is not None:
            s.expunge(found)
            return found
        cfg = _config_id(s)
        day = calendar.previous_session(clock.now().astimezone(ET).date())
        t0 = _at(day, 9, 36)
        sig = _signal(s, run_id, cfg, sym, day, t0)
        entry_p = m.Proposal(
            run_id=run_id, signal_id=sig, kind="entry",
            order_spec={"symbol_id": sym, "side": "buy", "order_type": "stop", "stop": "21.55",
                        "stop_loss": "21.05", "reason": "orb_breakout"},
            qty=10, status="submitted", created_at=t0, expires_at=t0 + timedelta(seconds=90),
            decided_at=t0 + timedelta(seconds=12), decided_via="telegram", decided_by="telegram",
            decision_latency_ms=12000, sizing={"per_share_risk": "0.50"}, escalations=0,
        )  # fmt: skip
        s.add(entry_p)
        s.flush()
        entry_o = _order(
            s, run_id, sym, cfg, day, proposal_id=entry_p.id, side="buy", order_type="stop", purpose="entry",
            stop_price=Decimal("21.55"), stop_loss=Decimal("21.05"), submitted_at=t0 + timedelta(seconds=12),
        )  # fmt: skip
        entry_p.order_id = entry_o.id
        pos = m.Position(
            run_id=run_id, symbol_id=sym, strategy_config_id=cfg, qty=10, avg_price=Decimal("21.56"),
            stop_loss=Decimal("21.05"), planned_risk=Decimal("5.10"), session_date=day,
            opened_at=t0 + timedelta(minutes=1), closed_at=_at(day, 15, 55), entry_order_id=entry_o.id,
            unprotected_seconds=7,
        )  # fmt: skip
        s.add(pos)
        s.flush()
        entry_o.position_id = entry_p.position_id = pos.id
        exit_o = _order(
            s, run_id, sym, cfg, day, position_id=pos.id, side="sell", order_type="market", purpose="exit",
            reason="flatten_close", submitted_at=_at(day, 15, 55),
        )  # fmt: skip
        _fill(s, run_id, entry_o, "21.56", t0 + timedelta(minutes=1))
        _fill(s, run_id, exit_o, "22.81", _at(day, 15, 55))
        trade = m.Trade(
            run_id=run_id, position_id=pos.id, symbol_id=sym, session_date=day, entry_price=Decimal("21.56"),
            exit_price=Decimal("22.81"), qty=10, pnl=Decimal("12.5000"), pnl_r=Decimal("2.4500"),
            planned_risk=Decimal("5.10"), exit_reason="flatten_close", slippage_total=Decimal("0.0200"),
            fees_total=Decimal("0.0000"), opened_at=pos.opened_at, closed_at=_at(day, 15, 55),
        )  # fmt: skip
        s.add(trade)
        s.commit()
        s.refresh(trade)
        s.expunge(trade)
        return trade


def _ensure_events(factory: sessionmaker[Session], clock: Clock, run_id: int) -> None:
    with factory() as s:
        if s.execute(select(m.EventLog.id).where(m.EventLog.source == SOURCE)).first() is not None:
            return
        now = clock.now()
        for n, (level, message) in enumerate(
            (("info", "smoke stack seeded"), ("warning", "smoke warning event"), ("info", "smoke info event"))
        ):
            s.add(m.EventLog(ts=now - timedelta(minutes=3 - n), level=level, source=SOURCE, run_id=run_id,
                             message=message, data={"seed": True}))  # fmt: skip
        s.commit()


def seed(factory: sessionmaker[Session], clock: Clock | None = None) -> SeedResult:
    """Ensure the smoke rows (idempotent). Returns the ids the smoke run uses. The SimBroker is built as
    `runtime.build_sim_broker` builds it (without needing a Core)."""
    clock = clock or RealClock()
    calendar = SessionCalendar()
    settings = SettingsStore(factory, now=clock.now)
    StrategyRegistry(factory, clock).ensure_defaults(actor=SOURCE)
    loaded = settings.load()
    run = get_live_run(factory, clock, loaded)
    broker = SimBroker(
        factory,
        clock,
        Ledger(calendar),
        QuoteFillModel(FillParams.from_settings(loaded)),
        run.id,
        currency=loaded.account_currency,
        calendar=calendar,
        settings=settings.load,
    )
    proposal_id = _ensure_pending(factory, clock, settings, run.id, broker)
    trade = _ensure_closed_trade(factory, clock, calendar, run.id)
    _ensure_events(factory, clock, run.id)
    return SeedResult(run.id, proposal_id, trade.position_id, trade.id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the throwaway smoke database (never trader_dev).")
    parser.add_argument("--url", required=True, help="the smoke database URL (app role)")
    args = parser.parse_args(argv)
    if make_url(args.url).database != SMOKE_DATABASE:
        parser.error(f"refusing to seed a database other than {SMOKE_DATABASE}")
    engine = make_engine(args.url)
    try:
        result = seed(make_session_factory(engine))
    finally:
        engine.dispose()
    print(
        f"seeded: run {result.run_id}, pending proposal {result.proposal_id}, "
        f"position {result.position_id}, trade {result.trade_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
