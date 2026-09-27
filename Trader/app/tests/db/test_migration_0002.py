from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0, add_run, add_strategy_config, add_symbol
from trader.db import models as m

pytestmark = pytest.mark.db

TRADING_TABLES = {
    "runs",
    "sim_accounts",
    "strategy_configs",
    "catalysts",
    "candidates",
    "signals",
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "cash_ledger",
    "equity_snapshots",
    "journal",
    "kill_switch_events",
}
RUN_SCOPED = TRADING_TABLES - {"runs", "strategy_configs", "catalysts"}


def test_every_trading_table_exists(migrated_engine: Engine) -> None:
    assert TRADING_TABLES <= set(inspect(migrated_engine).get_table_names(schema="trader"))


def test_views_exist(migrated_engine: Engine) -> None:
    assert {"v_trade_metrics", "v_daily_pnl"} <= set(inspect(migrated_engine).get_view_names(schema="trader"))


@pytest.mark.parametrize("table", sorted(RUN_SCOPED))
def test_run_id_on_every_trading_row(migrated_engine: Engine, table: str) -> None:
    insp = inspect(migrated_engine)
    cols = {c["name"]: c for c in insp.get_columns(table, schema="trader")}
    assert "run_id" in cols and cols["run_id"]["nullable"] is False
    fks = insp.get_foreign_keys(table, schema="trader")
    assert any(fk["constrained_columns"] == ["run_id"] and fk["referred_table"] == "runs" for fk in fks)


def test_keys(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert insp.get_pk_constraint("equity_snapshots", schema="trader")["constrained_columns"] == [
        "run_id",
        "ts",
    ]
    assert insp.get_pk_constraint("journal", schema="trader")["constrained_columns"] == [
        "run_id",
        "session_date",
    ]
    uniques = {
        t: [u["column_names"] for u in insp.get_unique_constraints(t, schema="trader")]
        for t in ("sim_accounts", "strategy_configs", "catalysts", "candidates", "fills", "trades")
    }
    assert ["run_id"] in uniques["sim_accounts"]
    assert ["strategy_key", "revision"] in uniques["strategy_configs"]
    assert ["symbol_id", "session_date"] in uniques["catalysts"]
    assert ["run_id", "session_date", "strategy_key", "symbol_id"] in uniques["candidates"]
    assert ["order_id"] in uniques["fills"]
    assert ["position_id"] in uniques["trades"]


def test_only_one_active_live_run(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        add_run(s)
        add_run(s, mode="replay")
        add_run(s, mode="replay")
        add_run(s, status="completed")
        s.commit()
        with pytest.raises(IntegrityError):
            add_run(s)


def _trade(s: Session, run_id: int, sym: int, cfg: int, pnl: str, r: str, day: date) -> None:
    pos = m.Position(
        run_id=run_id,
        symbol_id=sym,
        strategy_config_id=cfg,
        qty=10,
        avg_price=Decimal("10"),
        stop_loss=Decimal("9"),
        planned_risk=Decimal("10"),
        session_date=day,
        opened_at=T0,
        closed_at=T0 + timedelta(hours=1),
        entry_order_id=1,
        stop_order_id=None,
        unprotected_since=None,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    s.add(
        m.Trade(
            run_id=run_id,
            position_id=pos.id,
            symbol_id=sym,
            session_date=day,
            entry_price=Decimal("10"),
            exit_price=Decimal("10") + Decimal(pnl) / 10,
            qty=10,
            pnl=Decimal(pnl),
            pnl_r=Decimal(r),
            planned_risk=Decimal("10"),
            exit_reason="test",
            slippage_total=Decimal("0.02"),
            fees_total=Decimal("0.01"),
            opened_at=T0,
            closed_at=T0 + timedelta(hours=1),
        )
    )


def test_v_trade_metrics_hand_made_trades(db_factory: sessionmaker[Session]) -> None:
    d1, d2 = date(2026, 10, 5), date(2026, 10, 6)
    with db_factory() as s:
        run = add_run(s)
        other = add_run(s, mode="replay")
        sym = add_symbol(s)
        cfg = add_strategy_config(s)
        for pnl, r, day in (("20", "2", d1), ("-10", "-1", d1), ("-10", "-1", d2), ("15", "1.5", d2)):
            _trade(s, run, sym, cfg, pnl, r, day)
        for i, dd in enumerate(("0.0200", "0.0500", "0.0100")):
            s.add(
                m.EquitySnapshot(
                    run_id=run,
                    ts=T0 + timedelta(minutes=i),
                    equity=Decimal("700"),
                    cash=Decimal("700"),
                    settled_cash=Decimal("700"),
                    peak_equity=Decimal("720"),
                    drawdown_pct=Decimal(dd),
                )
            )
        for day, followed in ((d1, True), (d2, False), (date(2026, 10, 7), None)):
            s.add(
                m.Journal(
                    run_id=run, session_date=day, rules_followed=followed, notes=None, answered_via=None
                )
            )
        s.commit()
        row = (
            s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": run})
            .mappings()
            .one()
        )
        empty = (
            s.execute(text("SELECT * FROM trader.v_trade_metrics WHERE run_id = :r"), {"r": other})
            .mappings()
            .one()
        )
        daily = (
            s.execute(
                text(
                    "SELECT session_date, trades, realized_pnl FROM trader.v_daily_pnl ORDER BY session_date"
                )
            )
            .mappings()
            .all()
        )
    assert row["trades"] == 4 and row["wins"] == 2
    assert row["win_rate"] == Decimal("0.5000")
    assert row["avg_win_r"] == Decimal("1.7500")
    assert row["avg_loss_r"] == Decimal("-1.0000")
    assert row["expectancy_r"] == Decimal("0.3750")
    assert row["profit_factor"] == Decimal("1.7500")
    assert row["avg_slippage"] == Decimal("0.0200")
    assert row["max_drawdown_pct"] == Decimal("0.0500")
    assert row["adherence_pct"] == Decimal("0.5000")
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["expectancy_r"] is None
    assert [(r["session_date"], r["trades"], r["realized_pnl"]) for r in daily] == [
        (d1, 2, Decimal("10.0000")),
        (d2, 2, Decimal("5.0000")),
    ]


def test_cash_ledger_trigger_exists(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        names = conn.execute(
            text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = 'trader.cash_ledger'::regclass AND NOT tgisinternal"
            )
        ).scalars()
        assert set(names) == {"cash_ledger_append_only"}


def test_timestamps_are_timestamptz(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        bad = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'trader' AND data_type = 'timestamp without time zone'"
            )
        ).all()
    assert bad == []


def test_created_at_round_trips_in_utc(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run_id = add_run(s, started_at=datetime.fromisoformat("2026-10-06T09:30:00-04:00"))
        s.commit()
        started = s.get(m.Run, run_id)
        assert started is not None
    assert started.started_at.utcoffset() == timedelta(0)
    assert started.started_at.hour == 13
