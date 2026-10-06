"""OPTSIM T6: expiry, exercise, assignment, early assignment and adjustment freezes (`LifecycleEngine`),
against T1's `FakeBook` and `FakeOptionMarket`. No database: the session is a stand-in that records what was
added to it (the `event_log` rows)."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from tests.options.factories import EXPIRY
from tests.options.fakes import FakeBook, FakeOptionMarket
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.options.lifecycle import EVENT_SOURCE, LifecycleEngine, MissingClose
from trader.options.settings import OptionSettings
from trader.options.types import SOURCE_MANUAL, LifecycleEvent, OptionContract, StructureKind

CAL = SessionCalendar()
NOW = datetime(2026, 11, 20, 21, 20, tzinfo=UTC)  # 16:20 ET on the expiry day
LATER = date(2026, 12, 18)
D = Decimal

Leg = tuple[OptionContract | None, int, str]  # contract (None: shares), signed qty, price


class StubSession:
    """What the engine needs of a session: the lock statement is swallowed, added rows are kept."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add(self, row: Any) -> None:
        self.added.append(row)

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


class Env:
    def __init__(self, cash: str = "5000", **settings: Any) -> None:
        self.clock = FixedClock(NOW)
        self.market = FakeOptionMarket(self.clock)
        self.market.add_underlying("F", "15")
        self.book = FakeBook(cash, self.market.contracts)
        self.session = StubSession()
        self.closes: dict[tuple[str, date], Decimal] = {}
        self.engine = LifecycleEngine(
            cast(Any, lambda: self.session),
            self.clock,
            CAL,
            self.market,
            OptionSettings(**settings),
            1,
            lambda session: self.book,
            self._official_close,
        )

    async def _official_close(self, underlying: str, day: date) -> Decimal | None:
        return self.closes.get((underlying, day))

    def set_close(self, price: str, day: date = EXPIRY) -> None:
        self.closes[("F", day)] = D(price)

    def contract(self, strike: str, right: str, expiry: date = EXPIRY, **kw: Any) -> OptionContract:
        return self.market.add_contract("F", expiry, strike, cast(Any, right), **kw)

    def structure(
        self,
        kind: StructureKind,
        *legs: Leg,
        reserved: str = "0",
        cover: int | None = None,
        source: str = SOURCE_MANUAL,
        config: int | None = None,
    ) -> int:
        sid = self.book.add_structure(
            kind=kind,
            source=source,
            strategy_config_id=config,
            underlying="F",
            qty=1,
            entry_net=D(0),
            reserved_cash=D(reserved),
            cover_structure_id=cover,
            parent_structure_id=None,
            take_profit_net=None,
            meta={},
            ts=NOW,
        )
        for contract, qty, price in legs:
            if contract is None:
                self.book.apply(sid, "shares", None, qty, D(price), NOW)
            else:
                self.book.apply(sid, "option", contract.id, qty, D(price), NOW)
        return sid

    def logged(self, level: str) -> list[Any]:
        return [row for row in self.session.added if row.level == level and row.source == EVENT_SOURCE]

    def settled(self) -> list[str]:
        return [row.detail["settled"] for row in self.book.lifecycle]


def csp(env: Env, qty: int = -1, strike: str = "14.50", **kw: Any) -> int:
    put = env.contract(strike, "put", **kw.pop("contract", {}))
    return env.structure("csp", (put, qty, "0.45"), reserved=str(D(strike) * 100 * abs(qty)), **kw)


def covered_call(env: Env) -> int:
    shares = env.structure("shares", (None, 100, "14.50"))
    return env.structure("covered_call", (env.contract("15", "call"), -1, "0.30"), cover=shares)


def long_call(env: Env) -> int:
    return env.structure("long_call", (env.contract("14", "call"), 1, "1.00"))


def debit_spread(env: Env) -> int:
    return env.structure(
        "debit_spread", (env.contract("14", "call"), 1, "1.20"), (env.contract("15", "call"), -1, "0.50")
    )


def credit_spread(env: Env) -> int:
    legs: tuple[Leg, Leg] = (
        (env.contract("14", "call"), -1, "1.20"),
        (env.contract("15", "call"), 1, "0.50"),
    )
    return env.structure("credit_spread", *legs, reserved="100")


def summary(events: list[LifecycleEvent]) -> list[tuple[str, int, Decimal]]:
    return [(e.kind, e.shares_delta, e.cash_delta) for e in events]


# id, setup, cash, close, events (kind, shares, cash), how each settled, close reason (None: open), cash after
OUTCOMES: list[tuple[str, Callable[[Env], int], str, str | None, list[Any], list[str], str | None, str]] = [
    ("no_official_close", csp, "5000", None, [], [], None, "5000"),
    ("out_of_the_money", csp, "5000", "15", [("expired", 0, D(0))], ["expired"], "expired", "5000"),
    ("short_put_itm", csp, "5000", "14", [("assigned", 100, D(-1450))], ["shares"], "assigned", "3550"),
    (
        "covered_call_itm",
        covered_call,
        "5000",
        "16",
        [("called_away", -100, D(1500))],
        ["shares"],
        "called_away",
        "6500",
    ),
    (
        "long_itm_can_pay",
        long_call,
        "5000",
        "16",
        [("exercised", 100, D(-1400))],
        ["shares"],
        "exercised",
        "3600",
    ),
    (
        "long_itm_cannot_pay",
        long_call,
        "100",
        "16",
        [("exercised", 0, D(200))],
        ["intrinsic"],
        "exercised",
        "300",
    ),
    (
        "several_legs_itm",
        debit_spread,
        "5000",
        "16",
        [("exercised", 100, D(-1400)), ("assigned", -100, D(1500))],
        ["shares", "shares"],
        "assigned",
        "5100",
    ),
]


@pytest.mark.parametrize(
    ("setup", "cash", "close", "expected", "settled", "reason", "cash_after"),
    [row[1:] for row in OUTCOMES],
    ids=[row[0] for row in OUTCOMES],
)
async def test_expiry_outcomes(
    setup: Callable[[Env], int],
    cash: str,
    close: str | None,
    expected: list[Any],
    settled: list[str],
    reason: str | None,
    cash_after: str,
) -> None:
    env = Env(cash)
    sid = setup(env)
    if close is not None:
        env.set_close(close)
    events = await env.engine.run_expiry(EXPIRY)
    assert summary(events) == expected
    assert env.settled() == settled
    view = env.book.structure(sid)
    assert view.close_reason == reason
    assert view.state == ("open" if reason is None else "closed")
    assert env.book.cash() == D(cash_after)
    if reason is None:  # nothing changed, an error event, and the engine says what is waiting
        assert view.positions[0].qty == -1 and env.book.reserved() == D(1450)
        assert env.engine.missing_closes == [MissingClose("F", EXPIRY, (sid,))]
        assert len(env.logged("error")) == 1
    else:
        assert all(p.qty == 0 for p in view.positions) and env.book.reserved() == 0
        assert all(e.underlying_close == D(close or 0) and e.session_date == EXPIRY for e in events)


@pytest.mark.parametrize(
    ("right", "close", "kind"),
    [
        ("call", "15.00", "expired"),
        ("call", "15.01", "exercised"),
        ("call", "15.009", "expired"),
        ("put", "15.00", "expired"),
        ("put", "14.99", "exercised"),
        ("put", "14.991", "expired"),
    ],
)
async def test_itm_threshold_edges(right: str, close: str, kind: str) -> None:
    env = Env()
    env.structure("custom", (env.contract("15", right), 1, "0.20"))
    env.set_close(close)
    assert [e.kind for e in await env.engine.run_expiry(EXPIRY)] == [kind]


async def test_assignment_creates_shares_structure_at_strike_and_releases_reserve() -> None:
    env = Env()
    sid = csp(env, source="wheel", config=7)
    env.set_close("14")
    (event,) = await env.engine.run_expiry(EXPIRY)
    put = env.book.structure(sid)
    assert (put.state, put.close_reason, put.reserved_cash) == ("closed", "assigned", D(0))
    assert put.realized_pnl == D(45) and env.book.reserved() == 0
    (shares,) = env.book.structures()
    assert event.new_structure_id == shares.id and env.book.lifecycle[0].new_structure_id == shares.id
    assert (shares.kind, shares.source, shares.strategy_config_id) == ("shares", "wheel", 7)
    assert (shares.parent_structure_id, shares.qty, shares.entry_net) == (sid, 1, D("-14.50"))
    (lot,) = shares.positions
    assert (lot.instrument, lot.qty, lot.avg_price) == ("shares", 100, D("14.50"))
    assert (event.source, event.strategy_config_id, event.qty, event.strike) == ("wheel", 7, 1, D("14.50"))
    move = env.book.ledger[-1]
    assert (move.amount, move.kind, move.ref) == (D(-1450), "buy", f"opt_life:{event.id}")


async def test_called_away_closes_both_structures_and_moves_cash() -> None:
    env = Env()
    call_id = covered_call(env)
    shares_id = env.book.structure(call_id).cover_structure_id
    assert shares_id is not None
    env.set_close("16")
    (event,) = await env.engine.run_expiry(EXPIRY)
    call, shares = env.book.structure(call_id), env.book.structure(shares_id)
    assert (call.state, call.close_reason) == (shares.state, shares.close_reason) == ("closed", "called_away")
    assert shares.positions[0].qty == 0 and shares.realized_pnl == D(50)  # sold at the strike, 15
    assert call.realized_pnl == D(30) and event.new_structure_id is None
    assert env.book.cash() == D(6500) and env.book.ledger[-1].kind == "sell"


async def test_vertical_both_legs_itm_ends_in_cash_only() -> None:
    env = Env("50")  # far less than one leg's shares cost: the two legs net out
    sid = debit_spread(env)
    env.set_close("16")
    events = await env.engine.run_expiry(EXPIRY)
    assert [e.kind for e in events] == ["exercised", "assigned"] and env.settled() == ["shares", "shares"]
    assert env.book.cash() == D(150)  # bought at 14, sold at 15
    assert env.book.structures() == [] and len(env.book.structures(open_only=False)) == 1
    view = env.book.structure(sid)
    assert all(p.qty == 0 for p in view.positions)
    assert view.realized_pnl == D(100) - D(120) + D(50)


async def test_credit_spread_short_leg_only_itm() -> None:
    env = Env()
    sid = credit_spread(env)
    env.set_close("14.50")
    events = await env.engine.run_expiry(EXPIRY)
    # The long 15 call expires; the short 14 call has no shares to deliver: cash at intrinsic (R7).
    assert summary(events) == [("expired", 0, D(0)), ("assigned", 0, D(-50))]
    assert env.settled() == ["expired", "intrinsic"]
    view = env.book.structure(sid)
    assert (view.close_reason, view.reserved_cash) == ("assigned", D(0))
    assert env.book.cash() == D(4950) and len(env.book.structures(open_only=False)) == 1


@pytest.mark.parametrize(
    ("cash", "reserved_elsewhere", "settled", "cash_after"),
    [
        ("1400", "0", "shares", "0"),  # pays for the shares to the cent
        ("1399.99", "0", "intrinsic", "1599.99"),
        ("5000", "4000", "intrinsic", "5200"),  # cash held as another structure's reserve does not count
    ],
)
async def test_long_call_exercise_or_cash_settle_by_free_cash(
    cash: str, reserved_elsewhere: str, settled: str, cash_after: str
) -> None:
    env = Env(cash)
    long_call(env)
    if D(reserved_elsewhere):
        env.structure("csp", (env.contract("40", "put", LATER), -1, "1.00"), reserved=reserved_elsewhere)
    env.set_close("16")
    (event,) = await env.engine.run_expiry(EXPIRY)
    assert event.kind == "exercised" and env.settled() == [settled]
    assert env.book.cash() == D(cash_after)
    assert (event.new_structure_id is not None) == (settled == "shares")


async def test_assignment_fee_charged_once() -> None:
    env = Env(assignment_fee=D(5))
    assigned = csp(env, qty=-2)
    csp(env, strike="13")  # expires out of the money: no fee
    env.set_close("14")
    events = await env.engine.run_expiry(EXPIRY)
    assert summary(events) == [("assigned", 200, D(-2905)), ("expired", 0, D(0))]
    assert [m.amount for m in env.book.ledger if m.kind == "fee"] == [D(-5)]
    assert env.book.structure(assigned).fees_total == D(5)
    assert env.book.cash() == D(5000) - D(2905)


async def test_missing_close_changes_nothing_and_is_retried() -> None:
    env = Env()
    sid = csp(env)
    ledger = list(env.book.ledger)
    assert await env.engine.run_expiry(EXPIRY) == []
    assert env.book.ledger == ledger and env.book.lifecycle == []
    assert env.book.structure(sid).positions[0].qty == -1
    assert [m.underlying for m in env.engine.missing_closes] == ["F"]
    env.set_close("14")
    assert [e.kind for e in await env.engine.run_expiry(EXPIRY)] == ["assigned"]
    assert env.engine.missing_closes == []


async def test_second_run_is_a_no_op() -> None:
    env = Env()
    csp(env)
    covered_call(env)
    env.set_close("14")
    assert [e.kind for e in await env.engine.run_expiry(EXPIRY)] == ["assigned", "expired"]
    ledger, rows = list(env.book.ledger), len(env.book.lifecycle)
    assert await env.engine.run_expiry(EXPIRY) == []
    assert env.book.ledger == ledger and len(env.book.lifecycle) == rows

    # An event that already exists for a position: nothing else is done for it.
    other = Env()
    sid = csp(other)
    other.set_close("14")
    pos = other.book.structure(sid).positions[0]
    other.book.record_lifecycle(
        structure_id=sid,
        position_id=pos.id,
        contract_id=1,
        kind="assigned",
        session_date=EXPIRY,
        ts=NOW,
        underlying_close=None,
        strike=None,
        qty=1,
        shares_delta=0,
        cash_delta=D(0),
        detail={},
    )
    assert await other.engine.run_expiry(EXPIRY) == []
    assert other.book.structure(sid).positions[0].qty == -1 and other.book.cash() == D(5000)


async def test_expired_last_week_is_caught_up() -> None:
    env = Env()
    last_week = date(2026, 11, 13)
    sid = csp(env, contract={"expiry": last_week})
    env.set_close("14", last_week)  # in the money on its own expiry day
    env.set_close("15")  # today's close would say out of the money
    (event,) = await env.engine.run_expiry(EXPIRY)
    assert (event.kind, event.underlying_close, event.session_date) == ("assigned", D(14), EXPIRY)
    assert env.book.lifecycle[0].detail["close_date"] == "2026-11-13"
    assert env.book.structure(sid).close_reason == "assigned"


EARLY_DAY = date(2026, 11, 2)  # a Monday; the next session is Tuesday the 3rd
EARLY: list[tuple[str, dict[str, Any], bool]] = [
    ("fires", {}, True),
    ("no_mark_counts_as_zero", {"quote": None}, True),
    ("time_value_above_the_dividend", {"quote": ("1.20", "1.30")}, False),
    ("ex_date_two_sessions_out", {"ex": date(2026, 11, 4)}, False),
    ("out_of_the_money", {"close": "14.90", "quote": ("0.10", "0.20")}, False),
    ("disabled", {"enabled": False}, False),
    ("no_facts", {"facts": False}, False),
]


@pytest.mark.parametrize(("case", "fires"), [row[1:] for row in EARLY], ids=[row[0] for row in EARLY])
async def test_early_assignment(case: dict[str, Any], fires: bool) -> None:
    env = Env(early_assignment_enabled=case.get("enabled", True))
    call_id = covered_call(env)
    shares_id = env.book.structure(call_id).cover_structure_id
    assert shares_id is not None
    quote = case.get("quote", ("1.00", "1.10"))  # midpoint 1.05, intrinsic 1.00: time value 0.05
    if quote is not None:
        env.market.set_quote(env.book.structure(call_id).positions[0].contract.id, *quote)  # type: ignore[union-attr]
    if case.get("facts", True):
        env.market.set_facts(
            "F", next_ex_dividend_date=case.get("ex", date(2026, 11, 3)), dividend_per_share=D("0.15")
        )
    env.set_close(case.get("close", "16"), EARLY_DAY)
    events = await env.engine.run_early_assignment(EARLY_DAY)
    if not fires:
        assert events == [] and env.book.lifecycle == [] and env.book.cash() == D(5000)
        return
    assert summary(events) == [("early_assignment", -100, D(1500))]
    assert events[0].session_date == EARLY_DAY
    for structure_id in (call_id, shares_id):
        view = env.book.structure(structure_id)
        assert (view.state, view.close_reason) == ("closed", "called_away")
    assert env.book.cash() == D(6500)
    assert await env.engine.run_early_assignment(EARLY_DAY) == []


async def test_adjusted_contract_freezes_once_and_is_skipped() -> None:
    env = Env()
    sid = csp(env, contract={"adjusted": True})
    fine = csp(env, strike="13")
    (event,) = await env.engine.check_adjustments()
    assert (event.kind, event.structure_id, event.cash_delta, event.shares_delta) == ("frozen", sid, D(0), 0)
    assert env.book.structure(sid).frozen and not env.book.structure(fine).frozen
    assert len(env.logged("warning")) == 1  # the one alert
    assert await env.engine.check_adjustments() == []
    assert len(env.book.lifecycle) == 1 and len(env.logged("warning")) == 1
    env.set_close("12")  # both puts are in the money; the frozen one is left alone
    events = await env.engine.run_expiry(EXPIRY)
    assert [e.structure_id for e in events] == [fine]
    assert env.book.structure(sid).positions[0].qty == -1 and env.book.structure(sid).state == "open"


async def test_contract_missing_from_fresh_chain_freezes(monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env()
    env.clock.set(datetime(2026, 11, 2, 13, 15, tzinfo=UTC))  # before the expiry
    sid = csp(env)

    async def nothing(*args: Any) -> list[Any]:
        return []

    assert await env.engine.check_adjustments() == []  # listed in the chain
    monkeypatch.setattr(env.market, "strikes", nothing)
    monkeypatch.setattr(env.market, "expiries", nothing)
    assert await env.engine.check_adjustments() == []  # no chain at all: nothing was fetched
    monkeypatch.undo()
    monkeypatch.setattr(env.market, "strikes", nothing)
    assert [e.kind for e in await env.engine.check_adjustments()] == ["frozen"]
    assert env.book.structure(sid).frozen
