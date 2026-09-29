"""DB-T3 acceptance tests 4-6, 8, 9 and 16 (testcontainer): period P&L, fees and Claude spend per period, the
P&L = equity invariant, an empty run, and the equity series (live dashboard plan S4-S6).

The seed helpers write rows shaped the way the simulated broker writes them (an order and a fill per side, the
ledger's buy/sell/fee rows, a position, and a trade whose `pnl` is net of both fees).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.api.livedata.equity import equity_series
from trader.api.livedata.periods import claude_today, period_blocks, period_windows
from trader.api.livedata.types import OpenValue
from trader.broker.types import Fees
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, et_date
from trader.reports.metrics import compute_metrics
from trader.reports.weekly import claude_spent
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

CAL = SessionCalendar()
Q4 = Decimal("0.0001")
RUN_START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # Thu 10-01 08:00 ET
CASH = Decimal("10000.0000")
NO_FEES = Fees()


def et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=ET).astimezone(UTC)


def q4(v: Decimal) -> Decimal:
    return v.quantize(Q4, ROUND_HALF_UP)


# --- seed helpers (shared by test_books.py) -----------------------------------------------------------------


def seed_run(
    s: Session,
    *,
    mode: str = "live",
    status: str = "active",
    started_at: datetime = RUN_START,
    cash: Decimal = CASH,
) -> int:
    run_id = add_run(s, mode=mode, status=status, started_at=started_at)
    s.add(
        m.SimAccount(
            run_id=run_id,
            currency="USD",
            starting_cash=cash,
            source_amount=cash,
            source_currency="USD",
            created_at=started_at,
        )
    )
    ledger(s, run_id, started_at, cash, "deposit", "deposit")
    s.flush()
    return run_id


def ledger(s: Session, run_id: int, ts: datetime, amount: Decimal, kind: str, ref: str) -> None:
    d = et_date(ts)
    s.add(
        m.CashLedger(
            run_id=run_id,
            ts=ts,
            trade_date=d,
            settle_date=d,
            currency="USD",
            amount=amount,
            kind=kind,
            ref=ref,
        )
    )


def _order(s: Session, run_id: int, sym: int, side: str, purpose: str, qty: int, ts: datetime) -> m.Order:
    o = m.Order(
        run_id=run_id,
        symbol_id=sym,
        side=side,
        order_type="market",
        purpose=purpose,
        qty=qty,
        tif="day",
        status="filled",
        reason="test",
        session_date=et_date(ts),
        submitted_at=ts,
        closed_at=ts,
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    return o


def fill(
    s: Session,
    run_id: int,
    sym: int,
    side: str,
    purpose: str,
    qty: int,
    price: Decimal,
    fees: Fees,
    ts: datetime,
) -> tuple[m.Order, m.Fill]:
    """One filled order with its ledger rows, as `SimBroker._fill` writes them."""
    o = _order(s, run_id, sym, side, purpose, qty, ts)
    f = m.Fill(
        run_id=run_id,
        order_id=o.id,
        ts=ts,
        qty=qty,
        price=price,
        fees=fees.to_json(),
        quote_snapshot={},
        slippage=Decimal("0.01"),
    )
    s.add(f)
    s.flush()
    value = q4(price * qty)
    ledger(s, run_id, ts, -value if side == "buy" else value, side, f"fill:{f.id}")
    if fees.total > 0:
        ledger(s, run_id, ts, -q4(fees.total), "fee", f"fill:{f.id}:fees")
    return o, f


def open_position(
    s: Session,
    run_id: int,
    sym: int,
    *,
    qty: int,
    price: Decimal,
    ts: datetime,
    fees: Fees = NO_FEES,
    stop: Decimal | None = None,
) -> int:
    o, _ = fill(s, run_id, sym, "buy", "entry", qty, price, fees, ts)
    pos = m.Position(
        run_id=run_id,
        symbol_id=sym,
        qty=qty,
        avg_price=price,
        stop_loss=stop,
        planned_risk=q4((price - stop) * qty) if stop is not None else None,
        session_date=et_date(ts),
        opened_at=ts,
        entry_order_id=o.id,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    o.position_id = pos.id
    return pos.id


def close_position(
    s: Session,
    position_id: int,
    *,
    price: Decimal,
    ts: datetime,
    fees: Fees = NO_FEES,
    pnl_r: Decimal | None = None,
) -> m.Trade:
    pos = s.get(m.Position, position_id)
    assert pos is not None
    entry = s.execute(select(m.Fill).where(m.Fill.order_id == pos.entry_order_id)).scalar_one()
    o, _ = fill(s, pos.run_id, pos.symbol_id, "sell", "exit", pos.qty, price, fees, ts)
    o.position_id = pos.id
    fees_total = Fees.from_json(entry.fees).total + fees.total
    pos.closed_at = ts
    trade = m.Trade(
        run_id=pos.run_id,
        position_id=pos.id,
        symbol_id=pos.symbol_id,
        session_date=et_date(ts),
        entry_price=pos.avg_price,
        exit_price=price,
        qty=pos.qty,
        pnl=q4((price - pos.avg_price) * pos.qty - fees_total),
        pnl_r=pnl_r,
        planned_risk=pos.planned_risk,
        exit_reason="test",
        slippage_total=Decimal("0.0200") * pos.qty,
        fees_total=fees_total,
        opened_at=pos.opened_at,
        closed_at=ts,
    )
    s.add(trade)
    s.flush()
    return trade


def round_trip(
    s: Session,
    run_id: int,
    sym: int,
    *,
    entry: str,
    exit_: str,
    qty: int,
    opened: datetime,
    closed: datetime,
    entry_fees: Fees = NO_FEES,
    exit_fees: Fees = NO_FEES,
    pnl_r: str | None = None,
) -> m.Trade:
    pid = open_position(s, run_id, sym, qty=qty, price=Decimal(entry), ts=opened, fees=entry_fees)
    return close_position(
        s,
        pid,
        price=Decimal(exit_),
        ts=closed,
        fees=exit_fees,
        pnl_r=Decimal(pnl_r) if pnl_r is not None else None,
    )


def fees(total: str) -> Fees:
    return Fees(ecn=Decimal(total))


# --- 4. realized per period and the metrics -----------------------------------------------------------------


@dataclass
class Seeded:
    run_id: int
    sym: int


def _week_of_trades(factory: sessionmaker[Session]) -> Seeded:
    with factory() as s:
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        old = seed_run(s, status="completed", started_at=datetime(2026, 9, 1, 12, tzinfo=UTC))
        replay = seed_run(s, mode="replay", status="completed")
        run = seed_run(s)
        # previous Friday, Monday, Wednesday x2 (one without R)
        round_trip(
            s,
            run,
            sym,
            entry="10.00",
            exit_="12.00",
            qty=10,
            opened=et(2026, 10, 2, 9, 40),
            closed=et(2026, 10, 2, 10, 0),
            pnl_r="1.0000",
        )
        round_trip(
            s,
            run,
            sym,
            entry="20.00",
            exit_="18.75",
            qty=10,
            opened=et(2026, 10, 5, 9, 40),
            closed=et(2026, 10, 5, 11, 0),
            pnl_r="-0.5000",
        )
        round_trip(
            s,
            run,
            sym,
            entry="10.00",
            exit_="10.725",
            qty=10,
            opened=et(2026, 10, 7, 9, 40),
            closed=et(2026, 10, 7, 10, 0),
            pnl_r="0.3000",
        )
        round_trip(
            s,
            run,
            sym,
            entry="10.00",
            exit_="9.70",
            qty=10,
            opened=et(2026, 10, 7, 9, 45),
            closed=et(2026, 10, 7, 10, 30),
        )
        # other runs on the same Wednesday: never counted
        round_trip(
            s,
            replay,
            sym,
            entry="10.00",
            exit_="110.00",
            qty=10,
            opened=et(2026, 10, 7, 9, 40),
            closed=et(2026, 10, 7, 10, 0),
            pnl_r="9.0000",
        )
        round_trip(
            s,
            old,
            sym,
            entry="10.00",
            exit_="60.00",
            qty=10,
            opened=et(2026, 10, 7, 9, 40),
            closed=et(2026, 10, 7, 10, 0),
            pnl_r="5.0000",
        )
        s.commit()
    return Seeded(run, sym)


def test_realized_per_period_counts_only_the_live_run(db_factory: sessionmaker[Session]) -> None:
    seeded = _week_of_trades(db_factory)
    windows = period_windows(CAL, et(2026, 10, 7, 14), RUN_START)
    today, week, run = period_blocks(db_factory, seeded.run_id, windows, [])
    assert [b.period for b in (today, week, run)] == ["today", "week", "run"]
    assert today.realized == Decimal("4.2500")  # 7.25 - 3.00
    assert week.realized == Decimal("-8.2500")  # -12.50 + 4.25
    assert run.realized == Decimal("11.7500")  # 20.00 - 8.25
    for block in (today, week, run):
        assert block.unrealized is None and block.unrealized_partial is False
        assert block.pnl_after_fees == block.realized
        metrics = compute_metrics(db_factory, seeded.run_id, block.date_from, block.date_to)
        assert (block.trades, block.wins, block.losses) == (metrics.trades, metrics.wins, metrics.losses)
        assert block.win_rate == metrics.win_rate
        assert block.expectancy_r == metrics.expectancy_r
        assert block.trades_without_r == metrics.trades_without_r
    assert (today.trades, today.wins, today.losses, today.trades_without_r) == (2, 1, 1, 1)
    assert today.win_rate == Decimal("0.5000") and today.expectancy_r == Decimal("0.3000")
    assert (run.trades, week.trades) == (4, 3)


# --- 5. fees ------------------------------------------------------------------------------------------------


def test_fees_follow_fill_time_in_et_bounds_across_dst(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "MSFT", questrade_id=9)
        run = seed_run(s, started_at=datetime(2026, 10, 20, 12, tzinfo=UTC))
        other = seed_run(s, mode="replay", status="completed")
        sunday_end = datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC)  # Sun 11-01 23:59:59 EST
        monday = datetime(2026, 11, 2, 5, 0, 0, tzinfo=UTC)  # Mon 11-02 00:00:00 EST
        fill(
            s, run, sym, "buy", "entry", 1, Decimal("10"), Fees(sec=Decimal("0.00005")), sunday_end
        )  # 0.0001
        fill(s, run, sym, "buy", "entry", 1, Decimal("10"), fees("0.2000"), monday)
        fill(s, run, sym, "buy", "entry", 1, Decimal("10"), fees("0.0400"), et(2026, 10, 28, 10))
        fill(s, other, sym, "buy", "entry", 1, Decimal("10"), fees("9.0000"), et(2026, 10, 28, 10))
        s.commit()
    started = datetime(2026, 10, 20, 12, tzinfo=UTC)
    _, week_oct26, run_oct = period_blocks(
        db_factory, run, period_windows(CAL, et(2026, 11, 1, 12), started), []
    )
    assert week_oct26.fees == Decimal("0.0401")
    assert run_oct.fees == Decimal("0.0401")  # the run window ends with Sunday 11-01 (ET)
    today_nov2, week_nov2, _ = period_blocks(
        db_factory, run, period_windows(CAL, et(2026, 11, 2, 12), started), []
    )
    assert week_nov2.fees == Decimal("0.2000") and today_nov2.fees == Decimal("0.2000")


def test_fees_are_not_subtracted_twice(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "NVDA", questrade_id=10)
        run = seed_run(s)
        trade = round_trip(
            s,
            run,
            sym,
            entry="20.00",
            exit_="18.785",
            qty=10,
            opened=et(2026, 10, 7, 9, 40),
            closed=et(2026, 10, 7, 10, 0),
            entry_fees=fees("0.1000"),
            exit_fees=fees("0.2500"),
        )
        s.commit()
        assert trade.pnl == Decimal("-12.5000")
    today, _, _ = period_blocks(db_factory, run, period_windows(CAL, et(2026, 10, 7, 14), RUN_START), [])
    assert today.realized == Decimal("-12.5000")
    assert today.fees == Decimal("0.3500")
    assert today.pnl_after_fees == Decimal("-12.5000")


# --- 6. Claude spend ----------------------------------------------------------------------------------------


def _catalyst(s: Session, sym: int, day: date, cost: str) -> None:
    ts = et(day.year, day.month, day.day, 8)
    s.add(
        m.Catalyst(
            symbol_id=sym,
            session_date=day,
            headlines=[],
            catalyst_type="news",
            direction="up",
            cost_usd=Decimal(cost),
            created_at=ts,
        )
    )


def _weekly(s: Session, run_id: int, week_ending: date, updated_at: datetime, cost: str) -> None:
    s.add(
        m.WeeklyReport(
            week_ending=week_ending,
            week_start=week_ending - timedelta(days=4),
            run_id=run_id,
            facts={},
            commentary_status="ok",
            cost_usd=Decimal(cost),
            created_at=updated_at,
            updated_at=updated_at,
        )
    )


def test_claude_spend_per_period_and_today(db_factory: sessionmaker[Session]) -> None:
    now = et(2026, 10, 10, 12)  # Saturday: today = Friday 10-09, week 10-05..10-10
    with db_factory() as s:
        sym = add_symbol(s, "AMD", questrade_id=11)
        run = seed_run(s)
        _catalyst(s, sym, date(2026, 10, 5), "0.200000")
        _catalyst(s, sym, date(2026, 10, 8), "0.300000")
        _catalyst(s, sym, date(2026, 10, 9), "0.400012")
        _weekly(s, run, date(2026, 10, 2), et(2026, 10, 3, 9), "0.070000")
        _weekly(s, run, date(2026, 10, 9), et(2026, 10, 10, 11), "0.050000")
        round_trip(
            s,
            run,
            sym,
            entry="10.00",
            exit_="11.00",
            qty=10,
            opened=et(2026, 10, 9, 9, 40),
            closed=et(2026, 10, 9, 10, 0),
        )
        s.commit()
    today, week, run_b = period_blocks(db_factory, run, period_windows(CAL, now, RUN_START), [])
    assert today.claude_usd == Decimal("0.4000")
    assert week.claude_usd == Decimal("0.9500")
    assert run_b.claude_usd == Decimal("1.0200")
    for block in (today, week, run_b):
        assert block.net_after_ai == block.pnl_after_fees - block.claude_usd
    assert today.net_after_ai == Decimal("9.6000")
    settings = RuntimeSettings()
    ct = claude_today(db_factory, now, settings)
    assert ct.date == date(2026, 10, 10)
    assert ct.spent_usd == claude_spent(db_factory, et_date(now)) == Decimal("0.05")
    assert ct.cap_usd == settings.claude_daily_budget_usd
    assert ct.used_fraction == q4(Decimal("0.05") / settings.claude_daily_budget_usd)


def test_claude_today_with_a_zero_cap_has_no_fraction(db_factory: sessionmaker[Session]) -> None:
    settings = RuntimeSettings().model_copy(update={"claude_daily_budget_usd": Decimal(0)})
    ct = claude_today(db_factory, et(2026, 10, 7, 12), settings)
    assert ct.spent_usd == Decimal(0) and ct.used_fraction is None


# --- 7 (db) and 8. unrealized and the invariant -------------------------------------------------------------


def test_run_pnl_after_fees_equals_equity_at_marks_less_starting_cash(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        a = add_symbol(s, "AAA", questrade_id=21)
        b = add_symbol(s, "BBB", questrade_id=22)
        run = seed_run(s)
        round_trip(
            s,
            run,
            a,
            entry="10.0100",
            exit_="10.4700",
            qty=37,
            opened=et(2026, 10, 5, 9, 40),
            closed=et(2026, 10, 5, 15, 50),
            entry_fees=fees("0.1295"),
            exit_fees=Fees(ecn=Decimal("0.1295"), sec=Decimal("0.0080")),
        )
        round_trip(
            s,
            run,
            b,
            entry="33.3300",
            exit_="32.1100",
            qty=7,
            opened=et(2026, 10, 6, 9, 40),
            closed=et(2026, 10, 6, 10, 20),
            entry_fees=fees("0.0245"),
            exit_fees=Fees(ecn=Decimal("0.0245"), sec=Decimal("0.0046")),
        )
        p1 = open_position(
            s, run, a, qty=13, price=Decimal("10.2300"), ts=et(2026, 10, 7, 9, 41), fees=fees("0.0455")
        )
        p2 = open_position(
            s, run, b, qty=9, price=Decimal("31.0700"), ts=et(2026, 10, 7, 9, 43), fees=fees("0.0315")
        )
        s.commit()
        cash = s.execute(select(func.sum(m.CashLedger.amount)).where(m.CashLedger.run_id == run)).scalar_one()
    marks = {p1: Decimal("10.3350"), p2: Decimal("30.9990")}
    values = [
        OpenValue(
            position_id=p1,
            symbol_id=a,
            qty=13,
            avg_price=Decimal("10.2300"),
            mark=marks[p1],
            entry_fees=Decimal("0.0455"),
        ),
        OpenValue(
            position_id=p2,
            symbol_id=b,
            qty=9,
            avg_price=Decimal("31.0700"),
            mark=marks[p2],
            entry_fees=Decimal("0.0315"),
        ),
    ]
    equity_at_marks = cash + marks[p1] * 13 + marks[p2] * 9
    blocks = period_blocks(db_factory, run, period_windows(CAL, et(2026, 10, 7, 14), RUN_START), values)
    run_block = blocks[2]
    assert run_block.unrealized == q4(
        (marks[p1] - Decimal("10.23")) * 13
        - Decimal("0.0455")
        + (marks[p2] - Decimal("31.07")) * 9
        - Decimal("0.0315")
    )
    assert run_block.unrealized_partial is False
    assert run_block.pnl_after_fees == equity_at_marks - CASH
    assert all(b.unrealized == run_block.unrealized for b in blocks)  # the same open value in every period
    partial = period_blocks(
        db_factory,
        run,
        period_windows(CAL, et(2026, 10, 7, 14), RUN_START),
        [values[0], OpenValue(p2, b, 9, Decimal("31.07"), None, Decimal("0.0315"))],
    )
    assert partial[0].unrealized_partial is True
    assert partial[2].unrealized == q4((marks[p1] - Decimal("10.23")) * 13 - Decimal("0.0455"))
    unmarked = period_blocks(
        db_factory,
        run,
        period_windows(CAL, et(2026, 10, 7, 14), RUN_START),
        [OpenValue(p2, b, 9, Decimal("31.07"), None, Decimal("0.0315"))],
    )
    assert unmarked[2].unrealized is None and unmarked[2].pnl_after_fees == unmarked[2].realized


# --- 9. empty run -------------------------------------------------------------------------------------------


def test_an_empty_run_gives_zero_blocks(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = seed_run(s)
        s.commit()
    blocks = period_blocks(db_factory, run, period_windows(CAL, et(2026, 10, 7, 14), RUN_START), [])
    assert len(blocks) == 3
    for b in blocks:
        assert (b.realized, b.pnl_after_fees, b.fees, b.claude_usd, b.net_after_ai) == (0, 0, 0, 0, 0)
        assert (b.trades, b.wins, b.losses, b.trades_without_r) == (0, 0, 0, 0)
        assert b.win_rate is None and b.expectancy_r is None and b.unrealized is None


# --- 16. equity series --------------------------------------------------------------------------------------

DAY = date(2026, 10, 6)  # Tuesday: open 13:30Z, close 20:00Z
OPEN = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)
ENTRY_AT = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
EXIT_AT = datetime(2026, 10, 6, 14, 10, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 14, 30, tzinfo=UTC)


def _snapshot(s: Session, run_id: int, ts: datetime, equity: str) -> None:
    e = Decimal(equity)
    s.add(
        m.EquitySnapshot(
            run_id=run_id, ts=ts, equity=e, cash=e, settled_cash=e, peak_equity=e, drawdown_pct=Decimal(0)
        )
    )


def _bar_close(i: int) -> Decimal:
    return Decimal("10.0000") + Decimal("0.0100") * i


@dataclass
class EquityDay:
    run_id: int
    sym: int
    other_run: int


def _equity_day(factory: sessionmaker[Session], *, with_prior_snapshot: bool = True) -> EquityDay:
    with factory() as s:
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        replay = seed_run(s, mode="replay", status="completed")
        run = seed_run(s)
        if with_prior_snapshot:
            _snapshot(s, run, datetime(2026, 10, 5, 20, 0, tzinfo=UTC), "10010.0000")
        _snapshot(s, run, datetime(2026, 10, 6, 13, 45, 30, tzinfo=UTC), "9999.9000")
        _snapshot(s, run, datetime(2026, 10, 6, 14, 15, 30, tzinfo=UTC), "10003.1000")
        _snapshot(s, replay, datetime(2026, 10, 6, 14, 0, 30, tzinfo=UTC), "55555.0000")
        pid = open_position(s, run, sym, qty=100, price=Decimal("10.0000"), ts=ENTRY_AT, fees=fees("0.3500"))
        close_position(s, pid, price=Decimal("10.3500"), ts=EXIT_AT, fees=fees("0.3500"))
        for i in range(30):  # the 30 minutes 13:40..14:09 the position was open
            start = ENTRY_AT + timedelta(minutes=i)
            c = _bar_close(i)
            s.add(
                m.MarkBar(
                    run_id=run,
                    symbol_id=sym,
                    minute_start=start,
                    open=c,
                    high=c,
                    low=c,
                    close=c,
                    samples=3,
                    updated_at=start + timedelta(seconds=59),
                )
            )
            s.add(
                m.MarkBar(
                    run_id=replay,
                    symbol_id=sym,
                    minute_start=start,
                    open=c * 2,
                    high=c * 2,
                    low=c * 2,
                    close=c * 2,
                    samples=3,
                    updated_at=start,
                )
            )
        candle = Decimal("10.5000")
        s.add(
            m.IntradayCandle(
                symbol_id=sym,
                interval="1m",
                ts=ENTRY_AT + timedelta(minutes=10),
                open=candle,
                high=candle,
                low=candle,
                close=candle,
                volume=100,
            )
        )
        archived = Decimal("10.6000")
        s.add(
            m.CandleArchive(
                symbol_id=sym,
                interval="1m",
                start_ts=ENTRY_AT + timedelta(minutes=15),
                open=archived,
                high=archived,
                low=archived,
                close=archived,
                volume=100,
            )
        )
        s.add(
            m.IntradayCandle(
                symbol_id=sym,
                interval="5m",
                ts=ENTRY_AT + timedelta(minutes=20),
                open=candle,
                high=candle,
                low=candle,
                close=Decimal("99"),
                volume=100,
            )
        )  # not 1m: ignored
        s.commit()
    return EquityDay(run, sym, replay)


def test_equity_today_merges_snapshots_marks_and_now(db_factory: sessionmaker[Session]) -> None:
    day = _equity_day(db_factory)
    out = equity_series(db_factory, CAL, day.run_id, NOW, "today", Decimal("10003.1000"))
    assert out.range == "today" and out.downsampled is False
    assert out.start_equity == Decimal("10010.0000")
    snaps = [(p.ts, p.equity) for p in out.points if p.source == "snapshot"]
    assert snaps == [
        (datetime(2026, 10, 6, 13, 45, 30, tzinfo=UTC), Decimal("9999.9000")),
        (datetime(2026, 10, 6, 14, 15, 30, tzinfo=UTC), Decimal("10003.1000")),
    ]
    marks = [p for p in out.points if p.source == "marks"]
    assert len(marks) == 30
    cash_open = CASH - Decimal("1000.0000") - Decimal("0.3500")
    cash_closed = cash_open + Decimal("1035.0000") - Decimal("0.3500")
    for i, p in enumerate(marks):
        end = ENTRY_AT + timedelta(minutes=i + 1)
        assert p.ts == end
        if end >= EXIT_AT:  # closed by the end of the minute: cash only
            assert p.equity == cash_closed
            continue
        close = {10: Decimal("10.5000"), 15: Decimal("10.6000")}.get(i, _bar_close(i))
        assert p.equity == cash_open + 100 * close, i
    nows = [p for p in out.points if p.source == "now"]
    assert [(p.ts, p.equity) for p in nows] == [(NOW, Decimal("10003.1000"))]
    assert out.points[-1].source == "now"
    assert [p.ts for p in out.points] == sorted(p.ts for p in out.points)
    assert [(f.side, f.purpose, f.qty, f.price, f.ticker) for f in out.fills] == [
        ("buy", "entry", 100, Decimal("10.0000"), "AAPL"),
        ("sell", "exit", 100, Decimal("10.3500"), "AAPL"),
    ]
    assert all(f.position_id is not None for f in out.fills)


def test_equity_today_without_a_prior_snapshot_starts_at_starting_cash(
    db_factory: sessionmaker[Session],
) -> None:
    day = _equity_day(db_factory, with_prior_snapshot=False)
    out = equity_series(db_factory, CAL, day.run_id, NOW, "today", None)
    assert out.start_equity == CASH
    assert not [p for p in out.points if p.source == "now"]


def test_equity_run_has_snapshots_and_now_only(db_factory: sessionmaker[Session]) -> None:
    day = _equity_day(db_factory)
    out = equity_series(db_factory, CAL, day.run_id, NOW, "run", Decimal("10003.1000"))
    assert out.range == "run" and out.start_equity == CASH
    assert [p.source for p in out.points] == ["snapshot", "snapshot", "snapshot", "now"]
    assert out.points[0].equity == Decimal("10010.0000")
    assert len(out.fills) == 2


def test_equity_on_a_saturday_shows_friday_without_a_now_point(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        run = seed_run(s)
        friday_entry = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
        open_position(s, run, sym, qty=10, price=Decimal("10.0000"), ts=friday_entry)
        s.add(
            m.MarkBar(
                run_id=run,
                symbol_id=sym,
                minute_start=friday_entry,
                open=Decimal(11),
                high=Decimal(11),
                low=Decimal(11),
                close=Decimal(11),
                samples=1,
                updated_at=friday_entry,
            )
        )
        s.commit()
    saturday = et(2026, 10, 10, 12)
    out = equity_series(db_factory, CAL, run, saturday, "today", Decimal("10010.0000"))
    assert not [p for p in out.points if p.source == "now"]
    marks = [p for p in out.points if p.source == "marks"]
    # Friday 10:00 ET to the 16:00 close: 360 minutes, the first at the bar's close, the rest carried forward
    assert len(marks) == 360
    assert marks[0].equity == CASH - Decimal("100.0000") + Decimal("110.0000")
    assert marks[-1].ts == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    assert all(p.equity == marks[0].equity for p in marks)
    run_out = equity_series(db_factory, CAL, run, saturday, "run", Decimal("10010.0000"))
    assert [p.source for p in run_out.points] == ["now"]


def test_equity_values_at_cost_before_any_bar(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        run = seed_run(s)
        open_position(s, run, sym, qty=10, price=Decimal("10.0000"), ts=ENTRY_AT)
        s.commit()
    out = equity_series(db_factory, CAL, run, ENTRY_AT + timedelta(minutes=3), "today", None)
    marks = [p for p in out.points if p.source == "marks"]
    assert [p.equity for p in marks] == [CASH] * 3  # cash - 100 + 10 x avg 10.00


def test_equity_fill_markers_keep_the_newest_200(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        sym = add_symbol(s, "AAPL", questrade_id=8049)
        run = seed_run(s)
        for i in range(210):
            fill(s, run, sym, "buy", "entry", 1, Decimal("1.0000"), NO_FEES, OPEN + timedelta(seconds=i))
        s.commit()
    out = equity_series(db_factory, CAL, run, NOW, "run", None)
    assert len(out.fills) == 200
    assert out.fills[0].ts == OPEN + timedelta(seconds=10) and out.fills[-1].ts == OPEN + timedelta(
        seconds=209
    )
