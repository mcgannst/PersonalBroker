"""SIZECAP: `trader live-run new --confirm` retires the active live run (status `completed`, every row kept)
and starts a fresh active live run and sim account from the current settings, audited. It refuses while a
position is open, an order is working or a proposal is pending, and from 09:15 to 16:30 ET on a session day.
`--set KEY=VALUE` settings and `--strategy-param KEY.PARAM=VALUE` strategy parameters are validated all
together first and written (audited) only when the new run can start."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_api import test_core
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.engine.runs import LiveRunRefused, get_live_run, sim_account, start_new_live_run
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.settings_store import RuntimeSettings, SettingsStore
from trader.strategies.registry import StrategyRegistry

pytestmark = pytest.mark.db
runner = CliRunner()
CAL = SessionCalendar()
TUE = date(2026, 9, 29)  # a session day
SAT = date(2026, 10, 3)


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


NIGHT = et(date(2026, 9, 28), 23, 45)  # Monday 23:45 ET: outside the block


def _old_run(factory: sessionmaker[Session]) -> int:
    return get_live_run(factory, FixedClock(et(date(2026, 9, 21), 8, 0)), RuntimeSettings()).id


def _add_order(factory: sessionmaker[Session], run_id: int, status: str) -> None:
    with factory() as s:
        sym = add_symbol(s, "ZZZ")
        s.add(
            m.Order(
                run_id=run_id,
                symbol_id=sym,
                side="buy",
                order_type="stop",
                purpose="entry",
                qty=3,
                stop_price=Decimal("20"),
                tif="day",
                status=status,
                reason="orb_breakout",
                session_date=date(2026, 9, 28),
                submitted_at=NIGHT,
                stale_alerted=False,
            )
        )
        s.commit()


def _add_position(factory: sessionmaker[Session], run_id: int, closed: bool) -> None:
    with factory() as s:
        sym = add_symbol(s, "YYY")
        s.add(
            m.Position(
                run_id=run_id,
                symbol_id=sym,
                qty=3,
                avg_price=Decimal("20"),
                session_date=date(2026, 9, 28),
                opened_at=NIGHT,
                closed_at=NIGHT if closed else None,
                entry_order_id=1,
                unprotected_seconds=0,
            )
        )
        s.commit()


def _add_pending(factory: sessionmaker[Session], run_id: int) -> None:
    with factory() as s:
        cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=run_id,
            strategy_config_id=cfg,
            symbol_id=None,
            session_date=date(2026, 9, 28),
            event_key="orb_open",
            ts=NIGHT,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.flush()
        s.add(
            m.Proposal(
                run_id=run_id,
                signal_id=sig.id,
                kind="entry",
                order_spec={},
                qty=3,
                status="pending",
                created_at=NIGHT,
                expires_at=NIGHT,
                escalations=0,
            )
        )
        s.commit()


CAD = RuntimeSettings(
    starting_cash=Decimal("1000"),
    starting_cash_currency="CAD",
    fx_cad_usd_rate=Decimal("0.72"),
    fx_fee_pct=Decimal("0.015"),
)


def test_retires_the_active_run_and_starts_a_fresh_one(db_factory: sessionmaker[Session]) -> None:
    old = _old_run(db_factory)
    _add_position(db_factory, old, closed=True)  # a closed position and a filled order never block
    _add_order(db_factory, old, "filled")
    out = start_new_live_run(db_factory, FixedClock(NIGHT), CAL, CAD, actor="cli:test")
    assert out.retired_id == old and out.run.id != old and out.run.mode == "live"
    acct = sim_account(db_factory, out.run.id)
    # 1000 CAD x 0.72 x (1 - 0.015) = 709.2 USD
    assert acct is not None and acct.starting_cash == Decimal("709.2000") and acct.currency == "USD"
    assert (acct.source_amount, acct.source_currency) == (Decimal("1000.0000"), "CAD")
    with db_factory() as s:
        runs = {r.id: r for r in s.execute(select(m.Run)).scalars()}
        deposits = (
            s.execute(select(m.CashLedger.amount).where(m.CashLedger.run_id == out.run.id)).scalars().all()
        )
        old_rows = s.execute(
            select(func.count()).select_from(m.Order).where(m.Order.run_id == old)
        ).scalar_one()
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "live_run.new")).scalar_one()
    assert runs[old].status == "completed" and runs[old].finished_at == NIGHT
    assert runs[out.run.id].status == "active" and runs[out.run.id].started_at == NIGHT
    assert runs[out.run.id].params["settings"]["starting_cash"] == "1000"
    assert deposits == [Decimal("709.2000")] and old_rows == 1  # the old run keeps its rows
    assert audit.actor == "cli:test" and audit.before == {"run_id": old, "status": "active"}
    assert audit.after["run_id"] == out.run.id and audit.after["starting_cash"] == "709.2000"
    # the next process reads the new run; nothing creates a third
    assert get_live_run(db_factory, FixedClock(NIGHT), CAD).id == out.run.id


def test_with_no_active_run_it_just_starts_one(db_factory: sessionmaker[Session]) -> None:
    out = start_new_live_run(db_factory, FixedClock(NIGHT), CAL, RuntimeSettings(), actor="cli:test")
    assert out.retired_id is None and out.account.starting_cash == Decimal("720.0000")


@pytest.mark.parametrize(
    ("setup", "words"),
    [
        (lambda f, r: _add_position(f, r, closed=False), "1 open position"),
        (lambda f, r: _add_order(f, r, "working"), "1 working order"),
        (lambda f, r: _add_pending(f, r), "1 pending proposal"),
    ],
)
def test_refuses_while_anything_is_live(db_factory: sessionmaker[Session], setup: object, words: str) -> None:
    old = _old_run(db_factory)
    setup(db_factory, old)  # type: ignore[operator]
    with pytest.raises(LiveRunRefused, match=words):
        start_new_live_run(db_factory, FixedClock(NIGHT), CAL, CAD, actor="cli:test")
    with db_factory() as s:
        assert s.execute(select(m.Run.status).where(m.Run.id == old)).scalar_one() == "active"
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 1


@pytest.mark.parametrize(
    ("now", "refused"),
    [
        (et(TUE, 9, 14), False),
        (et(TUE, 9, 15), True),
        (et(TUE, 12, 0), True),
        (et(TUE, 16, 29), True),
        (et(TUE, 16, 30), False),
        (et(SAT, 12, 0), False),  # not a session day
    ],
)
def test_refuses_during_the_trading_day(
    db_factory: sessionmaker[Session], now: datetime, refused: bool
) -> None:
    old = _old_run(db_factory)
    if refused:
        with pytest.raises(LiveRunRefused, match="09:15"):
            start_new_live_run(db_factory, FixedClock(now), CAL, CAD, actor="cli:test")
    else:
        assert start_new_live_run(db_factory, FixedClock(now), CAL, CAD, actor="cli:test").retired_id == old


# --- the CLI ------------------------------------------------------------------------------------------------
@pytest.fixture
def core(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Core]:
    c = test_core(db_factory, FixedClock(NIGHT))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


def _runs(factory: sessionmaker[Session]) -> list[tuple[int, str]]:
    with factory() as s:
        return [(r.id, r.status) for r in s.execute(select(m.Run).order_by(m.Run.id)).scalars()]


def test_cli_needs_confirm(core: Core) -> None:
    old = _old_run(core.factory)
    result = runner.invoke(app, ["live-run", "new"])
    assert result.exit_code == 1 and "--confirm" in result.output
    assert _runs(core.factory) == [(old, "active")]


def test_cli_applies_settings_and_strategy_params_then_starts_the_run(core: Core) -> None:
    old = _old_run(core.factory)
    StrategyRegistry(core.factory, core.clock).ensure_defaults()
    result = runner.invoke(
        app,
        [
            "live-run",
            "new",
            "--confirm",
            "--set",
            "starting_cash=1000",
            "--set",
            "starting_cash_currency=CAD",
            "--set",
            "fx.cad_usd_rate=0.72",
            "--set",
            "fx.fee_pct=0.015",
            "--strategy-param",
            "orb_sip.max_positions=10",
        ],
    )
    assert result.exit_code == 0, result.output
    (retired, new) = _runs(core.factory)
    assert retired == (old, "completed") and new[1] == "active"
    assert f"retired live run {old}" in result.output and f"live run {new[0]}" in result.output
    assert "709.2000 USD" in result.output and "1000 CAD" in result.output
    s = SettingsStore(core.factory, core.clock.now).load()
    assert (s.starting_cash, s.starting_cash_currency) == (Decimal("1000"), "CAD")
    assert StrategyRegistry(core.factory, core.clock).current("orb_sip").params["max_positions"] == 10
    with core.factory() as db:
        actions = [a.action for a in db.execute(select(m.AuditLog).order_by(m.AuditLog.id)).scalars()]
        actor = db.execute(select(m.AuditLog.actor).where(m.AuditLog.action == "live_run.new")).scalar_one()
    assert "settings.set:starting_cash" in actions and "strategy.update:orb_sip" in actions
    assert actions[-1] == "live_run.new" and actor == "cli:live-run"


@pytest.mark.parametrize(
    "bad",
    [
        ["--set", "starting_cash=-5"],
        ["--set", "no_such_key=1"],
        ["--set", "noequals"],
        ["--strategy-param", "orb_sip.max_positions=11"],
        ["--strategy-param", "nope.max_positions=2"],
        ["--strategy-param", "orb_sip=2"],
    ],
)
def test_cli_invalid_values_change_nothing(core: Core, bad: list[str]) -> None:
    old = _old_run(core.factory)
    StrategyRegistry(core.factory, core.clock).ensure_defaults()
    args = ["live-run", "new", "--confirm", "--set", "starting_cash=1000", *bad]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert _runs(core.factory) == [(old, "active")]
    assert SettingsStore(core.factory, core.clock.now).load().starting_cash == Decimal("720")
    assert StrategyRegistry(core.factory, core.clock).current("orb_sip").params["max_positions"] == 1


def test_cli_refusal_changes_nothing(core: Core) -> None:
    old = _old_run(core.factory)
    StrategyRegistry(core.factory, core.clock).ensure_defaults()
    _add_order(core.factory, old, "working")
    result = runner.invoke(
        app,
        [
            "live-run",
            "new",
            "--confirm",
            "--set",
            "starting_cash=1000",
            "--strategy-param",
            "orb_sip.max_positions=10",
        ],
    )
    assert result.exit_code == 1 and "1 working order" in result.output
    assert _runs(core.factory) == [(old, "active")]
    assert SettingsStore(core.factory, core.clock.now).load().starting_cash == Decimal("720")
    assert StrategyRegistry(core.factory, core.clock).current("orb_sip").params["max_positions"] == 1


def test_cli_refuses_during_the_trading_day(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = test_core(db_factory, FixedClock(et(TUE, 10, 0)))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    old = _old_run(db_factory)
    result = runner.invoke(app, ["live-run", "new", "--confirm"])
    assert result.exit_code == 1 and "09:15" in result.output
    assert _runs(db_factory) == [(old, "active")]
