"""DB-T4 acceptance tests 2–9 (testcontainer): the positions part and the risk panel of `GET /api/live`.

Positions with marks from `quote_marks`, the working stop (Telegram's `/positions` logic), R and distance to
the stop, sparklines and expanded bars (stored 1-minute candles win over bars built from quotes), the open
values and equity at marks; the risk panel (open risk, slots) and the kill-switch lights with live values from
`KillSwitches.inputs`. All of it read-only.
"""

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from trader.api.livedata.positions import live_positions
from trader.api.livedata.risk import killswitch_lights, risk_panel, trading_state
from trader.api.livedata.types import SPARK_POINTS
from trader.api.views import MANUAL_PAUSE_CLEARS, killswitch_states
from trader.broker.ledger import Ledger
from trader.broker.types import Fees
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 7)  # a Wednesday session (EDT)
NOW = datetime(2026, 10, 7, 18, 30, tzinfo=UTC)  # 14:30 ET
OPENED = datetime(2026, 10, 7, 13, 40, 12, tzinfo=UTC)  # 09:40:12 ET
START = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
Q4 = Decimal("0.0001")
REVISIONS = itertools.count(100)
FEES = Fees(commission=Decimal("1.00"), ecn=Decimal("0.0035"), sec=Decimal("0.0000278"))


def q4(v: Decimal) -> Decimal:
    return v.quantize(Q4, ROUND_HALF_UP)


@dataclass
class Book:
    """Rows for one run, written the way the sim broker writes them."""

    s: Session
    run_id: int
    config_id: int

    def ledger(self, amount: Decimal, kind: str, ref: str, at: datetime = OPENED) -> None:
        self.s.add(
            m.CashLedger(
                run_id=self.run_id,
                ts=at,
                trade_date=at.date(),
                settle_date=at.date(),
                currency="USD",
                amount=amount,
                kind=kind,
                ref=ref,
            )
        )

    def order(self, symbol_id: int, **kw: Any) -> m.Order:
        row = m.Order(
            run_id=self.run_id,
            strategy_config_id=kw.pop("strategy_config_id", self.config_id),
            symbol_id=symbol_id,
            tif="day",
            reason=kw.pop("reason", "orb"),
            session_date=kw.pop("session_date", DAY),
            submitted_at=kw.pop("submitted_at", OPENED),
            stale_alerted=False,
            **kw,
        )
        self.s.add(row)
        self.s.flush()
        return row

    def open(
        self,
        symbol_id: int,
        *,
        qty: int = 100,
        entry: str = "10.00",
        stop_loss: str | None = "9.50",
        stop_order: str | None = None,
        stop_status: str = "working",
        opened_at: datetime = OPENED,
        target: Any = None,
        config_id: int | None = None,
        session_date: date = DAY,
    ) -> m.Position:
        price, sl = Decimal(entry), Decimal(stop_loss) if stop_loss is not None else None
        cfg = config_id if config_id is not None else self.config_id
        evidence = {"target": target} if target is not None else {"rvol": "2.1"}
        sig = m.Signal(
            run_id=self.run_id,
            strategy_config_id=cfg,
            symbol_id=symbol_id,
            session_date=session_date,
            event_key="orb",
            ts=opened_at,
            intent={"reason": "orb"},
            evidence=evidence,
        )
        self.s.add(sig)
        self.s.flush()
        prop = m.Proposal(
            run_id=self.run_id,
            signal_id=sig.id,
            kind="entry",
            order_spec={"symbol_id": symbol_id},
            qty=qty,
            status="executed",
            created_at=opened_at,
            expires_at=opened_at + timedelta(minutes=5),
            escalations=0,
        )
        self.s.add(prop)
        self.s.flush()
        entry_order = self.order(
            symbol_id,
            proposal_id=prop.id,
            strategy_config_id=cfg,
            side="buy",
            order_type="stop",
            purpose="entry",
            qty=qty,
            stop_price=price,
            stop_loss=sl,
            status="filled",
            closed_at=opened_at,
            session_date=session_date,
            submitted_at=opened_at,
        )
        fill = m.Fill(
            run_id=self.run_id,
            order_id=entry_order.id,
            ts=opened_at,
            qty=qty,
            price=price,
            fees=FEES.to_json(),
            quote_snapshot={},
            slippage=Decimal(0),
        )
        self.s.add(fill)
        self.s.flush()
        self.ledger(-q4(price * qty), "buy", f"fill:{fill.id}", opened_at)
        self.ledger(-FEES.total, "fee", f"fill:{fill.id}:fees", opened_at)
        planned = q4((price - sl) * qty) if sl is not None and price > sl else None
        pos = m.Position(
            run_id=self.run_id,
            symbol_id=symbol_id,
            strategy_config_id=cfg,
            qty=qty,
            avg_price=price,
            stop_loss=sl,
            planned_risk=planned,
            session_date=session_date,
            opened_at=opened_at,
            entry_order_id=entry_order.id,
            unprotected_seconds=3,
        )
        self.s.add(pos)
        self.s.flush()
        entry_order.position_id = pos.id
        if stop_order is not None:
            stop = self.order(
                symbol_id,
                position_id=pos.id,
                side="sell",
                order_type="stop",
                purpose="stop",
                qty=qty,
                stop_price=Decimal(stop_order),
                status=stop_status,
                reason="protective_stop",
                strategy_config_id=cfg,
            )
            pos.stop_order_id = stop.id
        self.s.flush()
        return pos

    def mark(
        self,
        symbol_id: int,
        last: str | None,
        age_s: float = 5,
        bid: str | None = None,
        ask: str | None = None,
    ) -> None:
        at = NOW - timedelta(seconds=age_s)
        self.s.merge(
            m.QuoteMark(
                run_id=self.run_id,
                symbol_id=symbol_id,
                bid=Decimal(bid) if bid else None,
                ask=Decimal(ask) if ask else None,
                last=Decimal(last) if last is not None else None,
                observed_at=at,
                written_at=at,
            )
        )

    def mark_bars(self, symbol_id: int, first: datetime, n: int, base: str = "10.00") -> None:
        for i in range(n):
            close = Decimal(base) + Decimal(i) / 100
            self.s.add(
                m.MarkBar(
                    run_id=self.run_id,
                    symbol_id=symbol_id,
                    minute_start=first + timedelta(minutes=i),
                    open=close,
                    high=close + Decimal("0.05"),
                    low=close - Decimal("0.05"),
                    close=close,
                    samples=30,
                    updated_at=first + timedelta(minutes=i, seconds=59),
                )
            )


def new_book(s: Session, *, mode: str = "live", status: str = "active", cash: str = "10000") -> Book:
    run_id = add_run(s, mode=mode, status=status, started_at=START, label=mode)
    cfg = m.StrategyConfig(
        strategy_key="orb_sip",
        version="1.0.0",
        revision=next(REVISIONS),
        params={"max_positions": 1},
        enabled=True,
        created_at=START,
        created_by="test",
        scope="replay",  # never the live settings the registry reads (the risk tests use the registry's rows)
    )
    s.add(cfg)
    s.add(
        m.SimAccount(
            run_id=run_id,
            currency="USD",
            starting_cash=Decimal(cash),
            source_amount=Decimal(cash),
            source_currency="USD",
            created_at=START,
        )
    )
    s.flush()
    book = Book(s, run_id, cfg.id)
    book.ledger(Decimal(cash), "deposit", "deposit", START)
    return book


def candle(symbol_id: int, at: datetime, close: str, *, archive: bool = False) -> Any:
    price = Decimal(close)
    common: dict[str, Any] = {
        "symbol_id": symbol_id,
        "interval": "1m",
        "open": price,
        "high": price + Decimal("0.10"),
        "low": price - Decimal("0.10"),
        "close": price,
        "volume": 1000,
    }
    return m.CandleArchive(start_ts=at, **common) if archive else m.IntradayCandle(ts=at, **common)


@pytest.fixture
def symbols(db_factory: sessionmaker[Session]) -> list[int]:
    with db_factory() as s:
        ids = [add_symbol(s, f"T{i:02d}", questrade_id=1000 + i) for i in range(24)]
        s.commit()
    return ids


class Statements:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.engine = factory.kw["bind"]
        self.seen: list[str] = []

    def _record(self, *args: Any) -> None:
        self.seen.append(args[2])

    def __enter__(self) -> "Statements":
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


@pytest.fixture
def registry(db_factory: sessionmaker[Session]) -> Iterator[StrategyRegistry]:
    reg = StrategyRegistry(
        db_factory, FixedClock(START), plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    )
    reg.ensure_defaults()
    yield reg


# --- 2. one position with a fresh mark -------------------------------------------------------------------


def test_a_fresh_mark_gives_r_distance_to_stop_and_the_working_stop(
    db_factory: sessionmaker[Session], symbols: list[int]
) -> None:
    with db_factory() as s:
        b = new_book(s)
        near = b.open(symbols[0], qty=100, entry="10.00", stop_loss="9.50", stop_order="9.60", target="11.25")
        cancelled = b.open(
            symbols[1], qty=50, entry="20.00", stop_loss="19.00", stop_order="19.40", stop_status="cancelled"
        )
        b.mark(symbols[0], "9.70", age_s=5, bid="9.69", ask="9.71")
        b.mark(symbols[1], "21.00", age_s=2)
        s.commit()
        run_id, near_id, cancelled_id = b.run_id, near.id, cancelled.id
    out = live_positions(db_factory, run_id, NOW, [])
    rows = {p.id: p for p in out.positions}
    p = rows[near_id]
    assert (p.ticker, p.strategy_key, p.side, p.qty, p.entry) == (
        "T00",
        "orb_sip",
        "long",
        100,
        Decimal("10"),
    )
    assert (p.mark, p.bid, p.ask, p.mark_state) == (Decimal("9.70"), Decimal("9.69"), Decimal("9.71"), "live")
    assert p.mark_at == NOW - timedelta(seconds=5)
    assert (p.stop, p.stop_working) == (Decimal("9.60"), True)  # the working stop order wins over stop_loss
    assert p.target == Decimal("11.25")
    assert p.planned_risk == Decimal("50")
    assert p.unrealized == Decimal("-30")  # (9.70 - 10.00) x 100, gross (Telegram's definition)
    assert p.unrealized_r == Decimal("-0.6")
    assert p.distance_to_stop_r == Decimal("0.2")  # (9.70 - 9.60) / (50 / 100)
    assert p.near_stop is True
    assert p.held_seconds == int((NOW - OPENED).total_seconds())
    assert p.opened_at == OPENED
    assert p.unprotected_seconds == 3
    assert p.link == f"/trades?position={near_id}"
    c = rows[cancelled_id]
    assert (c.stop, c.stop_working) == (Decimal("19.00"), False)  # no working stop: the stop loss
    assert c.target is None
    assert c.distance_to_stop_r == Decimal("2")  # (21 - 19) / (50 / 50)
    assert c.near_stop is False
    assert c.unrealized_r == Decimal("1")


# --- 3. stale and missing marks ----------------------------------------------------------------------------


def test_stale_and_missing_marks_keep_the_row(db_factory: sessionmaker[Session], symbols: list[int]) -> None:
    with db_factory() as s:
        b = new_book(s)
        stale = b.open(symbols[0])
        missing = b.open(symbols[1])
        b.mark(symbols[0], "10.40", age_s=45)
        s.commit()
        run_id, stale_id, missing_id = b.run_id, stale.id, missing.id
    rows = {p.id: p for p in live_positions(db_factory, run_id, NOW, []).positions}
    assert (rows[stale_id].mark_state, rows[stale_id].mark) == ("stale", Decimal("10.40"))
    assert rows[stale_id].unrealized == Decimal("40")
    m2 = rows[missing_id]
    assert (m2.mark_state, m2.mark, m2.mark_at, m2.unrealized) == ("missing", None, None, None)
    assert (m2.unrealized_r, m2.distance_to_stop_r, m2.near_stop) == (None, None, False)
    assert m2.spark == []


# --- 4. 0, 1 and 20 positions -------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 20])
def test_one_row_per_open_position_of_the_live_run(
    db_factory: sessionmaker[Session], symbols: list[int], n: int
) -> None:
    with db_factory() as s:
        b = new_book(s)
        for i in range(n):
            b.open(symbols[i])
            b.mark(symbols[i], "10.10")
            b.mark_bars(symbols[i], OPENED.replace(second=0), 100)
        closed = b.open(symbols[21])
        closed.closed_at = NOW - timedelta(minutes=5)
        other = new_book(s, mode="replay", status="completed")
        other.open(symbols[22])
        other.mark(symbols[22], "10.00")
        old = new_book(s, mode="live", status="completed")
        old.open(symbols[23])
        s.commit()
        run_id = b.run_id
    out = live_positions(db_factory, run_id, NOW, [])
    assert len(out.positions) == n
    assert {p.symbol_id for p in out.positions} == set(symbols[:n])
    for p in out.positions:
        assert 2 <= len(p.spark) <= SPARK_POINTS
        assert (p.spark[-1].ts, p.spark[-1].price) == (p.mark_at, p.mark)  # ends at the mark
        assert p.bars is None  # not expanded
        assert [f.purpose for f in p.fills] == ["entry"]
    assert len(out.open_values) == n


# --- 5. bars: candles win over mark bars; expand limits --------------------------------------------------


def test_expanded_bars_prefer_stored_candles_and_expand_is_capped(
    db_factory: sessionmaker[Session], symbols: list[int]
) -> None:
    first = OPENED.replace(second=0)  # 13:40Z
    with db_factory() as s:
        b = new_book(s)
        ps = [b.open(symbols[i]) for i in range(4)]
        for i in range(4):
            b.mark(symbols[i], "11.00")
        b.mark_bars(symbols[0], first - timedelta(minutes=5), 105)  # 5 minutes before the entry minute too
        s.add_all(candle(symbols[0], first + timedelta(minutes=20 + k), "50.00") for k in range(15))
        s.add_all(
            candle(symbols[0], first + timedelta(minutes=35 + k), "60.00", archive=True) for k in range(5)
        )
        s.add(candle(symbols[0], first + timedelta(minutes=20), "70.00", archive=True))  # intraday wins
        s.add(candle(symbols[0], NOW + timedelta(minutes=1), "80.00"))  # after now: never shown
        s.commit()
        run_id, ids = b.run_id, [p.id for p in ps]
    out = live_positions(db_factory, run_id, NOW, [ids[0], ids[1], ids[2], ids[3]])
    rows = {p.id: p for p in out.positions}
    assert [rows[i].bars is not None for i in ids] == [True, True, True, False]
    bars = rows[ids[0]].bars
    assert bars is not None and len(bars) == 100  # from the entry minute on, one per minute
    assert bars[0].start == first
    starts = [x.start for x in bars]
    assert starts == sorted(starts) and len(set(starts)) == 100
    by_start = {x.start: x for x in bars}
    for k in range(100):
        bar = by_start[first + timedelta(minutes=k)]
        if 20 <= k < 35:
            assert (bar.source, bar.close) == ("candle", Decimal("50.00")), k
        elif 35 <= k < 40:
            assert (bar.source, bar.close) == ("candle", Decimal("60.00")), k
        else:
            assert (bar.source, bar.close) == ("marks", Decimal("10.00") + Decimal(k + 5) / 100), k
    assert rows[ids[1]].bars == []  # expanded, nothing stored yet
    # an id that is not open is ignored and does not use a slot of its own beyond the first three given
    out2 = live_positions(db_factory, run_id, NOW, [999_999, ids[3]])
    rows2 = {p.id: p for p in out2.positions}
    assert rows2[ids[3]].bars is not None and all(rows2[i].bars is None for i in ids[:3])
    # the sparkline uses the same bars (candles included) and ends at the mark
    spark = rows[ids[0]].spark
    assert len(spark) <= SPARK_POINTS and spark[-1].price == Decimal("11.00")
    assert spark[-2].ts == bars[-1].start


def test_bars_keep_the_latest_390(db_factory: sessionmaker[Session], symbols: list[int]) -> None:
    opened = NOW - timedelta(minutes=500)
    with db_factory() as s:
        b = new_book(s)
        p = b.open(symbols[0], opened_at=opened)
        b.mark_bars(symbols[0], opened.replace(second=0), 450)
        s.commit()
        run_id, pid = b.run_id, p.id
    row = live_positions(db_factory, run_id, NOW, [pid]).positions[0]
    assert row.bars is not None and len(row.bars) == 390
    assert row.bars[-1].start == opened.replace(second=0) + timedelta(minutes=449)
    assert len(row.spark) <= SPARK_POINTS - 1 and row.spark[-1].ts == row.bars[-1].start  # no mark


# --- 6. open values and equity at marks --------------------------------------------------------------------


def test_open_values_and_equity_at_marks(db_factory: sessionmaker[Session], symbols: list[int]) -> None:
    with db_factory() as s:
        b = new_book(s)
        a = b.open(symbols[0], qty=100, entry="10.00")
        c = b.open(symbols[1], qty=40, entry="25.50")
        b.mark(symbols[0], "10.25")
        s.commit()
        run_id, a_id, c_id = b.run_id, a.id, c.id
    out = live_positions(db_factory, run_id, NOW, [])
    with db_factory() as s:
        cash = Ledger(CAL).balances(s, run_id, DAY).total
    assert cash == Decimal("10000") - Decimal("1000") - Decimal("1020") - 2 * q4(FEES.total)
    assert out.equity_at_marks == cash + Decimal("10.25") * 100 + Decimal("25.50") * 40  # avg when unmarked
    assert out.all_marked is False
    values = {v.position_id: v for v in out.open_values}
    assert values[a_id].mark == Decimal("10.25") and values[c_id].mark is None
    assert values[a_id].entry_fees == q4(FEES.total)
    assert (values[c_id].qty, values[c_id].avg_price, values[c_id].symbol_id) == (
        40,
        Decimal("25.5"),
        symbols[1],
    )
    with db_factory() as s:
        b2 = Book(s, run_id, 0)
        b2.mark(symbols[1], "26.00")
        s.commit()
    assert live_positions(db_factory, run_id, NOW, []).all_marked is True


def test_no_open_position_gives_the_ledger_cash(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        b = new_book(s, cash="2500")
        s.commit()
        run_id = b.run_id
    out = live_positions(db_factory, run_id, NOW, [1, 2])
    assert (out.positions, out.open_values, out.equity_at_marks, out.all_marked) == (
        [],
        [],
        Decimal("2500"),
        True,
    )


# --- 7. risk: open risk and slots ---------------------------------------------------------------------------


def test_open_risk_and_slots(
    db_factory: sessionmaker[Session], symbols: list[int], registry: StrategyRegistry
) -> None:
    registry.update("orb_sip", params={"max_positions": 2}, actor="test")
    orb = registry.current("orb_sip").id
    overlay = registry.current("spy_overlay").id
    with db_factory() as s:
        b = new_book(s)
        b.open(symbols[0], qty=100, entry="10.00", stop_loss="9.50", stop_order="9.60", config_id=orb)
        b.open(
            symbols[1], qty=10, entry="20.00", stop_loss="19.00", stop_order="20.50", config_id=orb
        )  # trailed
        b.open(symbols[2], qty=5, entry="30.00", stop_loss=None, config_id=overlay)  # no stop, overlay
        yday = b.open(symbols[3], qty=5, entry="8.00", config_id=orb, session_date=DAY - timedelta(days=1))
        yday.closed_at = OPENED - timedelta(days=1)
        for i in range(3):
            b.mark(symbols[i], "10.00")
        s.commit()
        run_id = b.run_id
    positions = live_positions(db_factory, run_id, NOW, [])
    ks = KillSwitches(db_factory, FixedClock(NOW))
    settings = RuntimeSettings()
    out = risk_panel(ks, registry, db_factory, CAL, settings, run_id, NOW, positions)
    assert out.open_risk == Decimal("40")  # (10.00 - 9.60) x 100; the trailed stop adds 0
    assert out.slots_max == 2  # orb_sip's max_positions; the overlay adds nothing
    assert out.slots_used == 2  # today's entries of the entry strategy (not the overlay, not yesterday)
    assert out.open_positions == 3
    assert out.equity == positions.equity_at_marks
    assert out.open_risk_cap == (settings.risk_pct * positions.equity_at_marks * 2).quantize(Decimal("0.01"))
    assert [k.switch for k in out.killswitches] == [
        "daily_loss_pct",
        "max_drawdown_pct",
        "expectancy",
        "manual_pause",
    ]
    registry.update("orb_sip", enabled=False, actor="test")
    off = risk_panel(ks, registry, db_factory, CAL, settings, run_id, NOW, positions)
    assert (off.slots_max, off.slots_used, off.open_risk_cap) == (0, 0, None)


# --- 8. kill-switch lights ----------------------------------------------------------------------------------


def _snapshot(s: Session, run_id: int, at: datetime, equity: str, peak: str) -> None:
    s.add(
        m.EquitySnapshot(
            run_id=run_id,
            ts=at,
            equity=Decimal(equity),
            cash=Decimal(equity),
            settled_cash=Decimal(equity),
            peak_equity=Decimal(peak),
            drawdown_pct=Decimal(0),
        )
    )


def _trip(
    s: Session, run_id: int, switch: str, value: str | None = None, threshold: str | None = None
) -> None:
    s.add(
        m.KillSwitchEvent(
            run_id=run_id,
            switch=switch,
            session_date=DAY,
            tripped_at=NOW - timedelta(minutes=30),
            value=Decimal(value) if value else None,
            threshold=Decimal(threshold) if threshold else None,
        )
    )


def test_killswitch_lights_show_live_values_and_open_trips(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        b = new_book(s, cash="1000")
        _snapshot(s, b.run_id, datetime(2026, 10, 6, 20, 5, tzinfo=UTC), "1000", "1000")  # prior close
        for i in range(3):
            sym = add_symbol(s, f"X{i}")
            pos = b.open(sym, qty=1, entry="10", session_date=DAY)
            s.add(
                m.Trade(
                    run_id=b.run_id,
                    position_id=pos.id,
                    symbol_id=sym,
                    session_date=DAY,
                    entry_price=Decimal(10),
                    exit_price=Decimal(11),
                    qty=1,
                    pnl=Decimal(1),
                    pnl_r=Decimal("0.5"),
                    planned_risk=Decimal(2),
                    exit_reason="stop",
                    slippage_total=Decimal(0),
                    fees_total=Decimal(0),
                    opened_at=OPENED,
                    closed_at=OPENED + timedelta(minutes=10),
                )
            )
        s.flush()
        s.execute(update(m.Position).values(closed_at=OPENED + timedelta(minutes=10)))
        s.commit()
        run_id = b.run_id
    ks = KillSwitches(db_factory, FixedClock(NOW))
    settings = RuntimeSettings()
    lights = {
        k.switch: k for k in killswitch_lights(ks, db_factory, CAL, settings, run_id, NOW, Decimal("960"))
    }
    daily = lights["daily_loss_pct"]
    assert (daily.value, daily.threshold, daily.unit, daily.tripped) == (
        Decimal("0.04"),
        settings.killswitch_daily_loss_pct,
        "pct",
        False,
    )
    assert (daily.trip_value, daily.trip_threshold, daily.automatic, daily.needs_web_reset) == (
        None,
        None,
        True,
        False,
    )
    dd = lights["max_drawdown_pct"]
    assert (dd.value, dd.threshold, dd.unit) == (Decimal("0.04"), settings.killswitch_max_drawdown_pct, "pct")
    exp = lights["expectancy"]
    assert (exp.value, exp.threshold, exp.unit) == (
        Decimal("0.5"),
        settings.killswitch_expectancy_threshold_r,
        "r",
    )
    assert (exp.count, exp.count_min) == (3, settings.killswitch_expectancy_min_trades)
    pause = lights["manual_pause"]
    assert (pause.unit, pause.value, pause.threshold, pause.count, pause.automatic) == (
        "none",
        None,
        None,
        None,
        False,
    )
    assert trading_state(ks, run_id, DAY) == "running"

    with db_factory() as s:
        _trip(s, run_id, "max_drawdown_pct", "0.160000", "0.150000")
        s.commit()
    lights = {
        k.switch: k for k in killswitch_lights(ks, db_factory, CAL, settings, run_id, NOW, Decimal("960"))
    }
    states = {k.switch: k for k in killswitch_states(ks, db_factory, run_id, DAY)}
    dd = lights["max_drawdown_pct"]
    assert dd.tripped and dd.tripped_at == NOW - timedelta(minutes=30)
    assert (dd.trip_value, dd.trip_threshold) == (Decimal("0.16"), Decimal("0.15"))
    assert dd.value == Decimal("0.04")  # the live value is still shown beside the trip
    assert (dd.needs_web_reset, dd.clears, dd.label) == (
        states["max_drawdown_pct"].needs_web_reset,
        states["max_drawdown_pct"].clears,
        states["max_drawdown_pct"].label,
    )
    assert dd.needs_web_reset is True and dd.clears
    assert trading_state(ks, run_id, DAY) == "blocked"

    with db_factory() as s:
        _trip(s, run_id, "manual_pause")
        s.commit()
    assert trading_state(ks, run_id, DAY) == "paused"
    pause = {
        k.switch: k for k in killswitch_lights(ks, db_factory, CAL, settings, run_id, NOW, Decimal("960"))
    }["manual_pause"]
    assert pause.tripped and pause.clears == MANUAL_PAUSE_CLEARS


def test_on_a_weekend_the_lights_use_the_last_session(db_factory: sessionmaker[Session]) -> None:
    saturday = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)
    friday = date(2026, 10, 9)
    with db_factory() as s:
        b = new_book(s, cash="1000")
        _snapshot(s, b.run_id, datetime(2026, 10, 8, 20, 5, tzinfo=UTC), "1000", "1000")  # Thursday close
        _snapshot(s, b.run_id, datetime(2026, 10, 9, 20, 5, tzinfo=UTC), "990", "1000")  # Friday close
        s.add(
            m.KillSwitchEvent(
                run_id=b.run_id,
                switch="daily_loss_pct",
                session_date=friday,
                tripped_at=datetime(2026, 10, 9, 15, 0, tzinfo=UTC),
                value=Decimal("0.06"),
                threshold=Decimal("0.05"),
            )
        )
        s.commit()
        run_id = b.run_id
    ks = KillSwitches(db_factory, FixedClock(saturday))
    lights = {
        k.switch: k
        for k in killswitch_lights(ks, db_factory, CAL, RuntimeSettings(), run_id, saturday, Decimal("990"))
    }
    daily = lights["daily_loss_pct"]
    assert daily.tripped and daily.trip_value == Decimal("0.06")  # Friday's trip, shown for Friday
    assert daily.value == Decimal("0.01")  # vs Thursday's close: the start of Friday's session


# --- 9. read-only -------------------------------------------------------------------------------------------


def test_positions_risk_and_lights_only_select(
    db_factory: sessionmaker[Session], symbols: list[int], registry: StrategyRegistry
) -> None:
    with db_factory() as s:
        b = new_book(s)
        p = b.open(symbols[0], stop_order="9.60", target="12")
        b.mark(symbols[0], "10.10")
        b.mark_bars(symbols[0], OPENED.replace(second=0), 10)
        _trip(s, b.run_id, "expectancy", "-0.2", "0")
        s.commit()
        run_id, pid = b.run_id, p.id
    ks = KillSwitches(db_factory, FixedClock(NOW))
    with Statements(db_factory) as st:
        positions = live_positions(db_factory, run_id, NOW, [pid])
        risk_panel(ks, registry, db_factory, CAL, RuntimeSettings(), run_id, NOW, positions)
        killswitch_lights(ks, db_factory, CAL, RuntimeSettings(), run_id, NOW, positions.equity_at_marks)
        trading_state(ks, run_id, DAY)
    assert st.seen
    for sql in st.seen:
        assert sql.lstrip().upper().startswith("SELECT"), sql
        assert "FOR UPDATE" not in sql.upper() and "ADVISORY" not in sql.upper(), sql


# --- no per-row query (S14) ---------------------------------------------------------------------------------


def test_the_statement_count_does_not_grow_with_positions(
    db_factory: sessionmaker[Session], symbols: list[int], registry: StrategyRegistry
) -> None:
    counts: list[int] = []
    for n in (1, 20):
        with db_factory() as s:
            s.execute(update(m.Run).values(status="completed"))
            b = new_book(s)
            ids = []
            for i in range(n):
                ids.append(b.open(symbols[i], stop_order="9.60").id)
                b.mark(symbols[i], "10.10")
                b.mark_bars(symbols[i], OPENED.replace(second=0), 30)
            s.commit()
            run_id = b.run_id
        ks = KillSwitches(db_factory, FixedClock(NOW))
        with Statements(db_factory) as st:
            positions = live_positions(db_factory, run_id, NOW, ids[:3])
            risk_panel(ks, registry, db_factory, CAL, RuntimeSettings(), run_id, NOW, positions)
        assert len(positions.positions) == n
        counts.append(len(st.seen))
    assert counts[0] == counts[1], counts


def test_rows_of_another_run_never_leak(db_factory: sessionmaker[Session], symbols: list[int]) -> None:
    """A replay run holding the same symbol: its mark, bars and fills never reach the live rows."""
    with db_factory() as s:
        live = new_book(s)
        replay = new_book(s, mode="replay", status="completed")
        lp = live.open(symbols[0])
        replay.open(symbols[0], entry="50.00", stop_loss="49.00")
        replay.mark(symbols[0], "55.00")
        replay.mark_bars(symbols[0], OPENED.replace(second=0), 20, base="55.00")
        s.commit()
        run_id, lp_id = live.run_id, lp.id
    row = live_positions(db_factory, run_id, NOW, [lp_id]).positions[0]
    assert (row.mark, row.mark_state, row.bars, row.spark) == (None, "missing", [], [])
    assert len(row.fills) == 1 and row.fills[0].price == Decimal("10")
    with db_factory() as s:
        fills = s.execute(select(m.Fill.id)).scalars().all()
    assert len(fills) == 2
