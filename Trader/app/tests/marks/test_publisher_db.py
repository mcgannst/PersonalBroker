"""DB-T2 acceptance tests 7, 8, 9 and 11 (database part): what the `MarkPublisher` writes (testcontainer).

- Only symbols the live run holds or has working orders in get a `quote_marks` row (the newest observation;
  an older one arriving later never overwrites it) and 1-minute `mark_bars` (open kept, high/low widened,
  close from the latest observation, samples added up) (test 7).
- The run's bars older than 10 days are deleted once per ET day, other runs' bars untouched (test 8).
- Rows carry only the live run id `run_id()` gives; None writes nothing (test 9).
- A pass runs with `statement_timeout` and `lock_timeout` set and is not blocked by a trading row locked
  `FOR UPDATE` in another session (test 11).
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.factories import add_run, add_symbol
from tests.marks.test_publisher import Events, Tap
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.marks import publisher as pub_mod
from trader.marks.publisher import MarkPublisher, MarkPublisherDeps
from trader.marks.types import ObservedQuote

pytestmark = pytest.mark.db

DAY = date(2026, 10, 6)
T1 = datetime(2026, 10, 6, 14, 0, 10, tzinfo=UTC)
T2 = T1 + timedelta(seconds=10)
MINUTE = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 6, 14, 0, 30, tzinfo=UTC)


@dataclass
class World:
    live: int
    replay: int
    aaa: int  # held by the live run (qt 101)
    bbb: int  # held only by the replay run (qt 102)
    ccc: int  # a working order of the live run only (qt 103)
    position: int
    order: int


def _order(s: Session, run_id: int, symbol_id: int, status: str = "working") -> int:
    o = m.Order(
        run_id=run_id,
        symbol_id=symbol_id,
        side="buy",
        order_type="stop",
        purpose="entry",
        qty=10,
        stop_price=Decimal("30"),
        tif="day",
        status=status,
        reason="entry",
        session_date=DAY,
        submitted_at=T1,
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    return o.id


def _position(s: Session, run_id: int, symbol_id: int, *, closed: bool = False) -> int:
    entry = _order(s, run_id, symbol_id, "filled")
    p = m.Position(
        run_id=run_id,
        symbol_id=symbol_id,
        qty=10,
        avg_price=Decimal("10"),
        session_date=DAY,
        opened_at=T1,
        closed_at=T1 if closed else None,
        entry_order_id=entry,
        unprotected_seconds=0,
    )
    s.add(p)
    s.flush()
    return p.id


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    with db_factory() as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="running")
        aaa = add_symbol(s, "AAA", questrade_id=101)
        bbb = add_symbol(s, "BBB", questrade_id=102)
        ccc = add_symbol(s, "CCC", questrade_id=103)
        ddd = add_symbol(s, "DDD", questrade_id=104)
        position = _position(s, live, aaa)
        _position(s, live, ddd, closed=True)  # a closed position is not held
        _order(s, live, ddd, "cancelled")  # nor is a cancelled order working
        order = _order(s, live, ccc)
        _position(s, replay, bbb)
        s.commit()
    return World(live, replay, aaa, bbb, ccc, position, order)


def q(qid: int, last: str | None, at: datetime = T1, *, regular: str | None = None) -> ObservedQuote:
    price = Decimal(last) if last is not None else None
    quote = QtQuote(
        symbol_id=qid,
        symbol=f"Q{qid}",
        bid=(price - Decimal("0.01")) if price is not None else None,
        ask=(price + Decimal("0.01")) if price is not None else None,
        last=price,
        last_regular=Decimal(regular) if regular is not None else price,
        volume=100,
        last_trade_time=at - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
    )
    return ObservedQuote(qid, quote, at)


def publisher(
    factory: sessionmaker[Session], tap: Tap, run_id: Any, clock: FixedClock | None = None
) -> tuple[MarkPublisher, Events, FixedClock]:
    events = Events()
    clock = clock or FixedClock(NOW)
    return MarkPublisher(MarkPublisherDeps(factory, clock, tap, run_id, events)), events, clock


def marks(factory: sessionmaker[Session]) -> dict[tuple[int, int], m.QuoteMark]:
    with factory() as s:
        return {(r.run_id, r.symbol_id): r for r in s.execute(select(m.QuoteMark)).scalars()}


def bars(factory: sessionmaker[Session]) -> dict[tuple[int, int, datetime], m.MarkBar]:
    with factory() as s:
        return {(r.run_id, r.symbol_id, r.minute_start): r for r in s.execute(select(m.MarkBar)).scalars()}


# --- 7. what is written -------------------------------------------------------------------------------------


async def test_held_and_working_symbols_get_marks_and_bars(
    db_factory: sessionmaker[Session], world: World
) -> None:
    tap = Tap(
        [q(101, "10.00", T1), q(101, "10.50", T2), q(102, "20.00"), q(103, None, T1, regular="30.00")],
        [q(101, "9.00", T1 - timedelta(seconds=20))],  # older than the stored mark, arriving later
        [q(101, "9.90", T2 + timedelta(seconds=20))],
    )
    pub, events, clock = publisher(db_factory, tap, lambda: world.live)
    try:
        first = await pub.run_once()
        assert first.skipped is None and (first.marks_written, first.bars_written) == (2, 2)
        got = marks(db_factory)
        assert set(got) == {(world.live, world.aaa), (world.live, world.ccc)}  # BBB is not the live run's
        aaa = got[(world.live, world.aaa)]
        assert (aaa.bid, aaa.ask, aaa.last) == (Decimal("10.49"), Decimal("10.51"), Decimal("10.50"))
        assert aaa.observed_at == T2 and aaa.written_at == NOW and aaa.quote_time == T2 - timedelta(seconds=1)
        assert aaa.is_halted is False
        assert got[(world.live, world.ccc)].last == Decimal("30.00")  # last_regular when last is None
        bar = bars(db_factory)[(world.live, world.aaa, MINUTE)]
        assert (bar.open, bar.high, bar.low, bar.close, bar.samples) == (
            Decimal("10.00"),
            Decimal("10.50"),
            Decimal("10.00"),
            Decimal("10.50"),
            2,
        )
        assert bar.updated_at == NOW

        clock.advance(timedelta(seconds=2))
        await pub.run_once()
        aaa = marks(db_factory)[(world.live, world.aaa)]
        assert aaa.last == Decimal("10.50") and aaa.observed_at == T2  # the older observation lost
        earlier = bars(db_factory)[(world.live, world.aaa, MINUTE - timedelta(minutes=1))]
        assert (earlier.open, earlier.close, earlier.samples) == (Decimal("9.00"), Decimal("9.00"), 1)

        clock.advance(timedelta(seconds=2))
        await pub.run_once()
        bar = bars(db_factory)[(world.live, world.aaa, MINUTE)]
        assert (bar.open, bar.high, bar.low, bar.close, bar.samples) == (
            Decimal("10.00"),
            Decimal("10.50"),
            Decimal("9.90"),
            Decimal("9.90"),
            3,
        )
        assert marks(db_factory)[(world.live, world.aaa)].last == Decimal("9.90")
        assert pub.health_detail() == {"written_at": clock.now().isoformat(), "symbols": 1, "failing": False}
    finally:
        pub.close()
    assert events.rows == []


async def test_prices_that_are_missing_or_not_positive_make_no_bar(
    db_factory: sessionmaker[Session], world: World
) -> None:
    tap = Tap([q(101, None, T1), q(103, "0", T1)])
    pub, _, _ = publisher(db_factory, tap, lambda: world.live)
    try:
        step = await pub.run_once()
    finally:
        pub.close()
    assert step.skipped is None and (step.marks_written, step.bars_written) == (2, 0)
    assert bars(db_factory) == {}
    assert marks(db_factory)[(world.live, world.aaa)].last is None


# --- 8. retention -------------------------------------------------------------------------------------------


def _bar(factory: sessionmaker[Session], run_id: int, symbol_id: int, minute: datetime) -> None:
    with factory() as s:
        s.add(
            m.MarkBar(
                run_id=run_id,
                symbol_id=symbol_id,
                minute_start=minute,
                open=Decimal(1),
                high=Decimal(1),
                low=Decimal(1),
                close=Decimal(1),
                samples=1,
                updated_at=minute,
            )
        )
        s.commit()


async def test_old_bars_of_the_run_are_deleted_once_per_et_day(
    db_factory: sessionmaker[Session], world: World
) -> None:
    old = MINUTE - timedelta(days=11)
    recent = MINUTE - timedelta(days=9)
    _bar(db_factory, world.live, world.aaa, old)
    _bar(db_factory, world.live, world.aaa, recent)
    _bar(db_factory, world.replay, world.aaa, old)
    tap = Tap(every=[q(101, "10.00", T1)])
    pub, _, clock = publisher(db_factory, tap, lambda: world.live)
    try:
        await pub.run_once()
        kept = set(bars(db_factory))
        assert (world.live, world.aaa, old) not in kept
        assert {(world.live, world.aaa, recent), (world.replay, world.aaa, old)} <= kept

        _bar(db_factory, world.live, world.aaa, old - timedelta(minutes=1))
        clock.advance(timedelta(hours=1))
        await pub.run_once()  # the same ET day: no second deletion
        assert (world.live, world.aaa, old - timedelta(minutes=1)) in set(bars(db_factory))

        clock.advance(timedelta(days=1))
        await pub.run_once()  # the first write of the next ET day
        assert (world.live, world.aaa, old - timedelta(minutes=1)) not in set(bars(db_factory))
        assert (world.replay, world.aaa, old) in set(bars(db_factory))
    finally:
        pub.close()


# --- 9. run scoping -----------------------------------------------------------------------------------------


async def test_no_live_run_writes_nothing_and_rows_carry_the_given_run(
    db_factory: sessionmaker[Session], world: World
) -> None:
    tap = Tap([q(101, "10.00"), q(102, "20.00")], [q(101, "10.00"), q(102, "20.00")])
    pub, _, _ = publisher(db_factory, tap, lambda: None)
    try:
        assert (await pub.run_once()).skipped == "no_run"
    finally:
        pub.close()
    assert marks(db_factory) == {} and bars(db_factory) == {}
    pub, _, _ = publisher(db_factory, tap, lambda: world.live)
    try:
        await pub.run_once()
    finally:
        pub.close()
    assert {k[0] for k in marks(db_factory)} == {world.live}
    assert {k[0] for k in bars(db_factory)} == {world.live}
    assert (world.replay, world.bbb) not in marks(db_factory)


# --- 11. timeouts and no blocking on trading row locks ------------------------------------------------------


async def test_a_pass_sets_statement_and_lock_timeouts(
    db_factory: sessionmaker[Session], world: World
) -> None:
    seen: dict[str, str] = {}
    pub, _, _ = publisher(db_factory, Tap([q(101, "10.00")]), lambda: world.live)

    def show(s: Session, observed: Any, run_id: int, now: datetime) -> tuple[int, int]:
        seen["statement_timeout"] = s.execute(text("SHOW statement_timeout")).scalar_one()
        seen["lock_timeout"] = s.execute(text("SHOW lock_timeout")).scalar_one()
        return (0, 0)

    pub._apply = show  # type: ignore[method-assign]
    try:
        await pub.run_once()
    finally:
        pub.close()
    assert seen == {"statement_timeout": "5s", "lock_timeout": "5s"}
    with db_factory() as s:  # SET LOCAL: the pooled connection is back to the defaults
        assert s.execute(text("SHOW statement_timeout")).scalar_one() == "0"


async def test_trading_rows_locked_for_update_do_not_block_the_publisher(
    db_factory: sessionmaker[Session], world: World
) -> None:
    other = db_factory()
    try:
        other.execute(select(m.Position).where(m.Position.id == world.position).with_for_update())
        other.execute(select(m.Order).where(m.Order.id == world.order).with_for_update())
        # a row update that keeps the key (FOR NO KEY UPDATE, what a plain UPDATE takes) on the run's and the
        # symbols' rows does not conflict with the publisher's KEY SHARE either
        other.execute(select(m.Run).where(m.Run.id == world.live).with_for_update(key_share=True))
        other.execute(select(m.Symbol).where(m.Symbol.id == world.aaa).with_for_update(key_share=True))
        pub, _, _ = publisher(db_factory, Tap([q(101, "10.00"), q(103, "30.00")]), lambda: world.live)
        try:
            step = await asyncio.wait_for(pub.run_once(), timeout=3)
        finally:
            pub.close()
        assert step.skipped is None and step.marks_written == 2 and pub.busy_passes == 0
    finally:
        other.rollback()
        other.close()


# --- fix round 1 (gauntlet F2): a symbols or run row locked FOR UPDATE skips the pass without waiting -------


@pytest.mark.parametrize("locked", ["symbol", "run"])
async def test_a_referenced_row_locked_for_update_skips_the_pass_at_once_and_retries_it(
    db_factory: sessionmaker[Session], world: World, locked: str
) -> None:
    # nightly's upsert_symbols (ON CONFLICT DO UPDATE SET ticker, exchange: key columns) locks symbols rows
    # FOR UPDATE; the publisher must neither wait for it nor count it as a failure
    tap = Tap([q(101, "10.00", T1), q(103, "30.00", T1)], [q(101, "10.50", T2)])
    pub, events, clock = publisher(db_factory, tap, lambda: world.live)
    other = db_factory()
    try:
        if locked == "symbol":
            other.execute(select(m.Symbol).where(m.Symbol.id == world.ccc).with_for_update())
        else:
            other.execute(select(m.Run).where(m.Run.id == world.live).with_for_update())
        with capture_logs() as logs:
            step, took = await _timed(pub.run_once())
        assert took < 1.0  # NOWAIT: no lock_timeout (5 s), no deadlock_timeout (1 s) wait
        assert step.skipped is None and (step.marks_written, step.bars_written) == (0, 0)
        assert pub.busy_passes == 1 and pub.health_detail()["failing"] is False
        assert [e["event"] for e in logs if e["log_level"] != "debug"] == []  # no warning, no event
        assert marks(db_factory) == {} and bars(db_factory) == {}  # the pass was rolled back
        # the locker is untouched: it can still update the key it locked (no deadlock, no abort)
        if locked == "symbol":
            other.execute(text("UPDATE trader.symbols SET ticker = 'CCC' WHERE id = :id"), {"id": world.ccc})
        other.commit()
        # the next cadence writes the carried observations with the new ones
        clock.advance(timedelta(seconds=2))
        step = await pub.run_once()
        assert step.skipped is None and (step.marks_written, step.bars_written) == (2, 2)
    finally:
        other.rollback()
        other.close()
        pub.close()
    got = marks(db_factory)
    assert got[(world.live, world.aaa)].last == Decimal("10.50")
    assert got[(world.live, world.ccc)].last == Decimal("30.00")
    assert bars(db_factory)[(world.live, world.aaa, MINUTE)].samples == 2
    assert events.rows == [] and pub.busy_passes == 1


async def test_a_lock_timeout_on_the_mark_tables_is_still_a_failure_not_a_busy_pass(
    db_factory: sessionmaker[Session], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pub_mod, "PUBLISH_STATEMENT_TIMEOUT_MS", 300)
    pub, events, _ = publisher(db_factory, Tap([q(101, "10.00")]), lambda: world.live)
    other = db_factory()
    try:
        other.execute(text("LOCK TABLE trader.quote_marks IN ACCESS EXCLUSIVE MODE"))
        step = await pub.run_once()
    finally:
        other.rollback()
        other.close()
        pub.close()
    assert step.skipped == "error" and pub.busy_passes == 0
    assert [row[0] for row in events.rows] == ["warning"]


async def _timed(coro: Any) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = await asyncio.wait_for(coro, timeout=10)
    return out, time.perf_counter() - t0
