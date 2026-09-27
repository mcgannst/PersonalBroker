"""P1-T2 gauntlet (Breaker): try to break the DB models, migration 0001 and the session helpers.

Everything runs in the throwaway PostgreSQL 14 test container (conftest fixtures), never trader_dev.
Two tests create extra databases (and throwaway roles) inside that same container.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import alembic_config
from trader.db.models import DailyCandle, IntradayCandle, JobRun, Symbol, UniverseSnapshot
from trader.db.session import make_engine, make_session_factory, session_scope

pytestmark = pytest.mark.db

ET = ZoneInfo("America/New_York")
PART_OF_TS = "SELECT tableoid::regclass::text FROM trader.intraday_candles WHERE ts = :ts"
# 2026-06 .. 2028-12 inclusive is 31 monthly partitions, plus the default partition.
EXPECTED_PARTITIONS = 32


def _url(base: str, **parts: str) -> str:
    return make_url(base).set(**parts).render_as_string(hide_password=False)  # type: ignore[arg-type]


def _run_admin(url: str, *statements: str) -> None:
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        engine.dispose()


def _candle(symbol_id: int, ts: datetime, px: str = "10") -> IntradayCandle:
    p = Decimal(px)
    return IntradayCandle(
        symbol_id=symbol_id, interval="5m", ts=ts, open=p, high=p, low=p, close=p, volume=1, vwap=None
    )


@pytest.fixture(scope="module")
def edmonton_db_url(pg_url: str) -> str:
    """A second database whose server-side TimeZone is America/Edmonton (as a LAN server may be),
    migrated to head with the owner-style URL."""
    _run_admin(
        pg_url,
        "DROP DATABASE IF EXISTS breaker_tz",
        "CREATE DATABASE breaker_tz",
        "ALTER DATABASE breaker_tz SET timezone TO 'America/Edmonton'",
    )
    url = _url(pg_url, database="breaker_tz")
    command.upgrade(alembic_config(url), "head")
    return url


# 1. Partition boundaries ---------------------------------------------------------------------------


def test_partition_boundaries_route_to_the_right_month(db_factory: sessionmaker[Session]) -> None:
    cases = [
        (datetime(2026, 5, 31, 23, 59, 59, 999999, tzinfo=UTC), "trader.intraday_candles_default"),
        (datetime(2026, 6, 1, tzinfo=UTC), "trader.intraday_candles_202606"),
        (datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC), "trader.intraday_candles_202609"),
        (datetime(2026, 10, 1, tzinfo=UTC), "trader.intraday_candles_202610"),
        (datetime(2028, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), "trader.intraday_candles_202812"),
        (datetime(2029, 1, 1, tzinfo=UTC), "trader.intraday_candles_default"),
        (datetime(2035, 7, 4, 14, 30, tzinfo=UTC), "trader.intraday_candles_default"),
        (datetime(1999, 1, 4, 14, 30, tzinfo=UTC), "trader.intraday_candles_default"),
    ]
    with db_factory() as s:
        sym = Symbol(ticker="EDGE", exchange="NYSE", questrade_id=101, currency="USD", name=None)
        s.add(sym)
        s.flush()
        for ts, _ in cases:
            s.add(_candle(sym.id, ts))
        s.commit()
        for ts, expected in cases:
            assert s.execute(text(PART_OF_TS), {"ts": ts}).scalar_one() == expected, ts
        # The microsecond survives the round trip (no truncation to ms / s).
        got = s.scalars(select(IntradayCandle.ts).order_by(IntradayCandle.ts)).all()
        assert got == sorted(ts for ts, _ in cases)
        # The composite PK still holds for rows that land in the default partition.
        s.add(_candle(sym.id, datetime(2029, 1, 1, tzinfo=UTC), "11"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_partition_bounds_are_utc_months_whatever_the_server_timezone(edmonton_db_url: str) -> None:
    """The migration writes bounds as bare dates ('2026-10-01'), which PostgreSQL reads in the session
    TimeZone. On a server whose TimeZone isn't UTC the months shift by the UTC offset, so
    2026-10-01 03:00Z (still 30 Sep in Edmonton) lands in the September partition. Monthly retention /
    archiving by partition would then move the wrong rows."""
    engine = make_engine(edmonton_db_url)
    try:
        with make_session_factory(engine)() as s:
            sym = Symbol(ticker="TZB", exchange="NYSE", questrade_id=1, currency="USD", name=None)
            s.add(sym)
            s.flush()
            ts = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
            s.add(_candle(sym.id, ts))
            s.commit()
            part = s.execute(text(PART_OF_TS), {"ts": ts}).scalar_one()
    finally:
        engine.dispose()
    assert part == "trader.intraday_candles_202610"


# 2. Time zones ---------------------------------------------------------------------------------------


def test_et_aware_datetimes_round_trip_as_utc(
    db_factory: sessionmaker[Session], edmonton_db_url: str
) -> None:
    fall_back_second_0130 = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=ET)  # EST -> 06:30Z
    spring_forward = datetime(2026, 3, 8, 3, 0, tzinfo=ET)  # EDT -> 07:00Z
    expected_start = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    expected_finish = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)

    def store_and_read(factory: sessionmaker[Session]) -> tuple[datetime, datetime | None]:
        with factory() as s:
            s.add(
                JobRun(
                    job="tz",
                    session_date=date(2026, 11, 2),
                    started_at=fall_back_second_0130,
                    finished_at=spring_forward,
                    status="succeeded",
                    error=None,
                    detail=None,
                )
            )
            s.commit()
        with factory() as s:
            row = s.execute(select(JobRun.started_at, JobRun.finished_at)).one()
            return row[0], row[1]

    # Default container (server TimeZone UTC).
    started, finished = store_and_read(db_factory)
    assert started == expected_start
    assert finished == expected_finish
    assert started.utcoffset() == timedelta(0)

    # A server whose TimeZone isn't UTC: the app's engine should still hand back UTC datetimes.
    engine = make_engine(edmonton_db_url)
    try:
        started, finished = store_and_read(make_session_factory(engine))
    finally:
        engine.dispose()
    assert started == expected_start
    assert finished == expected_finish
    assert started.utcoffset() == timedelta(0), f"read back as {started.isoformat()}, not UTC"


# 3. Money precision ----------------------------------------------------------------------------------


def test_money_rounds_to_four_dp_and_rejects_overflow(db_factory: sessionmaker[Session]) -> None:
    inputs = ["1.23455", "-1.23455", "0.00005", "335.56434999", "9999999999.9999", "0.0001"]
    with db_factory() as s:
        sym = Symbol(ticker="MNY", exchange="NYSE", questrade_id=7, currency="USD", name=None)
        s.add(sym)
        s.flush()
        sym_id = sym.id
        for i, raw in enumerate(inputs):
            p = Decimal(raw)
            s.add(
                DailyCandle(
                    symbol_id=sym_id,
                    date=date(2026, 1, 1) + timedelta(days=i),
                    open=p,
                    high=p,
                    low=p,
                    close=p,
                    volume=10**15,
                    vwap=p,
                )
            )
        s.commit()
        got = s.scalars(select(DailyCandle.close).order_by(DailyCandle.date)).all()
    assert all(isinstance(v, Decimal) for v in got)
    assert got == [Decimal(r).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) for r in inputs]

    for too_big in ("10000000000", "9999999999.99995", "-10000000000"):
        with db_factory() as s:
            p = Decimal(too_big)
            s.add(
                DailyCandle(
                    symbol_id=sym_id,
                    date=date(2027, 1, 1),
                    open=p,
                    high=p,
                    low=p,
                    close=p,
                    volume=1,
                    vwap=None,
                )
            )
            with pytest.raises(DBAPIError, match="numeric field overflow"):
                s.commit()


# 4. Constraints --------------------------------------------------------------------------------------


def test_foreign_keys_and_symbol_uniqueness(db_factory: sessionmaker[Session]) -> None:
    missing = 424242
    with db_factory() as s:
        s.add(
            UniverseSnapshot(
                session_date=date(2026, 9, 25),
                symbol_id=missing,
                price=Decimal("10"),
                avg_volume=1,
                atr14=None,
                source="finviz",
            )
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            s.commit()
    with db_factory() as s:
        p = Decimal("1")
        s.add(
            DailyCandle(
                symbol_id=missing, date=date(2026, 9, 25), open=p, high=p, low=p, close=p, volume=1, vwap=None
            )
        )
        with pytest.raises(IntegrityError, match="foreign key"):
            s.commit()

    with db_factory() as s:
        s.add(Symbol(ticker="AAPL", exchange="NASDAQ", questrade_id=8049, currency="USD", name="Apple"))
        s.commit()
    with db_factory() as s:  # same (ticker, exchange), different questrade_id
        s.add(Symbol(ticker="AAPL", exchange="NASDAQ", questrade_id=9999, currency="USD", name=None))
        with pytest.raises(IntegrityError, match="unique"):
            s.commit()
    with db_factory() as s:  # same questrade_id, different ticker
        s.add(Symbol(ticker="AAPL2", exchange="NASDAQ", questrade_id=8049, currency="USD", name=None))
        with pytest.raises(IntegrityError, match="unique"):
            s.commit()
    with db_factory() as s:  # same ticker on another exchange, and several NULL questrade_ids, are fine
        s.add(Symbol(ticker="AAPL", exchange="NEO", questrade_id=None, currency="CAD", name=None))
        s.add(Symbol(ticker="BBB", exchange="TSX", questrade_id=None, currency="CAD", name=None))
        s.commit()
        assert s.scalar(select(func.count()).select_from(Symbol)) == 3

    # A referenced symbol can't be deleted out from under its candles.
    with db_factory() as s:
        sym_id = s.scalars(select(Symbol.id).where(Symbol.questrade_id == 8049)).one()
        p = Decimal("1")
        s.add(
            DailyCandle(
                symbol_id=sym_id, date=date(2026, 9, 25), open=p, high=p, low=p, close=p, volume=1, vwap=None
            )
        )
        s.commit()
        with pytest.raises(IntegrityError, match="foreign key"):
            s.execute(text("DELETE FROM trader.symbols WHERE id = :i"), {"i": sym_id})


# 5. session_scope --------------------------------------------------------------------------------------


def test_session_scope_rolls_back_on_error_and_commits_on_success(
    db_factory: sessionmaker[Session],
) -> None:
    def count() -> int:
        with db_factory() as s:
            return int(s.scalar(select(func.count()).select_from(Symbol)) or 0)

    with pytest.raises(RuntimeError, match="boom"):
        with session_scope(db_factory) as s:
            s.add(Symbol(ticker="RB1", exchange="NYSE", questrade_id=1, currency="USD", name=None))
            s.flush()  # the row reached the DB inside the transaction
            raise RuntimeError("boom")
    assert count() == 0

    # An error raised by the commit itself (unique violation) must also leave nothing behind.
    with pytest.raises(IntegrityError):
        with session_scope(db_factory) as s:
            s.add(Symbol(ticker="RB2", exchange="NYSE", questrade_id=2, currency="USD", name=None))
            s.add(Symbol(ticker="RB2", exchange="NYSE", questrade_id=3, currency="USD", name=None))
    assert count() == 0

    with session_scope(db_factory) as s:
        s.add(Symbol(ticker="OK", exchange="NYSE", questrade_id=4, currency="USD", name=None))
    assert count() == 1


# 6. Re-running the migration ----------------------------------------------------------------------------


def test_upgrade_at_head_is_a_noop(pg_url: str, db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        s.add(Symbol(ticker="KEEP", exchange="NYSE", questrade_id=55, currency="USD", name=None))
        s.commit()

    def snapshot() -> tuple[list[str], int, list[str]]:
        with db_factory() as s:
            versions = list(s.scalars(text("SELECT version_num FROM trader.alembic_version")).all())
            parts = int(
                s.scalar(
                    text(
                        "SELECT count(*) FROM pg_inherits "
                        "WHERE inhparent = 'trader.intraday_candles'::regclass"
                    )
                )
                or 0
            )
            tables = list(
                s.scalars(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'trader' ORDER BY tablename")
                ).all()
            )
        return versions, parts, tables

    before = snapshot()
    cfg = alembic_config(pg_url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    after = snapshot()

    assert before == after
    assert after[0] == ["0001"]
    assert after[1] == EXPECTED_PARTITIONS
    with db_factory() as s:
        assert s.scalars(select(Symbol.ticker)).all() == ["KEEP"]


# 7. Least privilege (SPEC §13, §15: owner migrates, the non-owner app role only reads/writes) ------------


@pytest.fixture(scope="module")
def least_privilege_urls(pg_url: str) -> Iterator[tuple[str, str]]:
    """Mirror the dev set-up: an owner role owns the database and schema `trader` and runs the migration;
    the app role gets USAGE on the schema and DML on new tables through default privileges only."""
    _run_admin(
        pg_url,
        "DROP DATABASE IF EXISTS breaker_lp",
        "DROP ROLE IF EXISTS bk_app",
        "DROP ROLE IF EXISTS bk_owner",
        "CREATE ROLE bk_owner LOGIN PASSWORD 'bk_owner_pw'",
        "CREATE ROLE bk_app LOGIN PASSWORD 'bk_app_pw'",
        "CREATE DATABASE breaker_lp OWNER bk_owner",
    )
    super_url = _url(pg_url, database="breaker_lp")
    _run_admin(
        super_url,
        "CREATE SCHEMA trader AUTHORIZATION bk_owner",
        "GRANT USAGE ON SCHEMA trader TO bk_app",
        "ALTER DEFAULT PRIVILEGES FOR ROLE bk_owner IN SCHEMA trader "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bk_app",
    )
    owner_url = _url(pg_url, database="breaker_lp", username="bk_owner", password="bk_owner_pw")
    app_url = _url(pg_url, database="breaker_lp", username="bk_app", password="bk_app_pw")
    command.upgrade(alembic_config(owner_url), "head")
    yield owner_url, app_url


def test_app_role_can_write_but_cannot_do_ddl(least_privilege_urls: tuple[str, str]) -> None:
    _, app_url = least_privilege_urls
    engine = make_engine(app_url)
    try:
        # DML works through default privileges, including identity columns and partition routing.
        with session_scope(make_session_factory(engine)) as s:
            sym = Symbol(ticker="LP", exchange="NYSE", questrade_id=1, currency="USD", name=None)
            s.add(sym)
            s.flush()
            s.add(_candle(sym.id, datetime(2026, 9, 25, 13, 30, tzinfo=UTC)))
            s.add(_candle(sym.id, datetime(2030, 1, 2, 14, 30, tzinfo=UTC)))  # default partition
            s.add(
                JobRun(
                    job="lp",
                    session_date=date(2026, 9, 25),
                    started_at=datetime(2026, 9, 25, tzinfo=UTC),
                    finished_at=None,
                    status="running",
                    error=None,
                    detail=None,
                )
            )

        ddl = [
            "CREATE TABLE trader.evil (id int)",
            "DROP TABLE trader.symbols",
            "TRUNCATE trader.symbols CASCADE",
            "CREATE TABLE trader.intraday_candles_203001 PARTITION OF trader.intraday_candles "
            "FOR VALUES FROM ('2030-01-01') TO ('2030-02-01')",
            "ALTER TABLE trader.symbols ADD COLUMN evil int",
        ]
        for stmt in ddl:
            with engine.connect() as conn:
                with pytest.raises(ProgrammingError, match="permission denied|must be owner"):
                    conn.execute(text(stmt))
    finally:
        engine.dispose()
