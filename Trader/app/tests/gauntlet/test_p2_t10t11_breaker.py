"""P2-B3 gauntlet breaker: risk manager, kill switches (P2-T10) and the proposal service (P2-T11).

Fakes and the testcontainers database only.
Targets SPEC §6.1-§6.3, BR-30/31/33/40/41/42 and Review Focus 2, 4, 5.
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import AccountState, OrderSpec, OrderView, PositionView
from trader.db import models as m
from trader.engine.killswitch import SWITCHES, KillSwitches, KillSwitchInputs
from trader.engine.proposals import DecisionResult, ProposalService
from trader.engine.risk import Rejection, RiskContext, RiskManager, SizedOrder
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.base import Cancel, EnterLong, Exit

CAL = SessionCalendar()
RISK = RiskManager(CAL)
DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET
EARLY = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close (18:00 UTC)
THANKSGIVING = date(2026, 11, 26)


# --------------------------------------------------------------------------------------------- pure helpers
def acct(equity: str = "720", buying_power: str = "720") -> AccountState:
    bp = Decimal(buying_power)
    return AccountState(bp, bp, bp, Decimal(0), Decimal(equity))


POS = PositionView(3, 7, 1, 35, Decimal("20.02"), Decimal("19.91"), T, DAY, None, T, 0)
ORDER = OrderView(9, 7, "buy", "stop", "entry", 35, Decimal("20.01"), None, "working", None, 1, 4, T)


def ctx(**over: Any) -> RiskContext:
    base = RiskContext(
        now=T,
        session_date=DAY,
        settings=RuntimeSettings(),
        account=acct(),
        positions={POS.id: POS},
        orders={ORDER.id: ORDER},
        strategy_config_id=1,
        symbol_market="US",
    )
    return replace(base, **over)


def long(
    entry: str = "20.01", stop_loss: str = "19.91", order_type: Literal["stop", "market"] = "stop"
) -> EnterLong:
    stop = Decimal(entry) if order_type == "stop" else None
    return EnterLong(7, order_type, stop, None, Decimal(stop_loss), "orb_breakout", {})


# ------------------------------------------------------------------------------------------ 1. sizing edges
@pytest.mark.parametrize(
    ("intent", "over", "expect"),
    [
        # stop == entry would divide by zero: must be a clean rejection, never ZeroDivisionError
        (long("20.01", "20.01"), {}, ("invalid", None)),
        # a "stop" above the entry of a long is backwards
        (long("20.01", "20.50"), {}, ("invalid", None)),
        # tiny equity: risk $0.0144 / 0.10 -> 0 shares; SIZECAP: the 10% cap ($0.072) buys no share first
        (long(), {"account": acct(equity="0.72", buying_power="720")}, ("position_cap", None)),
        # fractional floor: 1000 x 2% = 20 / 0.07 = 285.71 -> 285 (risk);
        # SIZECAP: the 10% cap 100 / (20.00 x 1.005) = 4.98 -> 4 binds
        (long("20.00", "19.93"), {"account": acct("1000", "100000")}, (None, 4)),
        # cash exactly 35 x 20.00 x 1.005 = 703.5 -> 35, not 34
        (long("20.00", "19.93"), {"account": acct("10000", "703.5")}, (None, 35)),
        # one ten-thousandth less cash -> 34
        (long("20.00", "19.93"), {"account": acct("10000", "703.4999")}, (None, 34)),
        # buying power below the price of one share -> zero shares, not a negative or fractional order
        (long(), {"account": acct("720", "20")}, ("zero_shares", None)),
        # risk_pct at its upper bound (10%): 72 / 0.10 = 720; SIZECAP: the 10% cap 72 / 20.11 -> 3 binds
        (
            long(),
            {"account": acct("720", "1000000"), "settings": RuntimeSettings(risk_pct=Decimal("0.10"))},
            (None, 3),
        ),
        # ... and with the cap off (1 = all of equity) the cap is 720 / 20.11 = 35.8 -> 35
        (
            long(),
            {
                "account": acct("720", "1000000"),
                "settings": RuntimeSettings(risk_pct=Decimal("0.10"), max_position_pct=Decimal("1")),
            },
            (None, 35),
        ),
        # risk_pct near its lower bound: 0.072 / 0.10 -> 0
        (long(), {"settings": RuntimeSettings(risk_pct=Decimal("0.0001"))}, ("zero_shares", None)),
        # market entry with no reference price has nothing to size from
        (long(order_type="market"), {}, ("invalid", None)),
        # market entry sizes from the reference price (SIZECAP: the 10% cap, 4, binds as above)
        (
            long("20.00", "19.93", "market"),
            {"account": acct("1000", "100000"), "reference_price": Decimal("20.00")},
            (None, 4),
        ),
    ],
)
def test_sizing_edges_never_crash_and_floor_correctly(
    intent: EnterLong, over: dict[str, Any], expect: tuple[str | None, int | None]
) -> None:
    out = RISK.evaluate(intent, ctx(**over))
    check, qty = expect
    if check is not None:
        assert isinstance(out, Rejection) and out.check == check, out
        if check == "zero_shares":
            assert out.detail["shares"] == "0" and "limited_by" in out.detail
        return
    assert isinstance(out, SizedOrder) and out.qty == qty, out
    assert out.spec is not None and out.spec.qty == qty and out.spec.side == "buy"
    assert int(out.sizing["shares"]) == qty
    # SIZECAP: the binding limit is one of the three whose shares equal the order's
    limits = {k: int(out.sizing[f"shares_{k}"]) for k in ("risk", "cash", "cap")}
    assert min(limits.values()) == qty
    assert out.sizing["limited_by"] in [k for k, v in limits.items() if v == qty]


def test_risk_pct_bounds_are_enforced() -> None:
    RuntimeSettings(risk_pct=Decimal("0.10"))
    for bad in ("0", "-0.01", "0.1001"):
        with pytest.raises(ValidationError):
            RuntimeSettings(risk_pct=Decimal(bad))


# -------------------------------------------------------------------------- 2. SPEC §6.1 check order + hours
def test_checks_run_in_spec_order_and_hours_follow_early_close_and_holidays() -> None:
    after_close = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    c = ctx(
        blocking_switch="expectancy",
        daily_pnl_pct=Decimal("-0.05"),  # exactly at the limit: blocked
        entries_today=1,
        now=after_close,
        account=acct("720", "0"),
        symbol_market="TSX",
    )
    fixes: list[tuple[str, dict[str, Any]]] = [
        ("kill_switch", {"blocking_switch": None}),
        ("daily_loss", {"daily_pnl_pct": Decimal("-0.049999")}),
        ("max_positions", {"entries_today": 0}),
        ("market_hours", {"now": T}),
        ("settled_cash", {"account": acct()}),
        ("market_enabled", {"symbol_market": "US"}),
    ]
    seen = []
    for expected, fix in fixes:
        out = RISK.evaluate(long(), c)
        assert isinstance(out, Rejection), out
        seen.append(out.check)
        assert out.check == expected, seen
        c = replace(c, **fix)
    assert isinstance(RISK.evaluate(long(), c), SizedOrder)

    def at(d: date, h: int, mi: int, s: int = 0) -> SizedOrder | Rejection:
        return RISK.evaluate(
            long(), replace(c, session_date=d, now=datetime(d.year, d.month, d.day, h, mi, s, tzinfo=UTC))
        )

    # early close 13:00 ET -> entries end at 12:30 ET (17:30 UTC); open 09:30 ET = 14:30 UTC (EST)
    assert isinstance(at(EARLY, 14, 29, 59), Rejection)
    assert isinstance(at(EARLY, 14, 30), SizedOrder)
    assert isinstance(at(EARLY, 17, 29, 59), SizedOrder)
    for h, mi in ((17, 30), (18, 0), (19, 0), (20, 0)):
        out = at(EARLY, h, mi)
        assert isinstance(out, Rejection) and out.check == "market_hours", (h, mi)
    out = at(THANKSGIVING, 15, 0)
    assert isinstance(out, Rejection) and out.check == "market_hours"
    # a normal day: close 16:00 ET = 20:00 UTC (EDT), cutoff 19:30 UTC exactly is blocked
    assert isinstance(at(DAY, 19, 29, 59), SizedOrder)
    assert isinstance(at(DAY, 19, 30), Rejection)


# ------------------------------------------------------------------------------ 3. Review Focus 5 (pure)
@pytest.mark.parametrize("switch", SWITCHES)
def test_exits_stops_and_cancels_pass_with_everything_tripped_on_a_weekend(switch: str) -> None:
    saturday = date(2026, 10, 10)
    worst = ctx(
        blocking_switch=switch,
        daily_pnl_pct=Decimal("-0.50"),
        entries_today=99,
        now=datetime(2026, 10, 10, 3, 0, tzinfo=UTC),
        session_date=saturday,
        account=AccountState(Decimal("-50"), Decimal("-50"), Decimal("-50"), Decimal(0), Decimal("-50")),
        symbol_market="TSX",
        settings=RuntimeSettings(markets_enabled=["US"]),
    )
    flat = RISK.evaluate(Exit(POS.id, "market", None, "flatten_close"), worst)
    assert isinstance(flat, SizedOrder) and flat.kind == "exit" and flat.qty == POS.qty
    assert flat.spec is not None and (flat.spec.side, flat.spec.position_id) == ("sell", POS.id)
    stop = RISK.evaluate(Exit(POS.id, "stop", Decimal("19.91"), "protective_stop"), worst)
    assert isinstance(stop, SizedOrder) and stop.kind == "stop" and stop.spec is not None
    assert stop.spec.order_type == "stop" and stop.spec.stop == Decimal("19.91")
    cancel = RISK.evaluate(Cancel(ORDER.id, "entry_cancel_at"), worst)
    assert isinstance(cancel, SizedOrder) and cancel.kind == "cancel" and cancel.cancel_order_id == ORDER.id
    entry = RISK.evaluate(long(), worst)
    assert isinstance(entry, Rejection) and entry.check == "kill_switch"


# ------------------------------------------------------------------------------------ kill switches (DB)
def ks_inputs(
    start: str, equity: str, peak: str, trades: int = 0, exp: str | None = None
) -> KillSwitchInputs:
    return KillSwitchInputs(
        Decimal(start), Decimal(equity), Decimal(peak), trades, Decimal(exp) if exp else None
    )


@pytest.mark.db
def test_daily_loss_resets_at_the_next_session_across_the_et_day_boundary(
    db_factory: sessionmaker[Session],
) -> None:
    # 20:00 ET on the early-close Friday is already Saturday in UTC: the trip belongs to Friday's session
    clock = FixedClock(datetime(2026, 11, 28, 1, 0, tzinfo=UTC))
    run = get_live_run(db_factory, clock, RuntimeSettings()).id
    ks = KillSwitches(db_factory, clock)
    s = RuntimeSettings()
    assert ks.evaluate(run, EARLY, ks_inputs("720", "680", "720"), s) == ["daily_loss_pct"]
    assert ks.blocking(run, EARLY) == "daily_loss_pct"
    nxt = CAL.next_session(EARLY)
    assert nxt == date(2026, 11, 30)
    assert ks.blocking(run, nxt) is None  # Monday: reset by itself, no manual step
    with db_factory() as ses:
        (row,) = ses.execute(select(m.KillSwitchEvent)).scalars().all()
    assert row.session_date == EARLY and row.reset_at is None
    # a fresh loss on the new session trips again (the old trip must not suppress it)
    clock.set(datetime(2026, 11, 30, 16, 0, tzinfo=UTC))
    assert ks.evaluate(run, nxt, ks_inputs("680", "680", "720"), s) == []
    assert ks.evaluate(run, nxt, ks_inputs("680", "640", "720"), s) == ["daily_loss_pct"]
    assert ks.blocking(run, nxt) == "daily_loss_pct"
    assert ks.blocking(run, CAL.next_session(nxt)) is None


@pytest.mark.db
def test_drawdown_and_expectancy_need_a_reasoned_reset_and_pause_blocks_entries_only(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 15, 0, tzinfo=UTC))
    run = get_live_run(db_factory, clock, RuntimeSettings()).id
    ks = KillSwitches(db_factory, clock)
    tripped = ks.evaluate(run, DAY, ks_inputs("680", "680", "800", trades=50, exp="-0.1"), RuntimeSettings())
    assert sorted(tripped) == ["expectancy", "max_drawdown_pct"]
    far = date(2026, 12, 31)
    assert ks.blocking(run, far) in ("expectancy", "max_drawdown_pct")  # survives many sessions
    assert ks.resume(run, actor="telegram") is False  # /resume never lifts an automatic switch
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="reason"):
            ks.reset(run, "expectancy", blank, actor="stephen")
    with pytest.raises(ValueError):
        ks.reset(run, "no_such_switch", "why", actor="stephen")
    ks.reset(run, "expectancy", "reviewed 50 trades", actor="stephen")
    assert ks.blocking(run, far) == "max_drawdown_pct"
    ks.reset(run, "max_drawdown_pct", "new peak policy", actor="stephen")
    assert ks.blocking(run, far) is None
    with db_factory() as ses:
        audits = ses.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars().all()
    assert [a.action for a in audits] == ["killswitch.reset:expectancy", "killswitch.reset:max_drawdown_pct"]
    assert audits[0].after == {"reason": "reviewed 50 trades"} and audits[0].actor == "stephen"

    # manual_pause: blocks entries (across sessions) but never exits, stops or cancels
    assert ks.pause(run, DAY, actor="telegram") is True
    blocking = ks.blocking(run, far)
    assert blocking == "manual_pause"
    c = ctx(blocking_switch=blocking)
    entry = RISK.evaluate(long(), c)
    assert isinstance(entry, Rejection) and entry.check == "kill_switch"
    assert isinstance(RISK.evaluate(Exit(POS.id, "market", None, "flatten"), c), SizedOrder)
    assert isinstance(RISK.evaluate(Exit(POS.id, "stop", Decimal("19.91"), "stop"), c), SizedOrder)
    assert isinstance(RISK.evaluate(Cancel(ORDER.id, "x"), c), SizedOrder)
    assert ks.resume(run, actor="telegram") is True
    assert ks.blocking(run, far) is None


# ------------------------------------------------------------------------------------- proposals (DB)
@dataclass
class Env:
    svc: ProposalService
    broker: SimBroker
    store: SettingsStore
    clock: FixedClock
    factory: sessionmaker[Session]
    run_id: int
    sym: int
    cfg: int
    signal_id: int


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "BRK", questrade_id=77)
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run.id,
            strategy_config_id=cfg,
            symbol_id=sym,
            session_date=T.date(),
            event_key="orb_open",
            ts=T,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.commit()
        signal_id = sig.id
    store = SettingsStore(db_factory, now=clock.now)
    broker = SimBroker(db_factory, clock, Ledger(CAL), QuoteFillModel(FillParams()), run.id)
    return Env(
        ProposalService(db_factory, clock, store, broker, run.id),
        broker,
        store,
        clock,
        db_factory,
        run.id,
        sym,
        cfg,
        signal_id,
    )


def entry_sized(e: Env, qty: int = 10) -> SizedOrder:
    intent = EnterLong(e.sym, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})
    spec = OrderSpec(
        e.sym,
        "buy",
        "stop",
        qty,
        stop=Decimal("20.01"),
        stop_loss=Decimal("19.91"),
        strategy_config_id=e.cfg,
        reason="orb_breakout",
    )
    return SizedOrder(intent, "entry", qty, spec, sizing={"shares": str(qty)})


def exit_sized(e: Env, pid: int, order_type: Literal["market", "stop"] = "market") -> SizedOrder:
    kind: Literal["stop", "exit"] = "stop" if order_type == "stop" else "exit"
    stop = Decimal("19.00") if order_type == "stop" else None
    spec = OrderSpec(e.sym, "sell", order_type, 10, stop=stop, purpose=kind, position_id=pid, reason=kind)
    return SizedOrder(Exit(pid, order_type, stop, kind), kind, 10, spec, position_id=pid)


def quote(e: Env, bid: str = "20.00", ask: str = "20.01") -> QtQuote:
    return QtQuote(
        e.sym,
        "BRK",
        Decimal(bid),
        Decimal(ask),
        Decimal(bid),
        None,
        1000,
        e.clock.now() - timedelta(seconds=1),
        0,
        False,
        None,
    )


def open_position(e: Env) -> int:
    e.broker.submit(
        OrderSpec(e.sym, "buy", "market", 10, stop_loss=Decimal("19.00"), strategy_config_id=e.cfg)
    )
    (ev,) = e.broker.on_quotes([quote(e)], e.clock.now())
    return ev.position_id


def orders(e: Env, **where: Any) -> list[m.Order]:
    with e.factory() as s:
        q = select(m.Order).where(m.Order.run_id == e.run_id)
        for k, v in where.items():
            q = q.where(getattr(m.Order, k) == v)
        return list(s.execute(q).scalars())


def race(*calls: Callable[[], Any]) -> list[Any]:
    gate = threading.Barrier(len(calls))

    def go(fn: Callable[[], Any]) -> Any:
        gate.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(go, calls))


@pytest.mark.db
def test_telegram_and_web_racing_to_approve_give_one_decision_and_one_order(env: Env) -> None:
    """Review Focus 2: two approvals at the same instant, repeated to shake out the race."""
    ids = [env.svc.create(env.signal_id, entry_sized(env), "entry").id for _ in range(6)]
    for pid in ids:
        results: list[DecisionResult] = race(
            partial(env.svc.decide, pid, "approve", "telegram", "stephen"),
            partial(env.svc.decide, pid, "approve", "web", "stephen"),
        )
        assert sorted(r.already_decided for r in results) == [False, True], results
        assert {r.status for r in results} == {"submitted"} and len({r.order_id for r in results}) == 1
    assert len(orders(env, purpose="entry")) == len(ids)  # never a second order for one proposal
    with env.factory() as s:
        decisions = (
            s.execute(select(m.AuditLog.action).where(m.AuditLog.action.like("proposal.%"))).scalars().all()
        )
    assert len(decisions) == len(ids)


@pytest.mark.db
def test_ttl_boundary_and_decision_latency(env: Env) -> None:
    """BR-31/BR-33: a tap at exactly the TTL expires; one a millisecond earlier executes; latency in ms."""
    early, late, rejected = (env.svc.create(env.signal_id, entry_sized(env), "entry") for _ in range(3))
    env.clock.set(T + timedelta(milliseconds=1500))
    assert env.svc.decide(rejected.id, "reject", "web", "stephen") == DecisionResult("rejected", False, None)
    env.clock.set(T + timedelta(seconds=299, milliseconds=999))
    ok = env.svc.decide(early.id, "approve", "telegram", "stephen")
    assert (ok.status, ok.already_decided) == ("submitted", False) and ok.order_id is not None
    env.clock.set(T + timedelta(seconds=300))
    too_late = env.svc.decide(late.id, "approve", "telegram", "stephen")
    assert (too_late.status, too_late.already_decided, too_late.order_id) == ("expired", True, None)
    assert env.svc.expire_due(T + timedelta(seconds=300)) == []  # already expired, not twice
    with env.factory() as s:
        rows = {p.id: p for p in s.execute(select(m.Proposal)).scalars()}
    assert rows[rejected.id].decision_latency_ms == 1500 and rows[rejected.id].decided_via == "web"
    assert rows[early.id].decision_latency_ms == 299_999 and rows[early.id].decided_via == "telegram"
    assert rows[late.id].expired_at == T + timedelta(seconds=300)
    assert rows[late.id].decided_via is None and rows[late.id].decision_latency_ms is None
    assert len(orders(env)) == 1


@pytest.mark.db
def test_auto_mode_executes_instantly_ignores_later_taps_and_every_mode_change_is_audited(env: Env) -> None:
    """BR-30, SPEC §6.2: auto approves at once; a later tap is already_decided; mode changes are audited."""
    env.svc.set_approval_mode("auto", actor="stephen")
    p = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert (p.status, p.decided_via, p.decision_latency_ms) == ("submitted", "auto", 0)
    assert p.decided_at == p.created_at == T and p.order_id is not None
    again = env.svc.decide(p.id, "approve", "telegram", "stephen")
    assert (again.status, again.already_decided, again.order_id) == ("submitted", True, p.order_id)
    cancel = SizedOrder(Cancel(p.order_id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=p.order_id)
    c = env.svc.create(env.signal_id, cancel, "cancel")
    assert c.status == "submitted" and env.broker.working_orders() == []
    env.clock.set(T + timedelta(minutes=1))
    env.svc.set_approval_mode("manual", actor="web:stephen")
    pending = env.svc.create(env.signal_id, entry_sized(env), "entry")
    assert pending.status == "pending" and pending.order_id is None
    with env.factory() as s:
        audits = (
            s.execute(
                select(m.AuditLog)
                .where(m.AuditLog.action == "settings.set:approval_mode")
                .order_by(m.AuditLog.id)
            )
            .scalars()
            .all()
        )
    assert [(a.actor, a.after) for a in audits] == [
        ("stephen", {"value": "auto"}),
        ("web:stephen", {"value": "manual"}),
    ]
    assert audits[1].ts == T + timedelta(minutes=1)


@pytest.mark.db
def test_unprotected_stop_alert_repeats_every_interval_until_a_stop_exists(env: Env) -> None:
    """SPEC §6.2: an expired protective stop re-alerts every stop_escalation_seconds while unprotected."""
    pid = open_position(env)
    stop = env.svc.create(env.signal_id, exit_sized(env, pid, "stop"), "stop")
    (expired,) = env.svc.expire_due(T + timedelta(seconds=180))
    assert expired.id == stop.id and expired.status == "expired" and expired.escalations == 1

    def alerts(sec: int) -> list[int]:
        return [p.escalations for p in env.svc.escalate_unprotected(T + timedelta(seconds=sec))]

    assert alerts(239) == []
    assert alerts(240) == [2]
    assert alerts(240) == []  # a second tick at the same instant must not double-alert
    assert alerts(299) == []
    assert alerts(300) == [3]
    assert alerts(390) == [4]  # a late tick still alerts once, then the cadence restarts
    assert alerts(449) == []
    assert alerts(450) == [5]
    env.store.set("stop_escalation_seconds", 30, actor="stephen")
    assert alerts(480) == [6]  # follows the configured interval
    with env.factory() as s:
        errors = (
            s.execute(select(m.EventLog).where(m.EventLog.source == "proposals", m.EventLog.level == "error"))
            .scalars()
            .all()
        )
    assert len(errors) == 6
    env.broker.submit(
        OrderSpec(env.sym, "sell", "stop", 10, stop=Decimal("19.00"), purpose="stop", position_id=pid)
    )
    assert alerts(600) == [] and alerts(900) == []  # protected now: alerts stop


@pytest.mark.db
def test_expiry_auto_executions_are_single_and_never_crash_the_sweep(env: Env) -> None:
    """Review Focus 2 + 4: a tap racing the expiry sweep gives ONE flatten; re-runs add nothing; an expired
    cancel of an order that already filled fails cleanly without blocking the rest of the sweep."""
    pid = open_position(env)
    flat = env.svc.create(env.signal_id, exit_sized(env, pid), "exit")
    env.clock.set(T + timedelta(seconds=299))
    race(
        lambda: env.svc.decide(flat.id, "approve", "web", "stephen"),
        lambda: env.svc.expire_due(T + timedelta(seconds=300)),
    )
    assert env.svc.expire_due(T + timedelta(seconds=400)) == []
    assert env.svc.expire_due(T + timedelta(seconds=400)) == []
    assert len(orders(env, purpose="exit", position_id=pid)) == 1
    late = env.svc.decide(flat.id, "approve", "telegram", "stephen")
    assert late.already_decided and late.status == "submitted"
    assert len(orders(env, purpose="exit", position_id=pid)) == 1

    # an expired cancel (auto_flatten_on_expiry on) whose entry filled in the meantime
    env.clock.set(T + timedelta(seconds=500))
    entry_id = env.broker.submit(
        OrderSpec(env.sym, "buy", "market", 5, stop_loss=Decimal("19.00"), strategy_config_id=env.cfg)
    )
    cancel = env.svc.create(
        env.signal_id,
        SizedOrder(Cancel(entry_id, "entry_cancel_at"), "cancel", 5, None, cancel_order_id=entry_id),
        "cancel",
    )
    other = env.svc.create(env.signal_id, entry_sized(env), "entry")
    env.broker.on_quotes([quote(env)], env.clock.now())  # the entry fills before anyone answers
    swept = {p.id: p for p in env.svc.expire_due(T + timedelta(seconds=500 + 300))}
    assert set(swept) == {cancel.id, other.id}
    assert swept[cancel.id].status == "failed" and swept[cancel.id].error
    assert swept[cancel.id].decided_by == "auto_flatten_on_expiry"
    assert swept[other.id].status == "expired"
    with env.factory() as s:
        filled = s.get(m.Order, entry_id)
    assert filled is not None and filled.status == "filled"
