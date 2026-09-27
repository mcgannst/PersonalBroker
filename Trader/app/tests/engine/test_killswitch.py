from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from trader.broker.types import AccountState
from trader.db import models as m
from trader.engine.killswitch import KillSwitches, KillSwitchInputs
from trader.engine.runs import get_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY, NEXT = date(2026, 10, 6), date(2026, 10, 7)
T = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
S = RuntimeSettings()


def inputs(
    start: str = "720", equity: str = "720", peak: str = "720", trades: int = 0, exp: str | None = None
) -> KillSwitchInputs:
    return KillSwitchInputs(
        Decimal(start), Decimal(equity), Decimal(peak), trades, Decimal(exp) if exp else None
    )


@pytest.fixture
def ks(db_factory: sessionmaker[Session]) -> tuple[KillSwitches, int]:
    clock = FixedClock(T)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    return KillSwitches(db_factory, clock), run.id


def test_inputs_properties() -> None:
    i = inputs(start="720", equity="680", peak="800")
    assert i.daily_pnl_pct == Decimal("-0.055556") and i.drawdown_pct == Decimal("0.150000")


def test_daily_loss_trips_and_resets_next_session(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(equity="684.01"), S) == []  # -4.999%
    assert switches.evaluate(run, DAY, inputs(equity="684"), S) == ["daily_loss_pct"]  # exactly -5%
    assert switches.blocking(run, DAY) == "daily_loss_pct"
    assert switches.blocking(run, NEXT) is None  # resets automatically next session
    with db_factory() as s:
        ev = s.execute(select(m.KillSwitchEvent)).scalar_one()
        alert = s.execute(select(m.EventLog).where(m.EventLog.source == "killswitch")).scalar_one()
    assert ev.switch == "daily_loss_pct" and ev.session_date == DAY and ev.value == Decimal("0.050000")
    assert ev.threshold == Decimal("0.050000") and alert.level == "error"


def test_an_active_switch_is_not_tripped_twice(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(equity="650", peak="650"), S) == ["daily_loss_pct"]
    assert switches.evaluate(run, DAY, inputs(equity="640", peak="640"), S) == []
    with db_factory() as s:
        assert len(s.execute(select(m.KillSwitchEvent)).scalars().all()) == 1


def test_drawdown_needs_a_manual_reset_with_a_reason(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.evaluate(run, DAY, inputs(start="680", equity="680", peak="800"), S) == [
        "max_drawdown_pct"
    ]
    assert switches.blocking(run, NEXT) == "max_drawdown_pct"  # survives the session change
    with pytest.raises(ValueError, match="reason"):
        switches.reset(run, "max_drawdown_pct", "   ", actor="stephen")
    switches.reset(run, "max_drawdown_pct", "reviewed the losing streak", actor="stephen")
    assert switches.blocking(run, NEXT) is None
    with pytest.raises(ValueError, match="not tripped"):
        switches.reset(run, "max_drawdown_pct", "again", actor="stephen")
    with db_factory() as s:
        audit = s.execute(select(m.AuditLog)).scalar_one()
        ev = s.execute(select(m.KillSwitchEvent)).scalar_one()
    assert audit.action == "killswitch.reset:max_drawdown_pct" and audit.actor == "stephen"
    assert ev.reset_reason == "reviewed the losing streak" and ev.reset_by == "stephen" and ev.reset_at == T


@pytest.mark.parametrize(
    ("trades", "exp", "trips"), [(50, "0", True), (50, "-0.2", True), (49, "-1", False), (60, "0.05", False)]
)
def test_expectancy_switch_after_n_trades(
    ks: tuple[KillSwitches, int], trades: int, exp: str, trips: bool
) -> None:
    switches, run = ks
    assert (switches.evaluate(run, DAY, inputs(trades=trades, exp=exp), S) == ["expectancy"]) is trips


def test_manual_pause_blocks_entries_and_resume_lifts_only_the_pause(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    assert switches.pause(run, DAY, actor="telegram") is True
    assert switches.pause(run, DAY, actor="telegram") is False  # already paused
    assert switches.blocking(run, NEXT) == "manual_pause"
    switches.evaluate(run, DAY, inputs(start="680", equity="680", peak="800"), S)
    assert switches.resume(run, actor="telegram") is True
    assert switches.blocking(run, DAY) == "max_drawdown_pct"  # /resume never lifts an automatic switch
    assert switches.resume(run, actor="telegram") is False
    with pytest.raises(ValueError, match="resume"):
        switches.reset(run, "manual_pause", "reason", actor="stephen")
    with db_factory() as s:
        actions = [a for a in s.execute(select(m.AuditLog.action).order_by(m.AuditLog.id)).scalars()]
    assert actions == ["killswitch.pause", "killswitch.resume"]


def test_inputs_come_from_snapshots_account_and_trades(
    db_factory: sessionmaker[Session], ks: tuple[KillSwitches, int]
) -> None:
    switches, run = ks
    session_open = CAL.session_open(DAY)
    acct = AccountState(Decimal("690"), Decimal("690"), Decimal("690"), Decimal("0"), Decimal("690"))
    first = switches.inputs(run, DAY, acct, session_open)
    assert (first.start_equity, first.peak_equity, first.closed_trades, first.expectancy_r) == (
        Decimal("720.0000"),
        Decimal("720.0000"),
        0,
        None,
    )
    with db_factory() as s:
        for ts, eq, peak in (
            (session_open - timedelta(hours=20), "700", "750"),
            (session_open - timedelta(hours=1), "710", "750"),
        ):
            s.add(
                m.EquitySnapshot(
                    run_id=run,
                    ts=ts,
                    equity=Decimal(eq),
                    cash=Decimal(eq),
                    settled_cash=Decimal(eq),
                    peak_equity=Decimal(peak),
                    drawdown_pct=Decimal("0"),
                )
            )
        sym, cfg = add_symbol(s), add_strategy_config(s)
        pos = m.Position(
            run_id=run,
            symbol_id=sym,
            strategy_config_id=cfg,
            qty=1,
            avg_price=Decimal("1"),
            stop_loss=None,
            planned_risk=None,
            session_date=DAY,
            opened_at=T,
            closed_at=T,
            entry_order_id=1,
            stop_order_id=None,
            unprotected_since=None,
            unprotected_seconds=0,
        )
        s.add(pos)
        s.flush()
        s.add(
            m.Trade(
                run_id=run,
                position_id=pos.id,
                symbol_id=sym,
                session_date=DAY,
                entry_price=Decimal("1"),
                exit_price=Decimal("1"),
                qty=1,
                pnl=Decimal("-1"),
                pnl_r=Decimal("-0.5"),
                planned_risk=None,
                exit_reason="t",
                slippage_total=Decimal("0"),
                fees_total=Decimal("0"),
                opened_at=T,
                closed_at=T,
            )
        )
        s.commit()
    later = switches.inputs(run, DAY, acct, session_open)
    assert later.start_equity == Decimal("710.0000") and later.peak_equity == Decimal("750.0000")
    assert later.closed_trades == 1 and later.expectancy_r == Decimal("-0.5000")
