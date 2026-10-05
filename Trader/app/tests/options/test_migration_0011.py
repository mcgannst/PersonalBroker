"""Migration 0011 (OPTSIM): the option tables of task plan §3.7 and the one-active-options-run index.
Additive. The generic ORM-vs-schema comparison (alembic autogenerate sees no difference) is in
tests/db/test_migration.py; here every table and column is pinned by name, type and nullability, and every
unique key, check and partial index is shown to refuse a violating row."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from tests.factories import add_run
from tests.options import factories as f
from trader.db import models as m

pytestmark = pytest.mark.db
D = Decimal

# column type shorthands, as SQLAlchemy reflects them
ID, INT, SMALL, BOOL, DATE, TS, TEXT, JSON = (
    "BIGINT",
    "INTEGER",
    "SMALLINT",
    "BOOLEAN",
    "DATE",
    "TIMESTAMP",
    "TEXT",
    "JSONB",
)
MONEY, RATIO = "NUMERIC(14, 4)", "NUMERIC(12, 6)"


def V(n: int) -> str:
    return f"VARCHAR({n})"


def cols(spec: str) -> dict[str, tuple[str, bool]]:
    """`"name TYPE, name TYPE?"`: a trailing `?` marks a nullable column."""
    out: dict[str, tuple[str, bool]] = {}
    for part in spec.split(";"):
        name, _, ty = part.strip().partition(" ")
        out[name] = (ty.rstrip("?").strip(), ty.endswith("?"))
    return out


EXPECTED: dict[str, dict[str, tuple[str, bool]]] = {
    "option_contracts": cols(
        f"id {ID}; underlying_symbol_id {ID}; underlying {V(20)}; qt_symbol_id {ID}; root {V(20)}; "
        f"expiry {DATE}; strike {MONEY}; right {V(4)}; multiplier {INT}; is_monthly {BOOL}; "
        f"adjusted {BOOL}; first_seen_at {TS}"
    ),
    "option_chain_cache": cols(f"underlying_symbol_id {ID}; fetched_at {TS}; chain {JSON}; expiries {INT}"),
    "option_quote_marks": cols(
        f"contract_id {ID}; fetched_at {TS}; bid {MONEY}?; ask {MONEY}?; last {MONEY}?; bid_size {INT}?; "
        f"ask_size {INT}?; volume {ID}?; open_interest {ID}?; iv {RATIO}?; delta {RATIO}?; "
        f"gamma {RATIO}?; theta {RATIO}?; vega {RATIO}?; last_trade_time {TS}?; delay {INT}?; "
        f"is_halted {BOOL}; underlying_price {MONEY}?"
    ),
    "underlying_facts": cols(
        f"symbol_id {ID}; as_of {DATE}; ticker {V(20)}; security_type {V(30)}?; sector {V(60)}?; "
        f"price {MONEY}?; eps_ttm {MONEY}?; eps_growth_yoy {RATIO}?; debt_to_equity NUMERIC(12, 4)?; "
        f"book_value_per_share {MONEY}?; market_cap_usd NUMERIC(20, 2)?; sma50 {MONEY}?; "
        f"sma50_prior {MONEY}?; low_52w {MONEY}?; sessions_since_52w_low {INT}?; rsi14 NUMERIC(8, 4)?; "
        f"next_earnings_date {DATE}?; next_ex_dividend_date {DATE}?; dividend_per_share {MONEY}?; "
        f"dividend_yield {RATIO}?; payout_ratio {RATIO}?; short_float {RATIO}?; has_options {BOOL}?; "
        f"sources {JSON}; fetched_at {TS}"
    ),
    "option_strategy_configs": cols(
        f"id {ID}; strategy_key {V(30)}; version {V(20)}; revision {INT}; params {JSON}; enabled {BOOL}; "
        f"created_at {TS}; created_by {V(50)}"
    ),
    "option_strategy_state": cols(
        f"strategy_key {V(30)}; scope_key {V(100)}; value {JSON}; version {INT}; updated_at {TS}"
    ),
    "opt_structures": cols(
        f"id {ID}; run_id {ID}; source {V(30)}; strategy_config_id {ID}?; kind {V(20)}; "
        f"underlying_symbol_id {ID}; underlying {V(20)}; state {V(8)}; close_reason {V(12)}?; "
        f"frozen {BOOL}; qty {INT}; entry_net {MONEY}; reserved_cash {MONEY}; take_profit_net {MONEY}?; "
        f"cover_structure_id {ID}?; parent_structure_id {ID}?; realized_pnl {MONEY}; fees_total {MONEY}; "
        f"opened_at {TS}; closed_at {TS}?; meta {JSON}"
    ),
    "opt_positions": cols(
        f"id {ID}; run_id {ID}; structure_id {ID}; instrument {V(6)}; contract_id {ID}?; "
        f"underlying_symbol_id {ID}; qty {INT}; avg_price {MONEY}; realized_pnl {MONEY}; opened_at {TS}; "
        f"closed_at {TS}?"
    ),
    "opt_orders": cols(
        f"id {ID}; run_id {ID}; source {V(30)}; strategy_config_id {ID}?; intent {V(5)}; "
        f"structure_id {ID}?; underlying_symbol_id {ID}; order_type {V(6)}; net_limit {MONEY}?; "
        f"tif {V(3)}; qty {INT}; status {V(10)}; walk {BOOL}; walk_next_at {TS}?; "
        f"take_profit_pct NUMERIC(6, 4)?; reject_reason {V(30)}?; reject_detail {TEXT}?; "
        f"reason {V(100)}; evidence {JSON}; reserved_cash {MONEY}; session_date {DATE}; "
        f"submitted_at {TS}; submitted_by {V(50)}; updated_at {TS}; closed_at {TS}?; fill_net {MONEY}?; "
        f"fees {MONEY}?"
    ),
    "opt_order_legs": cols(
        f"id {ID}; order_id {ID}; leg_no {SMALL}; instrument {V(6)}; contract_id {ID}?; "
        f"underlying_symbol_id {ID}; side {V(4)}; effect {V(5)}; ratio {INT}"
    ),
    "opt_fills": cols(
        f"id {ID}; run_id {ID}; order_id {ID}; leg_id {ID}; structure_id {ID}; ts {TS}; side {V(4)}; "
        f"qty {INT}; price {MONEY}; fee {MONEY}; quote {JSON}; usd_cad_rate {RATIO}"
    ),
    "opt_lifecycle_events": cols(
        f"id {ID}; run_id {ID}; structure_id {ID}; position_id {ID}; contract_id {ID}?; kind {V(16)}; "
        f"session_date {DATE}; ts {TS}; underlying_close {MONEY}?; strike {MONEY}?; qty {INT}; "
        f"shares_delta {INT}; cash_delta {MONEY}; new_structure_id {ID}?; detail {JSON}; "
        f"delivered_at {TS}?"
    ),
    "owner_prompts": cols(
        f"id {ID}; run_id {ID}; source {V(30)}; kind {V(30)}; scope_key {V(60)}; dedupe_key {V(150)}; "
        f"title {V(200)}; body {TEXT}; choices {JSON}; needs_text {BOOL}; default_choice {V(1)}?; "
        f"data {JSON}; status {V(10)}; asked_at {TS}; last_sent_at {TS}?; send_count {INT}; "
        f"answered_at {TS}?; answer {V(1)}?; answer_text {TEXT}?; answered_via {V(10)}?; "
        f"answered_by {V(50)}?; delivered_at {TS}?"
    ),
    "wheel_tickers": cols(
        f"symbol_id {ID}; ticker {V(20)}; status {V(10)}; would_own {BOOL}?; ownership_reason {TEXT}?; "
        f"thesis_broken {BOOL}; security_type_override {V(30)}?; acknowledged_cautions {JSON}; "
        f"last_verdict {V(30)}?; last_screen {JSON}?; last_screened_at {TS}?; origin {V(10)}; "
        f"created_at {TS}; updated_at {TS}; updated_by {V(50)}"
    ),
    "wheel_positions": cols(
        f"id {ID}; run_id {ID}; symbol_id {ID}; ticker {V(20)}; state {V(12)}; put_structure_id {ID}?; "
        f"shares_structure_id {ID}?; call_structure_id {ID}?; contracts {INT}; roll_count {INT}; "
        f"total_put_premium {MONEY}; total_call_premium {MONEY}; dividends {MONEY}; "
        f"assignment_strike {MONEY}?; net_cost {MONEY}?; entry {JSON}; fresh_cash_answer {BOOL}?; "
        f"fresh_cash_at {TS}?; drawdown_review_at {TS}?; drawdown_review_text {TEXT}?; fees {MONEY}; "
        f"opened_at {TS}; closed_at {TS}?; close_reason {V(30)}?; full_cycle_result {MONEY}?"
    ),
    "wheel_events": cols(
        f"id {ID}; run_id {ID}; wheel_position_id {ID}?; symbol_id {ID}; session_date {DATE}; ts {TS}; "
        f"kind {V(10)}; action {V(40)}?; reason {TEXT}; data {JSON}"
    ),
}

PRIMARY_KEYS = {
    "option_chain_cache": ["underlying_symbol_id"],
    "option_quote_marks": ["contract_id"],
    "underlying_facts": ["symbol_id", "as_of"],
    "option_strategy_state": ["strategy_key", "scope_key"],
    "wheel_tickers": ["symbol_id"],
}
INDEXES = {  # name -> (table, columns, unique)
    "ix_option_contracts_underlying_expiry": ("option_contracts", ["underlying", "expiry"], False),
    "ix_opt_structures_run_state": ("opt_structures", ["run_id", "state"], False),
    "ix_opt_structures_run_underlying": ("opt_structures", ["run_id", "underlying_symbol_id"], False),
    "ix_opt_orders_run_status": ("opt_orders", ["run_id", "status"], False),
    "ix_opt_fills_run_ts": ("opt_fills", ["run_id", "ts"], False),
    "ix_opt_lifecycle_run_day": ("opt_lifecycle_events", ["run_id", "session_date"], False),
    "ix_owner_prompts_run_status": ("owner_prompts", ["run_id", "status"], False),
    "ix_wheel_events_run_day": ("wheel_events", ["run_id", "session_date"], False),
    "uq_wheel_positions_open_run_symbol": ("wheel_positions", ["run_id", "symbol_id"], True),
    "uq_wheel_events_evaluate_position_day": ("wheel_events", ["wheel_position_id", "session_date"], True),
    "uq_runs_one_active_options": ("runs", ["mode"], True),
}


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    out: dict[str, tuple[str, bool]] = {}
    for c in inspect(engine).get_columns(table, schema="trader"):
        ty = str(c["type"])
        out[c["name"]] = ("TIMESTAMP" if ty.startswith("TIMESTAMP") else ty, c["nullable"])
    return out


def _version(engine: Engine) -> str:
    with engine.connect() as conn:
        return str(conn.execute(text("SELECT version_num FROM trader.alembic_version")).scalar_one())


def _index_names(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'trader' AND tablename = :t"),
            {"t": table},
        )
        return {str(r[0]) for r in rows}


@pytest.fixture
def _restore_head(pg_url: str, migrated_engine: Engine) -> Iterator[None]:
    yield
    command.upgrade(alembic_config(pg_url), "head")


def test_migration_up_down_up(pg_url: str, migrated_engine: Engine, _restore_head: None) -> None:
    assert ScriptDirectory.from_config(alembic_config(pg_url)).get_current_head() == "0011"
    assert _version(migrated_engine) == "0011"
    command.downgrade(alembic_config(pg_url), "0010")
    assert _version(migrated_engine) == "0010"
    tables = set(inspect(migrated_engine).get_table_names(schema="trader"))
    assert not set(EXPECTED) & tables
    assert "uq_runs_one_active_options" not in _index_names(migrated_engine, "runs")
    assert "uq_runs_one_active_live" in _index_names(migrated_engine, "runs")  # the stock index stays
    command.upgrade(alembic_config(pg_url), "head")
    assert _version(migrated_engine) == "0011"
    assert set(EXPECTED) <= set(inspect(migrated_engine).get_table_names(schema="trader"))


def test_models_match_the_migrated_schema(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    assert len(EXPECTED) == 16
    for table, expected in EXPECTED.items():
        assert _columns(migrated_engine, table) == expected, table
        # the ORM model has the same columns and nullability
        model = m.Base.metadata.tables[f"trader.{table}"]
        assert {c.name: bool(c.nullable) for c in model.columns} == {
            name: nullable for name, (_, nullable) in expected.items()
        }, table
        assert insp.get_pk_constraint(table, schema="trader")["constrained_columns"] == PRIMARY_KEYS.get(
            table, ["id"]
        ), table
        fks = {
            (fk["constrained_columns"][0], fk["referred_table"])
            for fk in insp.get_foreign_keys(table, schema="trader")
        }
        if "run_id" in expected:  # every run-scoped table points at its run
            assert ("run_id", "runs") in fks, table
        for column in expected:
            if column.endswith("symbol_id") and column != "qt_symbol_id":
                assert (column, "symbols") in fks, f"{table}.{column}"
            if column.endswith("structure_id"):
                assert (column, "opt_structures") in fks, f"{table}.{column}"
            if column == "contract_id":
                assert (column, "option_contracts") in fks, f"{table}.{column}"
            if column == "strategy_config_id":
                assert (column, "option_strategy_configs") in fks, f"{table}.{column}"
    assert {
        ("order_id", "opt_orders"),
        ("leg_id", "opt_order_legs"),
    } <= {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in insp.get_foreign_keys("opt_fills", schema="trader")
    }
    for name, (table, columns, unique) in INDEXES.items():
        found = {ix["name"]: ix for ix in insp.get_indexes(table, schema="trader")}
        assert name in found, name
        assert (found[name]["column_names"], bool(found[name]["unique"])) == (columns, unique), name
    assert "uq_opt_positions_structure_contract" in _index_names(migrated_engine, "opt_positions")


# --- constraints --------------------------------------------------------------------------------------------


@dataclass
class Seed:
    run: int
    symbol: int
    put: int
    call: int
    structure: int
    position: int
    order: int
    leg: int
    config: int


def _seed(s: Session) -> Seed:
    run = f.add_options_run(s)
    symbol = f.add_underlying(s, "F", questrade_id=19719)
    put = f.add_contract(s, symbol, strike="14.50", right="put", qt_symbol_id=101)
    call = f.add_contract(s, symbol, strike="15.00", right="call", qt_symbol_id=102)
    structure = f.add_structure(s, run, symbol, reserved_cash="1450")
    position = f.add_position(s, run, structure, symbol, contract_id=put)
    order = f.add_order(s, run, symbol, legs=[f.make_leg(1, contract_id=put)], status="filled")
    leg = s.execute(
        text("SELECT id FROM trader.opt_order_legs WHERE order_id = :o"), {"o": order}
    ).scalar_one()
    cfg = m.OptionStrategyConfig(
        strategy_key="wheel", version="1.0.0", revision=1, params={}, enabled=True, created_at=f.T0,
        created_by="test",
    )  # fmt: skip
    s.add(cfg)
    s.flush()
    return Seed(run, symbol, put, call, structure, position, order, leg, cfg.id)


def _contract(x: Seed, **over: Any) -> m.OptionContract:
    values: dict[str, Any] = {
        "underlying_symbol_id": x.symbol, "underlying": "F", "qt_symbol_id": 999, "root": "F",
        "expiry": f.EXPIRY, "strike": D("16.00"), "right": "put", "is_monthly": True, "first_seen_at": f.T0,
    }  # fmt: skip
    return m.OptionContract(**{**values, **over})


def _structure(x: Seed, **over: Any) -> m.OptStructure:
    values: dict[str, Any] = {
        "run_id": x.run, "source": "manual", "kind": "csp", "underlying_symbol_id": x.symbol,
        "underlying": "F", "state": "open", "qty": 1, "entry_net": D("0.45"), "opened_at": f.T0,
    }  # fmt: skip
    return m.OptStructure(**{**values, **over})


def _position(x: Seed, **over: Any) -> m.OptPosition:
    values: dict[str, Any] = {
        "run_id": x.run, "structure_id": x.structure, "instrument": "option", "contract_id": x.call,
        "underlying_symbol_id": x.symbol, "qty": 1, "avg_price": D("1.20"), "opened_at": f.T0,
    }  # fmt: skip
    return m.OptPosition(**{**values, **over})


def _order(x: Seed, **over: Any) -> m.OptOrder:
    values: dict[str, Any] = {
        "run_id": x.run, "source": "manual", "intent": "open", "underlying_symbol_id": x.symbol,
        "order_type": "limit", "net_limit": D("0.45"), "tif": "day", "qty": 1, "status": "working",
        "reason": "test", "session_date": f.SESSION, "submitted_at": f.T0, "submitted_by": "test",
        "updated_at": f.T0,
    }  # fmt: skip
    return m.OptOrder(**{**values, **over})


def _leg(x: Seed, **over: Any) -> m.OptOrderLeg:
    values: dict[str, Any] = {
        "order_id": x.order, "leg_no": 2, "instrument": "option", "contract_id": x.call,
        "underlying_symbol_id": x.symbol, "side": "buy", "effect": "open", "ratio": 1,
    }  # fmt: skip
    return m.OptOrderLeg(**{**values, **over})


def _fill(x: Seed, **over: Any) -> m.OptFill:
    values: dict[str, Any] = {
        "run_id": x.run, "order_id": x.order, "leg_id": x.leg, "structure_id": x.structure, "ts": f.T0,
        "side": "sell", "qty": 1, "price": D("0.45"), "fee": D("0.99"), "quote": {"bid": "0.45"},
        "usd_cad_rate": D("1.388889"),
    }  # fmt: skip
    return m.OptFill(**{**values, **over})


def _lifecycle(x: Seed, **over: Any) -> m.OptLifecycleEvent:
    values: dict[str, Any] = {
        "run_id": x.run, "structure_id": x.structure, "position_id": x.position, "contract_id": x.put,
        "kind": "expired", "session_date": f.SESSION, "ts": f.T0, "qty": 1,
    }  # fmt: skip
    return m.OptLifecycleEvent(**{**values, **over})


def _prompt(x: Seed, **over: Any) -> m.OwnerPrompt:
    values: dict[str, Any] = {
        "run_id": x.run, "source": "wheel", "kind": "candidate", "scope_key": "F",
        "dedupe_key": "wheel:cand:F", "title": "Approve F?", "body": "...",
        "choices": [{"code": "a", "label": "Approve"}], "status": "pending", "asked_at": f.T0,
    }  # fmt: skip
    return m.OwnerPrompt(**{**values, **over})


def _wheel_ticker(x: Seed, **over: Any) -> m.WheelTicker:
    values: dict[str, Any] = {
        "symbol_id": x.symbol, "ticker": "F", "status": "candidate", "origin": "manual",
        "created_at": f.T0, "updated_at": f.T0, "updated_by": "test",
    }  # fmt: skip
    return m.WheelTicker(**{**values, **over})


def _wheel_position(x: Seed, **over: Any) -> m.WheelPosition:
    values: dict[str, Any] = {
        "run_id": x.run, "symbol_id": x.symbol, "ticker": "F", "state": "PUT_OPEN", "contracts": 1,
        "opened_at": f.T0,
    }  # fmt: skip
    return m.WheelPosition(**{**values, **over})


def _wheel_event(x: Seed, position_id: int | None, kind: str = "evaluate") -> m.WheelEvent:
    return m.WheelEvent(
        run_id=x.run, wheel_position_id=position_id, symbol_id=x.symbol, session_date=f.SESSION, ts=f.T0,
        kind=kind, reason="HOLD",
    )  # fmt: skip


def _twice(build: Callable[[Seed], Any]) -> Callable[[Session, Seed], None]:
    """The same row added twice: the second breaks a unique key."""

    def run(s: Session, x: Seed) -> None:
        s.add(build(x))
        s.flush()
        s.add(build(x))

    return run


def _one(build: Callable[[Seed], Any]) -> Callable[[Session, Seed], None]:
    return lambda s, x: s.add(build(x))


def _two_wheel_evaluations(s: Session, x: Seed) -> None:
    position = _wheel_position(x)
    s.add(position)
    s.flush()
    s.add(_wheel_event(x, position.id))
    s.flush()
    s.add(_wheel_event(x, position.id))


VIOLATIONS: list[tuple[str, Callable[[Session, Seed], None]]] = [
    ("uq_runs_one_active_options", lambda s, x: s.add(m.Run(
        mode="options", started_at=f.T0, params={}, status="active", label="options"))),
    ("uq_option_contracts_qt_symbol_id", _one(lambda x: _contract(x, qt_symbol_id=101))),
    ("uq_option_contracts_key", _one(lambda x: _contract(x, strike=D("14.50")))),
    ("ck_option_contracts_right", _one(lambda x: _contract(x, right="both"))),
    ("option_chain_cache_pkey", _twice(lambda x: m.OptionChainCache(
        underlying_symbol_id=x.symbol, fetched_at=f.T0, chain=[], expiries=0))),
    ("option_quote_marks_pkey", _twice(lambda x: m.OptionQuoteMark(contract_id=x.put, fetched_at=f.T0))),
    ("underlying_facts_pkey", _twice(lambda x: m.UnderlyingFacts(
        symbol_id=x.symbol, as_of=f.SESSION, ticker="F", sources={}, fetched_at=f.T0))),
    ("uq_option_strategy_configs_key_revision", _one(lambda x: m.OptionStrategyConfig(
        strategy_key="wheel", version="1.0.1", revision=1, params={}, enabled=True, created_at=f.T0,
        created_by="test"))),
    ("option_strategy_state_pkey", _twice(lambda x: m.OptionStrategyState(
        strategy_key="wheel", scope_key="account", value={}, updated_at=f.T0))),
    ("ck_opt_structures_state", _one(lambda x: _structure(x, state="half"))),
    ("ck_opt_structures_qty_positive", _one(lambda x: _structure(x, qty=0))),
    ("ck_opt_structures_reserved_nonnegative", _one(lambda x: _structure(x, reserved_cash=D("-1")))),
    ("ck_opt_positions_instrument", _one(lambda x: _position(x, contract_id=None))),
    ("ck_opt_positions_instrument", _one(lambda x: _position(x, instrument="shares"))),
    ("uq_opt_positions_structure_contract", _one(lambda x: _position(x, contract_id=x.put))),
    ("uq_opt_positions_structure_contract", _twice(lambda x: _position(
        x, instrument="shares", contract_id=None, qty=100))),
    ("ck_opt_orders_qty_positive", _one(lambda x: _order(x, qty=0))),
    ("ck_opt_orders_intent", _one(lambda x: _order(x, intent="hedge"))),
    ("ck_opt_orders_order_type", _one(lambda x: _order(x, order_type="stop"))),
    ("ck_opt_orders_tif", _one(lambda x: _order(x, tif="ioc"))),
    ("ck_opt_orders_status", _one(lambda x: _order(x, status="pending"))),
    ("ck_opt_orders_limit", _one(lambda x: _order(x, net_limit=None))),
    ("uq_opt_order_legs_order_leg", _one(lambda x: _leg(x, leg_no=1))),
    ("ck_opt_order_legs_ratio_positive", _one(lambda x: _leg(x, ratio=0))),
    ("uq_opt_fills_order_leg", _twice(_fill)),
    ("ck_opt_fills_qty_positive", _one(lambda x: _fill(x, qty=0))),
    ("ck_opt_fills_price_nonnegative", _one(lambda x: _fill(x, price=D("-0.01")))),
    ("ck_opt_fills_fee_nonnegative", _one(lambda x: _fill(x, fee=D("-0.01")))),
    ("uq_opt_lifecycle_position_kind_day", _twice(_lifecycle)),
    ("uq_owner_prompts_dedupe_key", _twice(_prompt)),
    ("wheel_tickers_pkey", _twice(_wheel_ticker)),
    ("ck_wheel_tickers_status", _one(lambda x: _wheel_ticker(x, status="maybe"))),
    ("ck_wheel_positions_state", _one(lambda x: _wheel_position(x, state="WAITING"))),
    ("uq_wheel_positions_open_run_symbol", _twice(_wheel_position)),
    ("uq_wheel_events_evaluate_position_day", _two_wheel_evaluations),
]  # fmt: skip


@pytest.mark.parametrize(
    ("constraint", "violate"), VIOLATIONS, ids=[f"{n}:{i}" for i, (n, _) in enumerate(VIOLATIONS)]
)
def test_table_constraints(
    db_factory: sessionmaker[Session], constraint: str, violate: Callable[[Session, Seed], None]
) -> None:
    with db_factory() as s:
        seed = _seed(s)
        s.commit()
        violate(s, seed)
        with pytest.raises(IntegrityError, match=constraint):
            s.commit()


def test_the_keys_allow_what_they_should(db_factory: sessionmaker[Session]) -> None:
    """The other side of each rule: rows that look alike but are allowed, and the server defaults."""
    with db_factory() as s:
        x = _seed(s)
        s.add(m.Run(mode="options", started_at=f.T0, params={}, status="completed", label="options"))
        add_run(s, mode="live", label="live")  # an active live run beside the active options run
        s.add(_position(x, instrument="shares", contract_id=None, qty=100))  # shares beside an option
        s.add(_position(x))  # another contract in the same structure
        s.add(_order(x, order_type="market", net_limit=None))
        s.add(_order(x, net_limit=None, walk=True))  # the walk picks the limit
        s.add(_order(x, net_limit=None, status="rejected"))  # stored as it was asked
        s.add(_order(x, net_limit=D("-1.20"), tif="gtc", intent="roll"))  # a debit limit
        s.add(_lifecycle(x, kind="frozen"))
        s.add(_lifecycle(x, session_date=f.SESSION + timedelta(days=1)))
        s.add(_lifecycle(x))
        closed = _wheel_position(x, state="NONE", closed_at=f.T0, close_reason="put_expired")
        open_now = _wheel_position(x)
        s.add_all([closed, open_now])
        s.flush()
        s.add_all([_wheel_event(x, open_now.id), _wheel_event(x, closed.id)])
        s.add_all([_wheel_event(x, open_now.id, "action"), _wheel_event(x, open_now.id, "action")])
        s.add_all([_wheel_event(x, None, "screen"), _wheel_event(x, None, "screen")])
        s.add(_prompt(x))
        s.add(_wheel_ticker(x))
        s.commit()

        structure = s.get_one(m.OptStructure, x.structure)
        assert (structure.frozen, structure.realized_pnl, structure.fees_total) == (False, 0, 0)
        assert structure.meta == {} and structure.close_reason is None
        contract = s.get_one(m.OptionContract, x.put)
        assert (contract.multiplier, contract.adjusted, contract.right) == (100, False, "put")
        order = s.get_one(m.OptOrder, x.order)
        assert (order.walk, order.reserved_cash, order.evidence) == (False, 0, {})
        prompt = s.execute(text("SELECT needs_text, send_count, data FROM trader.owner_prompts")).one()
        assert tuple(prompt) == (False, 0, {})
        ticker = s.get_one(m.WheelTicker, x.symbol)
        assert (ticker.thesis_broken, ticker.acknowledged_cautions, ticker.would_own) == (False, [], None)
        wheel = s.get_one(m.WheelPosition, open_now.id)
        assert (wheel.roll_count, wheel.total_put_premium, wheel.dividends, wheel.entry) == (0, 0, 0, {})
        s.add(m.OptionStrategyState(strategy_key="wheel", scope_key="account", value={}, updated_at=f.T0))
        s.commit()
        assert s.execute(text("SELECT version FROM trader.option_strategy_state")).scalar_one() == 1
