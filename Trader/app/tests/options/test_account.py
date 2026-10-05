"""OPTSIM T1: the options run and its account (task plan §3.6). One active options run beside the stock live
run; `start_options_run` creates the USD account, its one deposit and one audit row, and refuses while
something is open or inside the trading day; the stock live-run lookups never see the options run."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from tests.options import factories as f
from trader import runtime
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.options.account import (
    OptionsRunRefused,
    active_options_run_id,
    lock_book,
    options_account,
    start_options_run,
)
from trader.options.settings import OptionSettings
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
TUE = date(2026, 9, 29)  # a session day
SAT = date(2026, 10, 3)
D = Decimal


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


NIGHT = et(date(2026, 9, 28), 23, 45)  # Monday 23:45 ET: outside the block
SETTINGS = OptionSettings()


def _start(factory: sessionmaker[Session], now: datetime = NIGHT, cash: str = "5000") -> int:
    return start_options_run(factory, FixedClock(now), CAL, SETTINGS, cash=D(cash), actor="cli:test").run_id


def test_one_active_options_run_beside_the_live_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        live = add_run(s, mode="live", label="live")
        first = f.add_options_run(s)
        f.add_options_run(s, status="completed")  # retired runs never collide
        s.commit()
        with pytest.raises(IntegrityError, match="uq_runs_one_active_options"):
            f.add_options_run(s)
        s.rollback()
        with pytest.raises(IntegrityError, match="uq_runs_one_active_live"):
            add_run(s, mode="live")
        s.rollback()
        add_run(s, mode="replay", status="running")  # other modes are unaffected
        s.commit()
        active = s.execute(select(m.Run.id, m.Run.mode).where(m.Run.status == "active")).all()
    assert sorted(active) == sorted([(live, "live"), (first, "options")])
    assert active_options_run_id(db_factory) == first


def test_start_options_run_creates_account_deposit_and_audit(db_factory: sessionmaker[Session]) -> None:
    assert active_options_run_id(db_factory) is None
    out = start_options_run(db_factory, FixedClock(NIGHT), CAL, SETTINGS, cash=D("5000"), actor="cli:test")
    assert out.retired_run_id is None and active_options_run_id(db_factory) == out.run_id
    acct = options_account(db_factory, out.run_id)
    assert acct is not None and acct.id == out.account_id
    assert (acct.currency, acct.starting_cash, acct.source_amount) == ("USD", D("5000.0000"), D("5000.0000"))
    assert (acct.source_currency, acct.fx_rate, acct.fx_fee) == ("USD", None, None)
    with db_factory() as s:
        run = s.get_one(m.Run, out.run_id)
        ledger = s.execute(select(m.CashLedger).where(m.CashLedger.run_id == out.run_id)).scalars().all()
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "options_run.new")).scalars().all()
    assert (run.mode, run.status, run.label, run.started_at) == ("options", "active", "options", NIGHT)
    assert run.params == {"options_settings": SETTINGS.model_dump(mode="json", by_alias=True)}
    assert run.params["options_settings"]["options.starting_cash"] == "5000"
    (deposit,) = ledger
    assert (deposit.kind, deposit.amount, deposit.currency) == ("deposit", D("5000.0000"), "USD")
    assert deposit.ref == f"sim_account:{out.account_id}"
    assert deposit.trade_date == deposit.settle_date == date(2026, 9, 28)  # settled the same day (ET)
    (row,) = audit
    assert (row.actor, row.before) == ("cli:test", None)
    assert row.after == {
        "run_id": out.run_id,
        "retired_run_id": None,
        "currency": "USD",
        "starting_cash": "5000.0000",
    }

    # A second start retires the first (its rows kept) in the same way.
    again = start_options_run(db_factory, FixedClock(NIGHT), CAL, SETTINGS, cash=D("2500.5"), actor="cli:x")
    assert again.retired_run_id == out.run_id and active_options_run_id(db_factory) == again.run_id
    with db_factory() as s:
        old = s.get_one(m.Run, out.run_id)
        kept = s.execute(
            select(func.count()).select_from(m.CashLedger).where(m.CashLedger.run_id == out.run_id)
        ).scalar_one()
        last = s.execute(select(m.AuditLog).order_by(m.AuditLog.id.desc()).limit(1)).scalar_one()
    assert (old.status, old.finished_at, kept) == ("completed", NIGHT, 1)
    assert last.before == {"run_id": out.run_id, "status": "active"}
    assert last.after["starting_cash"] == "2500.5000"
    new_acct = options_account(db_factory, again.run_id)
    assert new_acct is not None and new_acct.starting_cash == D("2500.5000")


def test_start_options_run_takes_decimal_cash_only(db_factory: sessionmaker[Session]) -> None:
    with pytest.raises(TypeError, match="Decimal"):
        start_options_run(db_factory, FixedClock(NIGHT), CAL, SETTINGS, cash=5000.0, actor="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        start_options_run(db_factory, FixedClock(NIGHT), CAL, SETTINGS, cash=D("0"), actor="x")
    assert active_options_run_id(db_factory) is None


def _open_structure(factory: sessionmaker[Session], run_id: int) -> None:
    with factory() as s:
        sym = f.add_underlying(s)
        f.add_structure(s, run_id, sym)
        s.commit()


def _closed_structure_and_done_order(factory: sessionmaker[Session], run_id: int) -> None:
    with factory() as s:
        sym = f.add_underlying(s)
        f.add_structure(s, run_id, sym, state="closed", close_reason="expired", closed_at=NIGHT)
        f.add_order(s, run_id, sym, status="filled")
        f.add_order(s, run_id, sym, status="rejected", net_limit=None)
        s.commit()


def _working_order(factory: sessionmaker[Session], run_id: int) -> None:
    with factory() as s:
        sym = f.add_underlying(s)
        f.add_order(s, run_id, sym, status="working")
        s.commit()


@pytest.mark.parametrize(
    ("setup", "now", "words"),
    [
        (_open_structure, NIGHT, "1 open structure"),
        (_working_order, NIGHT, "1 working order"),
        (None, et(TUE, 9, 15), "09:15"),
        (None, et(TUE, 12, 0), "09:15"),
        (None, et(TUE, 16, 29), "09:15"),
        (_closed_structure_and_done_order, NIGHT, None),  # nothing live: allowed
        (None, et(TUE, 9, 14), None),
        (None, et(TUE, 16, 30), None),
        (None, et(SAT, 12, 0), None),  # not a session day
    ],
)
def test_start_options_run_refusals(
    db_factory: sessionmaker[Session],
    setup: Callable[[sessionmaker[Session], int], None] | None,
    now: datetime,
    words: str | None,
) -> None:
    old = _start(db_factory)
    if setup is not None:
        setup(db_factory, old)
    if words is None:
        assert _start(db_factory, now) != old
        return
    with pytest.raises(OptionsRunRefused, match=words):
        _start(db_factory, now)
    with db_factory() as s:  # nothing changed
        assert s.execute(select(m.Run.status).where(m.Run.id == old)).scalar_one() == "active"
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1
        assert s.execute(select(func.count()).select_from(m.AuditLog)).scalar_one() == 1
    assert active_options_run_id(db_factory) == old


def test_live_run_lookup_ignores_the_options_run(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NIGHT)
    assert runtime.active_live_run_id(db_factory) is None
    options_id = _start(db_factory)
    assert runtime.active_live_run_id(db_factory) is None  # an options run is not a live run
    live = get_live_run(db_factory, clock, RuntimeSettings())  # created beside it, not instead of it
    assert live.id != options_id and live.mode == "live"
    assert runtime.active_live_run_id(db_factory) == live.id
    assert get_live_run(db_factory, clock, RuntimeSettings()).id == live.id
    assert active_options_run_id(db_factory) == options_id
    # A new options run leaves the live run and its account alone.
    newer = _start(db_factory)
    assert runtime.active_live_run_id(db_factory) == live.id and newer not in (live.id, options_id)
    with db_factory() as s:
        accounts = dict(s.execute(select(m.SimAccount.run_id, m.SimAccount.starting_cash)).all())
    assert accounts == {options_id: D("5000.0000"), live.id: D("720.0000"), newer: D("5000.0000")}


def test_the_book_lock_is_per_run_and_held_to_the_end_of_the_transaction(
    db_factory: sessionmaker[Session],
) -> None:
    try_run_1 = text("SELECT pg_try_advisory_xact_lock(hashtext('options_book:' || '1'))")
    with db_factory() as a, db_factory() as b:
        lock_book(a, 1)
        lock_book(b, 2)  # another run's book does not wait
        assert b.execute(try_run_1).scalar_one() is False  # run 1's book is locked by `a`
        a.commit()
        assert b.execute(try_run_1).scalar_one() is True
