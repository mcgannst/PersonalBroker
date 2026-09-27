"""P5-T4: SimBroker.on_candles, the candle twin of on_quotes (replay).

The broker is exercised with a small fake candle model (a triggered order fills at the worse of its stop and
the bar's open, market orders at the open, no slippage, no fees), so these tests pin the BROKER's rules: which
bar an order may use, hours and the entry cutoff against the bar's start, timestamps from `now`, per-order
savepoints and the `orders=` restriction. The real CandleFillModel is P5-T3's (tests/replay).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_replay import candle
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import ENTRY_CUTOFF, SimBroker
from trader.broker.types import Fees, FillDecision, FillModel, NoFill, OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tue; ET = UTC-4


def et(h: int, mi: int, sec: int = 0) -> datetime:
    return datetime(2026, 10, 6, h + 4, mi, sec, tzinfo=UTC)


class FakeCandleModel:
    """Fills a triggered order at the worse of its stop and the bar's open; a market order at the open.
    No slippage, no fees. Raises for the symbols in `fail` (to test the per-order savepoint)."""

    def __init__(self, fail: frozenset[int] = frozenset()) -> None:
        self.fail = fail
        self.calls: list[tuple[int, Candle]] = []

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None:
        out = self.assess(order, market, now)
        return out if isinstance(out, FillDecision) else None

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill:
        if not isinstance(market, Candle):
            raise TypeError("the fake candle model fills from candles only")
        if order.symbol_id in self.fail:
            raise RuntimeError("model exploded")
        self.calls.append((order.symbol_id, market))
        if market.volume == 0:
            return NoFill("no_volume")
        if order.order_type == "market":
            price = market.open
        elif order.side == "buy":
            assert order.stop is not None
            if market.high < order.stop:
                return NoFill("not_triggered")
            price = max(order.stop, market.open)
        else:
            assert order.stop is not None
            if market.low > order.stop:
                return NoFill("not_triggered")
            price = min(order.stop, market.open)
        return FillDecision(price, order.qty, Decimal("0"), Fees(), {"source": "fake"}, "fake")


_fake: FillModel = FakeCandleModel()  # satisfies the FillModel protocol (mypy)


@dataclass
class Env:
    broker: SimBroker
    clock: FixedClock
    model: FakeCandleModel
    run_id: int
    syms: dict[str, int]
    cfg: int
    factory: sessionmaker[Session]


def make_env(factory: sessionmaker[Session], model: FillModel | None = None) -> Env:
    clock = FixedClock(et(9, 35, 5))
    run = get_live_run(factory, clock, RuntimeSettings())  # deposit 720 USD
    with factory() as s:
        syms = {t: add_symbol(s, t, questrade_id=11 + i) for i, t in enumerate(("AAA", "BBB", "CCC"))}
        cfg = add_strategy_config(s)
        s.commit()
    fake = FakeCandleModel()
    broker = SimBroker(
        factory, clock, Ledger(CAL), model if model is not None else fake, run.id, calendar=CAL
    )
    return Env(broker, clock, fake, run.id, syms, cfg, factory)


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    return make_env(db_factory)


def entry(env: Env, ticker: str = "AAA", qty: int = 10, stop: str = "10.00") -> int:
    return env.broker.submit(
        OrderSpec(
            env.syms[ticker],
            "buy",
            "stop",
            qty,
            stop=Decimal(stop),
            stop_loss=Decimal("9.90"),
            strategy_config_id=env.cfg,
            reason="orb_breakout",
        )
    )


def open_position(env: Env, ticker: str = "AAA") -> int:
    """An entry filled by the 09:35 bar (at 09:36); returns the position id."""
    env.clock.set(et(9, 35, 5))
    entry(env, ticker)
    (ev,) = env.broker.on_candles(
        {env.syms[ticker]: candle(et(9, 35), "9.95", "10.10", "9.94", "10.05")}, et(9, 36)
    )
    return ev.position_id


def sell(env: Env, position_id: int, order_type: str = "stop", ticker: str = "AAA") -> int:
    return env.broker.submit(
        OrderSpec(
            env.syms[ticker],
            "sell",
            order_type,  # type: ignore[arg-type]
            10,
            stop=Decimal("9.90") if order_type == "stop" else None,
            purpose="stop" if order_type == "stop" else "exit",
            position_id=position_id,
            reason="protective_stop" if order_type == "stop" else "flatten_close",
        )
    )


def status(env: Env, order_id: int) -> tuple[str, str | None]:
    with env.factory() as s:
        o = s.get(m.Order, order_id)
        assert o is not None
        return o.status, o.cancel_reason


def events(env: Env, level: str) -> list[m.EventLog]:
    with env.factory() as s:
        return list(
            s.execute(select(m.EventLog).where(m.EventLog.level == level).order_by(m.EventLog.id)).scalars()
        )


# --- 1. which bar an order may use ------------------------------------------------------------


def test_entry_uses_the_first_bar_ending_after_submission_and_fills_at_now(env: Env) -> None:
    oid = entry(env)  # submitted 09:35:05
    aaa = env.syms["AAA"]
    # the 09:34 bar ended at 09:35, before the order existed: never used, even though it would trigger
    assert env.broker.on_candles({aaa: candle(et(9, 34), "10.00", "10.50", "9.90", "10.40")}, et(9, 35)) == []
    assert env.model.calls == []
    assert status(env, oid) == ("working", None)
    (ev,) = env.broker.on_candles({aaa: candle(et(9, 35), "9.95", "10.10", "9.94", "10.05")}, et(9, 36))
    assert (ev.order_id, ev.price, ev.ts) == (oid, Decimal("10.00"), et(9, 36))
    with env.factory() as s:
        fill = s.execute(select(m.Fill)).scalar_one()
        order = s.get(m.Order, oid)
        pos = s.get(m.Position, ev.position_id)
        ledger_ts = set(s.execute(select(m.CashLedger.ts).where(m.CashLedger.kind == "buy")).scalars())
    assert fill.ts == et(9, 36) and order is not None and order.closed_at == et(9, 36)
    assert pos is not None and pos.opened_at == et(9, 36) and ledger_ts == {et(9, 36)}


def test_stop_submitted_at_a_bar_boundary_ignores_the_bar_ending_then(env: Env) -> None:
    pid = open_position(env)
    env.clock.set(et(15, 50))
    stop = sell(env, pid)  # submitted exactly at 15:50:00
    aaa = env.syms["AAA"]
    # the 15:49 bar ends at 15:50:00, equal to the submission: not after it, so not used
    assert env.broker.on_candles({aaa: candle(et(15, 49), "9.85", "9.86", "9.80", "9.82")}, et(15, 50)) == []
    assert status(env, stop) == ("working", None)
    (ev,) = env.broker.on_candles({aaa: candle(et(15, 50), "9.95", "9.96", "9.80", "9.82")}, et(15, 51))
    assert (ev.order_id, ev.price, ev.ts, ev.trade_id is not None) == (
        stop,
        Decimal("9.90"),
        et(15, 51),
        True,
    )
    with env.factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
    assert trade.closed_at == et(15, 51) and trade.exit_reason == "protective_stop"


# --- 2. regular hours and the entry cutoff against the bar's start -----------------------------------------


def test_a_bar_starting_at_1559_fills_a_working_exit_at_1600(env: Env) -> None:
    pid = open_position(env)
    env.clock.set(et(15, 58, 30))
    exit_id = sell(env, pid, "market")
    (ev,) = env.broker.on_candles(
        {env.syms["AAA"]: candle(et(15, 59), "10.20", "10.25", "10.15", "10.22")}, et(16, 0)
    )
    assert (ev.order_id, ev.price, ev.ts) == (exit_id, Decimal("10.20"), et(16, 0))


def test_a_bar_starting_at_the_close_fills_nothing(env: Env) -> None:
    pid = open_position(env)
    env.clock.set(et(15, 58, 30))
    exit_id = sell(env, pid, "market")
    after = candle(et(16, 0), "10.20", "10.25", "10.15", "10.22")
    assert env.broker.on_candles({env.syms["AAA"]: after}, et(16, 1)) == []
    assert status(env, exit_id) == ("working", None)  # outside regular hours: it keeps working


def test_entry_cutoff_is_checked_against_the_bar_start(env: Env) -> None:
    # cutoff = 16:00 - 30 min = 15:30 ET
    env.clock.set(et(15, 20))
    early = entry(env, "AAA")
    late = entry(env, "BBB")
    bars = {
        env.syms["AAA"]: candle(et(15, 29), "9.95", "10.10", "9.94", "10.05"),  # starts before the cutoff
        env.syms["BBB"]: candle(et(15, 30), "9.95", "10.10", "9.94", "10.05"),  # starts at the cutoff
    }
    (ev,) = env.broker.on_candles(bars, et(15, 31))
    assert ev.order_id == early
    assert status(env, late) == ("cancelled", ENTRY_CUTOFF)
    with env.factory() as s:
        assert s.execute(select(m.Fill).where(m.Fill.order_id == late)).first() is None


# --- 5. several symbols, per-order isolation ---------------------------------------------------------------


def test_each_order_gets_its_own_symbols_bar_and_a_failure_is_isolated(
    db_factory: sessionmaker[Session],
) -> None:
    probe = make_env(db_factory)
    model = FakeCandleModel(fail=frozenset({probe.syms["BBB"]}))
    env = Env(
        SimBroker(db_factory, probe.clock, Ledger(CAL), model, probe.run_id, calendar=CAL),
        probe.clock,
        model,
        probe.run_id,
        probe.syms,
        probe.cfg,
        db_factory,
    )
    a, b, c = entry(env, "AAA"), entry(env, "BBB"), entry(env, "CCC")
    bars = {
        env.syms["AAA"]: candle(et(9, 35), "9.95", "10.10", "9.94", "10.05"),
        env.syms["BBB"]: candle(et(9, 35), "20.00", "20.10", "19.90", "20.05"),
    }
    (ev,) = env.broker.on_candles(bars, et(9, 36))
    assert (ev.order_id, ev.symbol_id) == (a, env.syms["AAA"])
    assert model.calls == [(env.syms["AAA"], bars[env.syms["AAA"]])]  # each order saw its own symbol's bar
    assert status(env, b) == ("working", None)  # rolled back alone
    assert status(env, c) == ("working", None)  # no bar for CCC: untouched
    (err,) = events(env, "error")
    assert err.data["order_id"] == b and err.data["error_type"] == "RuntimeError"
    assert err.run_id == env.run_id


# --- 6. a quote model given candles ------------------------------------------------------------


def test_quote_model_given_candles_raises_type_error_inside_the_savepoint(
    db_factory: sessionmaker[Session],
) -> None:
    env = make_env(db_factory, QuoteFillModel(FillParams()))
    oid = entry(env)
    bar = candle(et(9, 35), "9.95", "10.10", "9.94", "10.05")
    assert env.broker.on_candles({env.syms["AAA"]: bar}, et(9, 36)) == []
    (err,) = events(env, "error")
    assert err.data["order_id"] == oid and err.data["error_type"] == "TypeError"
    assert status(env, oid) == ("working", None)


# --- the orders= restriction and unusable bars -------------------------------------------------------------


def test_orders_restricts_the_pass_and_skips_the_submission_rule(env: Env) -> None:
    pid = open_position(env)  # at 09:36
    env.clock.set(et(9, 36))
    stop = sell(env, pid)  # submitted at 09:36, equal to the bar's end
    env.clock.set(et(9, 36))
    other = entry(env, "BBB")
    bar = candle(et(9, 35), "10.00", "10.10", "9.80", "9.85")
    both = {env.syms["AAA"]: bar, env.syms["BBB"]: candle(et(9, 35), "9.95", "10.10", "9.94", "10.05")}
    assert env.broker.on_candles(both, et(9, 36), orders=[]) == []
    (ev,) = env.broker.on_candles(both, et(9, 36), orders=[stop])
    assert (ev.order_id, ev.price, ev.ts) == (stop, Decimal("9.90"), et(9, 36))
    assert status(env, other) == ("working", None)  # not in `orders`: untouched


def test_an_unusable_bar_is_logged_once_per_order_at_info(env: Env) -> None:
    oid = entry(env)
    aaa = env.syms["AAA"]
    empty = candle(et(9, 35), "9.95", "10.10", "9.94", "10.05", 0)
    assert env.broker.on_candles({aaa: empty}, et(9, 36)) == []
    assert (
        env.broker.on_candles({aaa: candle(et(9, 36), "9.95", "10.10", "9.94", "10.05", 0)}, et(9, 37)) == []
    )
    notes = [e for e in events(env, "info") if e.data.get("reason") == "no_volume"]
    assert len(notes) == 1 and notes[0].data["order_id"] == oid
    assert events(env, "warning") == [] and events(env, "error") == []
    with env.factory() as s:
        order = s.get(m.Order, oid)
    assert order is not None and order.stale_since is None  # no quote-staleness bookkeeping for candles


def test_not_triggered_is_silent_and_empty_input_is_a_no_op(env: Env) -> None:
    entry(env)
    assert env.broker.on_candles({}, et(9, 36)) == []
    below = candle(et(9, 35), "9.80", "9.90", "9.70", "9.85")
    assert env.broker.on_candles({env.syms["AAA"]: below}, et(9, 36)) == []
    assert [e for e in events(env, "info") if "not filling" in e.message] == []
