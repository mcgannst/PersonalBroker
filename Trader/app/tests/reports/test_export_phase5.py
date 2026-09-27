"""P5-T12 acceptance test 3: the trades CSV gains seven traceability columns (BR-62, SPEC §11)."""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.reports import add_trade
from trader.db import models as m
from trader.reports.export import TRADE_CSV_COLUMNS, trades_csv

D1, D2 = date(2026, 11, 23), date(2026, 11, 24)
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

P4_COLUMNS = (
    "trade_id",
    "session_date",
    "ticker",
    "strategy",
    "qty",
    "entry_price",
    "exit_price",
    "pnl",
    "pnl_r",
    "planned_risk",
    "fees_total",
    "slippage_total",
    "exit_reason",
    "opened_at",
    "closed_at",
)
P5_COLUMNS = (
    "currency",
    "strategy_version",
    "config_revision",
    "config_scope",
    "stop_loss",
    "unprotected_seconds",
    "run_mode",
)


def _rows(factory: sessionmaker[Session], run_id: int) -> list[dict[str, str]]:
    lines = list(trades_csv(factory, run_id, None, None))
    table = list(csv.reader(io.StringIO("".join(lines))))
    assert tuple(table[0]) == TRADE_CSV_COLUMNS
    return [dict(zip(TRADE_CSV_COLUMNS, r, strict=True)) for r in table[1:]]


def _account(s: Session, run_id: int, currency: str) -> None:
    s.add(
        m.SimAccount(
            run_id=run_id,
            currency=currency,
            starting_cash=Decimal("10000.0000"),
            source_amount=Decimal("10000.0000"),
            source_currency=currency,
            fx_rate=None,
            fx_fee=None,
            created_at=T0,
        )
    )


def _protect(s: Session, trade_id: int, stop_loss: str | None, unprotected_seconds: int) -> None:
    trade = s.get_one(m.Trade, trade_id)
    pos = s.get_one(m.Position, trade.position_id)
    pos.stop_loss = Decimal(stop_loss) if stop_loss is not None else None
    pos.unprotected_seconds = unprotected_seconds
    s.flush()


def test_header_is_the_p4_fifteen_then_the_seven_new_columns() -> None:
    assert TRADE_CSV_COLUMNS == P4_COLUMNS + P5_COLUMNS
    assert len(TRADE_CSV_COLUMNS) == 22


@pytest.mark.db
def test_live_trade_fills_the_new_columns(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)
        _account(s, run, "USD")
        sym = add_symbol(s, "AAA")
        cfg = add_strategy_config(s, "orb_sip", version="1.2.0", revision=3)
        t1 = add_trade(s, run, sym, D1, "20.0000", "2.0000", config_id=cfg)
        _protect(s, t1, "19.5000", 4)
        s.commit()
    (row,) = _rows(db_factory, run)
    assert row["trade_id"] == str(t1) and row["pnl"] == "20.0000"  # the P4 columns are unchanged
    assert {k: row[k] for k in P5_COLUMNS} == {
        "currency": "USD",
        "strategy_version": "1.2.0",
        "config_revision": "3",
        "config_scope": "live",
        "stop_loss": "19.5000",
        "unprotected_seconds": "4",
        "run_mode": "live",
    }


@pytest.mark.db
def test_replay_trade_with_replay_scoped_config(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        live = add_run(s)
        replay = add_run(s, mode="replay", status="completed")
        _account(s, live, "USD")
        _account(s, replay, "CAD")
        sym = add_symbol(s, "BBB")
        live_cfg = add_strategy_config(s, "orb_sip", version="1.0.0", revision=2)
        replay_cfg = m.StrategyConfig(
            strategy_key="orb_sip",
            version="1.0.0",
            revision=2,  # a replay override keeps the base live revision
            params={"risk_pct": "0.005"},
            enabled=True,
            created_at=T0,
            created_by="replay",
            scope="replay",
        )
        s.add(replay_cfg)
        s.flush()
        add_trade(s, live, sym, D1, "5.0000", "0.5000", config_id=live_cfg)
        t = add_trade(s, replay, sym, D2, "-10.0000", "-1.0000", config_id=replay_cfg.id)
        _protect(s, t, "19.0000", 0)
        s.commit()
    (row,) = _rows(db_factory, replay)
    assert row["trade_id"] == str(t)
    assert row["config_scope"] == "replay" and row["run_mode"] == "replay"
    assert row["currency"] == "CAD" and row["config_revision"] == "2" and row["strategy_version"] == "1.0.0"
    assert row["stop_loss"] == "19.0000" and row["unprotected_seconds"] == "0"
    (live_row,) = _rows(db_factory, live)
    assert (
        live_row["config_scope"] == "live"
        and live_row["run_mode"] == "live"
        and live_row["currency"] == "USD"
    )


@pytest.mark.db
def test_missing_config_account_and_stop_are_empty_cells(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s)  # no sim account
        sym = add_symbol(s, "CCC")
        add_trade(s, run, sym, D1, "1.0000", None, config_id=None)
        s.commit()
    (row,) = _rows(db_factory, run)
    assert row["currency"] == row["strategy_version"] == row["config_revision"] == row["config_scope"] == ""
    assert row["stop_loss"] == "" and row["unprotected_seconds"] == "0" and row["run_mode"] == "live"


@pytest.mark.db
def test_new_text_cells_get_the_formula_guard(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        run = add_run(s, mode="=x()")
        _account(s, run, "@AB")
        sym = add_symbol(s, "DDD")
        cfg = add_strategy_config(s, "orb_sip", version="=HYPERLINK(1)", revision=1)
        t = add_trade(s, run, sym, D1, "-3.0000", "-0.3000", config_id=cfg)
        _protect(s, t, "18.0000", 12)
        s.commit()
    (row,) = _rows(db_factory, run)
    assert row["currency"] == "'@AB"
    assert row["strategy_version"] == "'=HYPERLINK(1)"
    assert row["run_mode"] == "'=x()"
    assert row["config_scope"] == "live"
    # numbers are never prefixed, even negative ones
    assert row["pnl"] == "-3.0000" and row["stop_loss"] == "18.0000" and row["unprotected_seconds"] == "12"
