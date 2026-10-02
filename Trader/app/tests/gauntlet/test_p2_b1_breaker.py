"""P2-B1 gauntlet breaker: money paths of the ledger, fill model and simulated broker (P2-T1..T5).

Written by the Breaker (attempt 1). Testcontainers DB and fakes only, never trader_dev.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.ledger import Ledger
from trader.broker.sim_broker import SimBroker
from trader.broker.types import FillDecision, NoFill, OrderSpec
from trader.db import models as m
from trader.engine.runs import get_live_run, sim_account
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

CAL = SessionCalendar()
Q4 = Decimal("0.0001")


@dataclass
class Env:
    broker: SimBroker
    clock: FixedClock
    run_id: int
    syms: list[int]
    cfg: int
    factory: sessionmaker[Session]


def make_env(
    factory: sessionmaker[Session], at: datetime, settings: RuntimeSettings | None = None, n_syms: int = 1
) -> Env:
    st = settings if settings is not None else RuntimeSettings()
    clock = FixedClock(at)
    run = get_live_run(factory, clock, st)
    with factory() as s:
        syms = [add_symbol(s, f"S{i}", questrade_id=100 + i) for i in range(n_syms)]
        cfg = add_strategy_config(s)
        s.commit()
    broker = SimBroker(
        factory,
        clock,
        Ledger(CAL),
        QuoteFillModel(FillParams.from_settings(st)),
        run.id,
        st.account_currency,
        calendar=CAL,
        settings=lambda: st,
    )
    return Env(broker, clock, run.id, syms, cfg, factory)


def quote(
    sym: int, now: datetime, bid: str | None, ask: str | None, last: str | None, age: float = 1.0
) -> QtQuote:
    return QtQuote(
        symbol_id=sym,
        symbol="S",
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        last=None if last is None else Decimal(last),
        last_regular=None,
        volume=1000,
        last_trade_time=now - timedelta(seconds=age),
        delay=0,
        is_halted=False,
        vwap=None,
    )


def entry(env: Env, sym: int | None = None, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(
            env.syms[0] if sym is None else sym,
            "buy",
            "stop",
            qty,
            stop=Decimal("10.00"),
            stop_loss=Decimal("9.90"),
            strategy_config_id=env.cfg,
            reason="orb_breakout",
        )
    )


def trigger(env: Env, sym: int | None = None) -> QtQuote:
    return quote(env.syms[0] if sym is None else sym, env.clock.now(), "10.01", "10.02", "10.02")


def market_exit(env: Env, position_id: int, qty: int = 50) -> int:
    return env.broker.submit(
        OrderSpec(env.syms[0], "sell", "market", qty, purpose="exit", position_id=position_id, reason="flat")
    )


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    """Build a UTC instant from an ET wall time (via the calendar's own session open offset)."""
    open_utc = CAL.session_open(d)  # 09:30 ET in UTC
    return open_utc + timedelta(hours=hh - 9, minutes=mm - 30, seconds=ss)


# 1 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
@pytest.mark.parametrize(
    ("trade_day", "holiday", "settle_day"),
    [
        (date(2026, 11, 25), date(2026, 11, 26), date(2026, 11, 27)),  # Wed before Thanksgiving
        (date(2026, 12, 24), date(2026, 12, 25), date(2026, 12, 28)),  # Christmas Eve, early close
        (date(2026, 12, 31), date(2027, 1, 1), date(2027, 1, 4)),  # year end
    ],
)
def test_same_day_round_trip_proceeds_settle_only_next_session(
    db_factory: sessionmaker[Session], trade_day: date, holiday: date, settle_day: date
) -> None:
    """Review Focus 3: a same-day buy+sell in cash_account_mode leaves only the unspent settled cash as
    buying power until the next session, across a holiday and year end; total cash = deposit + P&L."""
    env = make_env(db_factory, et(trade_day, 10, 0))
    entry(env)
    (ev,) = env.broker.on_quotes([trigger(env)], env.clock.now())
    market_exit(env, ev.position_id)
    exit_quote = quote(env.syms[0], env.clock.now(), "10.20", "10.21", "10.20")
    (out,) = env.broker.on_quotes([exit_quote], env.clock.now())
    # buy 50 @ 10.03 = 501.50; sell 50 @ 10.19 = 509.50; SEC 509.5 x 0.0000206 = 0.0104957 -> 0.0105
    assert out.pnl == Decimal("7.9895")
    for day, settled in ((trade_day, "218.4895"), (holiday, "218.4895"), (settle_day, "727.9895")):
        acct = env.broker.account_state(day, {}, cash_account_mode=True)
        assert acct.total_cash == Decimal("727.9895"), day
        assert acct.settled_cash == Decimal(settled), day
        assert acct.buying_power == Decimal(settled), day
    margin = env.broker.account_state(trade_day, {}, cash_account_mode=False)
    assert margin.buying_power == Decimal("727.9895")
    with env.factory() as s:
        settles = set(
            s.execute(select(m.CashLedger.settle_date).where(m.CashLedger.kind != "deposit")).scalars()
        )
    assert settles == {settle_day}


# 2 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
@pytest.mark.parametrize(
    ("day", "close_utc"),
    [
        (date(2026, 11, 27), datetime(2026, 11, 27, 18, 0, tzinfo=UTC)),  # early close 13:00 ET (EST)
        (date(2026, 11, 2), datetime(2026, 11, 2, 21, 0, tzinfo=UTC)),  # first Monday after DST ends
        (date(2026, 10, 30), datetime(2026, 10, 30, 20, 0, tzinfo=UTC)),  # last Friday of EDT
    ],
)
def test_entry_cutoff_at_the_exact_second(
    db_factory: sessionmaker[Session], day: date, close_utc: datetime
) -> None:
    """Review Focus 4: an entry fills one second before close-30min and is cancelled at the cutoff second,
    on an early-close day and on both sides of the DST change."""
    cutoff = close_utc - timedelta(minutes=30)
    env = make_env(db_factory, cutoff - timedelta(seconds=1))
    assert env.broker.entry_cutoff(day) == cutoff
    first = entry(env)
    (ev,) = env.broker.on_quotes([trigger(env)], env.clock.now())
    assert ev.order_id == first  # one second before the cutoff an entry still fills
    second = entry(env, qty=5)  # submitted before the cutoff, first quote arrives at the cutoff second
    env.clock.set(cutoff)
    assert env.broker.on_quotes([trigger(env)], cutoff) == []
    third = entry(env, qty=5)  # early-close day: 15:00 ET is before the normal 15:30 cutoff but after close
    later = close_utc + timedelta(hours=2)
    env.clock.set(later)
    assert env.broker.on_quotes([trigger(env)], later) == []
    with env.factory() as s:
        rows = [s.get(m.Order, oid) for oid in (second, third)]
    assert [(r.status, r.cancel_reason) for r in rows if r is not None] == [
        ("cancelled", "entry cutoff"),
        ("cancelled", "entry cutoff"),
    ]


# 3 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
def test_an_entry_never_fills_before_the_session_opens(db_factory: sessionmaker[Session]) -> None:
    """A day entry order met by a pre-market quote (09:00 ET) must not fill: Questrade quotes run from
    04:00 ET, and a regular-hours day order can't execute before 09:30. Filling here opens a position on
    a thin pre-market book at a price the real broker would never give."""
    day = date(2026, 10, 6)
    env = make_env(db_factory, et(day, 9, 0))
    entry(env)
    fills = env.broker.on_quotes([trigger(env)], env.clock.now())
    assert fills == []
    assert env.broker.open_positions() == []


# 4 -------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams())


def buy(order_type: str = "market", stop: str | None = None, limit: str | None = None) -> OrderSpec:
    return OrderSpec(
        1,
        "buy",
        order_type,  # type: ignore[arg-type]
        100,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
    )


def sell(
    order_type: str = "market", stop: str | None = None, limit: str | None = None, qty: int = 100
) -> OrderSpec:
    return OrderSpec(
        1,
        "sell",
        order_type,  # type: ignore[arg-type]
        qty,
        stop=Decimal(stop) if stop else None,
        limit=Decimal(limit) if limit else None,
        purpose="stop" if order_type == "stop" else "exit",
        position_id=7,
    )


def q1(
    bid: str | None, ask: str | None, last: str | None, age: float = 1.0, delay: int | None = 0
) -> QtQuote:
    return QtQuote(
        symbol_id=1,
        symbol="AAA",
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        last=None if last is None else Decimal(last),
        last_regular=None,
        volume=1,
        last_trade_time=NOW - timedelta(seconds=age),
        delay=delay,
        is_halted=False,
        vwap=None,
    )


def test_fill_model_boundaries() -> None:
    # stop exactly equal to last (ask below the stop): triggers, fills at max(stop, ask) + slip
    d = MODEL.assess(buy("stop", stop="10.00"), q1("9.98", "9.99", "10.00"), NOW)
    assert isinstance(d, FillDecision) and d.price == Decimal("10.0100") and d.trigger == "stop"
    # sell stop exactly equal to the last trade (FILLFIX: the last trade triggers, not the bid)
    d = MODEL.assess(sell("stop", stop="9.90"), q1("9.90", "9.91", "9.90"), NOW)
    assert isinstance(d, FillDecision) and d.price == Decimal("9.8900")
    # one cent short of the stop: no trigger
    n = MODEL.assess(buy("stop", stop="10.00"), q1("9.98", "9.99", "9.99"), NOW)
    assert isinstance(n, NoFill) and n.reason == "not_triggered"
    # a quote exactly stale_quote_seconds old is not "older than" the limit: fills; 1 ms more: no fill
    assert isinstance(MODEL.assess(buy(), q1("9.99", "10.00", "10.00", age=10.0), NOW), FillDecision)
    n = MODEL.assess(buy(), q1("9.99", "10.00", "10.00", age=10.001), NOW)
    assert isinstance(n, NoFill) and n.reason == "stale_quote"
    # zero or missing ask/bid is one-sided: never fills
    for bid, ask in (("9.99", "0"), ("9.99", None), ("0.0000", "0.0000")):
        n = MODEL.assess(buy(), q1(bid, ask, "10.00"), NOW)
        assert isinstance(n, NoFill) and n.reason == "no_ask", (bid, ask)
    for bid in ("0", None):
        n = MODEL.assess(sell(), q1(bid, "10.00", "10.00"), NOW)
        assert isinstance(n, NoFill) and n.reason == "no_bid", bid
    # delay None (Questrade omitted it) and delay > 0: never fills
    for delay in (None, 1, 15):
        n = MODEL.assess(buy(), q1("9.99", "10.00", "10.00", delay=delay), NOW)
        assert isinstance(n, NoFill) and n.reason == "delayed_quote", delay


# 5 -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_a_crossed_market_never_fills(side: str) -> None:
    """Review Focus 1 (bad quote): bid > ask is a broken or locked book (a data error or a halt/auction
    artefact). A market order filled on it gets an impossible price, e.g. a sell at bid 10.50 - slip when
    the offer is 10.00. The fill model must treat a crossed quote as unusable, like a one-sided one."""
    crossed = q1("10.50", "10.00", "10.25")
    order = buy() if side == "buy" else sell()
    out = MODEL.assess(order, crossed, NOW)
    assert isinstance(out, NoFill), out


# 6 -------------------------------------------------------------------------------------------------------
def _sec(value: Decimal) -> Decimal:
    return (Decimal("0.0000206") * value).quantize(Q4, ROUND_HALF_UP)


@pytest.mark.parametrize(
    ("qty", "bid"),
    [(1, "2.00"), (1, "5.00"), (3, "7.3333"), (100_000, "50.00"), (999_999, "4999.99")],
)
def test_sell_fees_and_slippage_round_consistently(qty: int, bid: str) -> None:
    """Tiny and huge sells: SEC fee = 0.0000206 x fill value to 4 dp half-up, fee and value fit
    numeric(14,4), and the price and slippage are exact 4-dp Decimals."""
    params = FillParams(direct_route=True, commission=Decimal("0.99"))
    model = QuoteFillModel(params)
    d = model.assess(sell(qty=qty), q1(bid, "9999", bid), NOW)
    assert isinstance(d, FillDecision)
    b = Decimal(bid)
    slip = max(Decimal("0.01"), (Decimal(5) / 10000 * b)).quantize(Q4, ROUND_HALF_UP)
    assert d.slippage == slip and d.price == (b - slip).quantize(Q4, ROUND_HALF_UP)
    assert d.fees.sec == _sec(d.price * qty)
    assert d.fees.ecn == (Decimal("0.0035") * qty).quantize(Q4, ROUND_HALF_UP)
    assert d.fees.total == d.fees.commission + d.fees.ecn + d.fees.sec
    for v in (d.price, d.slippage, d.fees.sec, d.fees.ecn, d.fees.total, d.price * qty):
        assert v == v.quantize(Q4) and abs(v) < Decimal("10000000000"), v
    buy_fees = model.fees("buy", qty, d.price)
    assert buy_fees.sec == 0  # SEC fee on sells only


# 7 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
def test_concurrent_quote_batches_and_cancels_fill_each_order_at_most_once(
    db_factory: sessionmaker[Session],
) -> None:
    """Review Focus 2: eight concurrent on_quotes batches over two symbols plus a racing cancel. Every
    order ends filled exactly once or cancelled, never both; one fill row, one position per filled order,
    and one buy ledger row per fill."""
    env = make_env(db_factory, et(date(2026, 10, 6), 10, 0), n_syms=2)
    a, b = env.syms
    orders = [entry(env, a, qty=1), entry(env, b, qty=1), entry(env, a, qty=2), entry(env, b, qty=2)]
    racing = orders[3]
    quotes = [trigger(env, a), trigger(env, b)]
    now = env.clock.now()
    with ThreadPoolExecutor(max_workers=9) as pool:
        futs: list[Any] = [pool.submit(env.broker.on_quotes, quotes, now) for _ in range(8)]
        cancel = pool.submit(env.broker.cancel, racing, "racing cancel")
        fills = [ev for f in futs for ev in f.result()]
        cancelled = cancel.result()
    filled_ids = [ev.order_id for ev in fills]
    assert len(filled_ids) == len(set(filled_ids))
    expected = set(orders) - ({racing} if cancelled else set())
    assert set(filled_ids) == expected
    with env.factory() as s:
        n_fills = s.execute(select(func.count()).select_from(m.Fill)).scalar_one()
        n_pos = s.execute(select(func.count()).select_from(m.Position)).scalar_one()
        n_buys = s.execute(
            select(func.count()).select_from(m.CashLedger).where(m.CashLedger.kind == "buy")
        ).scalar_one()
        racing_row = s.get(m.Order, racing)
    assert n_fills == n_pos == n_buys == len(expected)
    assert racing_row is not None and racing_row.status == ("cancelled" if cancelled else "filled")


# 8 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
def test_pnl_r_and_cash_reconcile_to_the_ten_thousandth(db_factory: sessionmaker[Session]) -> None:
    """With commission and ECN on, trade.pnl, fees_total and pnl_r are exact Decimals, and the ledger total
    equals the deposit plus the trade's net P&L (no rounding leak between the ledger and the trade)."""
    st = RuntimeSettings(fees_direct_route=True, fees_commission=Decimal("1.00"))
    env = make_env(db_factory, et(date(2026, 10, 6), 10, 0), st)
    entry(env)
    (ev,) = env.broker.on_quotes([trigger(env)], env.clock.now())
    market_exit(env, ev.position_id)
    (out,) = env.broker.on_quotes(
        [quote(env.syms[0], env.clock.now(), "10.3333", "10.3400", "10.3333")], env.clock.now()
    )
    # entry 10.03, fees 1.00 + 0.1750; exit 10.3333 - 0.01 = 10.3233, fees 1.00 + 0.1750 + SEC 0.0106
    with env.factory() as s:
        trade = s.execute(select(m.Trade)).scalar_one()
        fee_rows = s.execute(select(m.CashLedger.amount).where(m.CashLedger.kind == "fee")).scalars().all()
    assert out.price == Decimal("10.3233")
    assert trade.fees_total == Decimal("2.3606")
    assert trade.pnl == Decimal("12.3044")  # 0.2933 x 50 - 2.3606
    assert trade.planned_risk == Decimal("6.5000")
    assert trade.pnl_r == Decimal("1.8930")  # 12.3044 / 6.5 = 1.89298...
    assert -sum(fee_rows, Decimal(0)) == trade.fees_total
    acct = env.broker.account_state(date(2026, 10, 7), {}, cash_account_mode=True)
    assert acct.total_cash == Decimal("720") + trade.pnl == acct.settled_cash == acct.equity


# 9 -------------------------------------------------------------------------------------------------------
@pytest.mark.db
@pytest.mark.parametrize(
    ("src", "dst", "amount", "expected", "rate"),
    [
        ("CAD", "USD", "1000", "709.2000", "0.720000"),  # 1000 x 0.72 x (1 - 0.015)
        ("USD", "CAD", "720", "985.0000", "1.388889"),  # 720 / 0.72 x 0.985
    ],
)
def test_equity_after_fx_conversion(
    db_factory: sessionmaker[Session], src: str, dst: str, amount: str, expected: str, rate: str
) -> None:
    st = RuntimeSettings(
        starting_cash=Decimal(amount),
        starting_cash_currency=src,
        account_currency=dst,  # type: ignore[arg-type]
    )
    env = make_env(db_factory, et(date(2026, 10, 6), 10, 0), st)
    acct = env.broker.account_state(date(2026, 10, 6), {}, cash_account_mode=True)
    assert acct.equity == acct.settled_cash == acct.buying_power == Decimal(expected)
    info = sim_account(env.factory, env.run_id)
    assert info is not None
    assert (info.currency, info.starting_cash, info.source_amount, info.source_currency) == (
        dst,
        Decimal(expected),
        Decimal(amount),
        src,
    )
    assert info.fx_rate == Decimal(rate) and info.fx_fee == Decimal("0.0150")
    # a second get_live_run (another process) must not convert or deposit again
    get_live_run(env.factory, env.clock, st)
    with env.factory() as s:
        deposits = s.execute(select(m.CashLedger.currency, m.CashLedger.amount)).all()
    assert deposits == [(dst, Decimal(expected))]


# 10 ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("stale_quote_seconds", 0),
        ("stale_quote_seconds", "NaN"),
        ("slippage_bps", -1),
        ("slippage_min", "-0.01"),
        ("fees.sec_rate", "0.01"),
        ("fees.commission", "-1"),
        ("fx.fee_pct", "0.2"),
        ("fx.cad_usd_rate", "0"),
        ("fx.cad_usd_rate", "Infinity"),
        ("starting_cash", "0"),
        ("starting_cash", "100000000000"),
        ("no_entry_before_close_minutes", -1),
        ("no_entry_before_close_minutes", 391),
        ("risk_pct", "0"),
        ("account_currency", "EUR"),
        ("killswitch.max_drawdown_pct", "1.5"),
    ],
)
def test_money_settings_reject_out_of_range_values(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({key: value})
