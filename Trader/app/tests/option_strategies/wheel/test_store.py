"""OPTSIM T11: the wheel's three tables through `WheelStore`."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.options import factories as f
from trader.market.clock import FixedClock
from trader.option_strategies.wheel.store import WheelStore

pytestmark = pytest.mark.db
D = Decimal


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(f.T0)


@pytest.fixture
def store(db_factory: sessionmaker[Session], clock: FixedClock) -> WheelStore:
    with db_factory() as s:
        run_id = f.add_options_run(s)
        symbol_id = f.add_underlying(s, "F")
        for _ in range(3):
            f.add_structure(s, run_id, symbol_id, source="wheel")
        s.commit()
    assert symbol_id == 1
    return WheelStore(db_factory, clock, run_id)


def test_ticker_rows_keep_the_owner_inputs_and_who_changed_them(store: WheelStore, clock: FixedClock) -> None:
    added = store.add_ticker(1, "F", origin="screen", actor="strategy:wheel")
    assert (added.status, added.origin, added.would_own, added.acknowledged) == (
        "candidate",
        "screen",
        None,
        frozenset(),
    )
    assert store.add_ticker(1, "F", origin="manual", actor="x").origin == "screen"  # already listed

    clock.advance(timedelta(hours=1))
    changed = store.update_ticker(
        "F", "owner:panel", status="approved", would_own=True, ownership_reason="solid", acknowledged={"size"}
    )
    assert changed is not None
    assert (changed.status, changed.ownership_reason, changed.acknowledged) == (
        "approved",
        "solid",
        frozenset({"size"}),
    )
    assert (changed.updated_by, changed.updated_at) == ("owner:panel", f.T0 + timedelta(hours=1))

    store.save_screen("F", "QUALIFIED", {"verdict": "QUALIFIED", "breakeven": D("48.50")})
    screened = store.ticker("F")
    assert screened is not None
    assert (screened.last_verdict, screened.last_screen) == (
        "QUALIFIED",
        {"verdict": "QUALIFIED", "breakeven": "48.50"},
    )
    assert screened.updated_by == "owner:panel"  # a screen is not an owner change
    assert [t.ticker for t in store.tickers("approved")] == ["F"]
    assert store.tickers("candidate") == [] and store.update_ticker("ZZZ", "x", status="approved") is None


def test_position_rows_follow_one_cycle(store: WheelStore) -> None:
    pos = store.open_put(
        symbol_id=1,
        ticker="F",
        structure_id=1,
        contracts=1,
        premium=D("1.50"),
        fees=D("0.99"),
        entry={"strike": D(50), "open_date": date(2026, 10, 6)},
    )
    assert (pos.state, pos.total_put_premium, pos.entry) == (
        "PUT_OPEN",
        D("1.50"),
        {"strike": "50", "open_date": "2026-10-06"},
    )
    assert store.by_structure(1) == pos and store.by_structure(2) is None
    assert store.cycles(1) == 0

    held = store.update_position(pos.id, state="SHARES_HELD", shares_structure_id=2, net_cost=D("48.50"))
    assert store.by_structure(2) == held and store.open_positions() == [held]

    closed = store.close_position(pos.id, "called_away", D("208.02"))
    assert (closed.state, closed.close_reason, closed.full_cycle_result, closed.closed_at) == (
        "NONE",
        "called_away",
        D("208.02"),
        f.T0,
    )
    assert store.open_positions() == [] and store.by_structure(2) is None
    assert store.cycles(1) == 1 and store.position(pos.id) == closed


def test_one_evaluation_per_position_and_session(store: WheelStore) -> None:
    pos = store.open_put(
        symbol_id=1, ticker="F", structure_id=1, contracts=1, premium=D(1), fees=D(0), entry={}
    )
    day = f.SESSION
    assert not store.evaluated(pos.id, day)
    assert store.add_event("evaluate", 1, day, "no rule applies", position_id=pos.id, action="HOLD")
    assert not store.add_event("evaluate", 1, day, "again", position_id=pos.id, action="CLOSE_PUT_NOW")
    assert store.add_event("action", 1, day, "after an answer", position_id=pos.id, action="SELL_CALL")
    assert store.add_event("alert", 1, day, "time exit", position_id=pos.id, data={"at": D("1.5")})
    assert store.add_event(
        "evaluate", 1, day + timedelta(days=1), "next day", position_id=pos.id, action="HOLD"
    )

    assert store.evaluated(pos.id, day)
    assert [(e.kind, e.action) for e in store.events(position_id=pos.id)] == [
        ("evaluate", "HOLD"),
        ("action", "SELL_CALL"),
        ("alert", None),
        ("evaluate", "HOLD"),
    ]
    assert store.events(kind="alert")[0].data == {"at": "1.5"}
    assert store.last_actions()[pos.id].reason == "next day"
