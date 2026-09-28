"""P5-T14: `error` / `critical` log lines mirrored into `event_log`, never blocking, recursing or leaking."""

import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trader.db.models import EventLog
from trader.logging_mirror import MIRROR_SOURCE_PREFIX, EventLogMirror, install_event_mirror
from trader.logging_setup import configure_logging  # the real one (conftest patches the module attribute)
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

T0 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
TOUCHED_LOGGERS = ("", "httpx", "httpcore", "telegram", "sqlalchemy")


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Whatever configure_logging does in the test is undone afterwards (handler, levels, structlog)."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    levels = {name: logging.getLogger(name).level for name in TOUCHED_LOGGERS}
    structlog.reset_defaults()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


@pytest.fixture
def fresh_logging(clean_logging: None) -> None:
    """A fresh root logger configured by the real configure_logging."""
    configure_logging("test")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def mirrors() -> Iterator[list[EventLogMirror]]:
    """Every mirror a test makes is closed at the end (handler removed, thread stopped)."""
    made: list[EventLogMirror] = []
    yield made
    for mirror in made:
        mirror.close()


def make(
    mirrors: list[EventLogMirror],
    factory: Any,
    clock: FixedClock,
    *,
    level: Any = "error",
    max_per_minute: int = 30,
    run_id: int | None = None,
    queue_size: int = 1000,
    process: str = "test",
) -> EventLogMirror:
    mirror = EventLogMirror(
        factory,
        clock,
        process=process,
        level=level,
        max_per_minute=max_per_minute,
        run_id=run_id,
        queue_size=queue_size,
    )
    mirrors.append(mirror)
    mirror.install()
    return mirror


def rows(factory: sessionmaker[Session]) -> list[EventLog]:
    with factory() as session:
        return list(session.scalars(select(EventLog).order_by(EventLog.id)))


def settings(**values: Any) -> RuntimeSettings:
    return RuntimeSettings.model_validate(values)


# --- 1. what is mirrored ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_error_lines_are_mirrored_and_lower_levels_are_not(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = install_event_mirror(db_factory, clock, "test", settings())
    assert mirror is not None
    mirrors.append(mirror)
    structlog.get_logger("trader.x").error("thing.failed", symbol="AAA")
    logging.getLogger("finviz").error("boom")
    structlog.get_logger("trader.x").info("thing.ok", symbol="AAA")
    structlog.get_logger("trader.x").warning("thing.odd", symbol="AAA")
    logging.getLogger("finviz").warning("meh")
    assert mirror.flush() == 2

    got = rows(db_factory)
    assert [(r.level, r.source, r.message) for r in got] == [
        ("error", "log.test", "trader.x: thing.failed"),
        ("error", "log.test", "finviz: boom"),
    ]
    assert got[0].data == {"symbol": "AAA"}
    assert got[0].run_id is None and got[0].ts == T0
    assert MIRROR_SOURCE_PREFIX == "log."


@pytest.mark.usefixtures("fresh_logging")
def test_exception_records_carry_type_and_one_line_message_never_the_traceback(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    try:
        raise ValueError("bad value password=hunter2\nsecond line")
    except ValueError:
        structlog.get_logger("trader.x").exception("thing.crashed")
        logging.getLogger("finviz").exception("fetch failed")
    assert mirror.flush() == 2
    for row in rows(db_factory):
        assert row.data["exc_type"] == "ValueError"
        assert "\n" not in row.data["exc_message"]
        assert "hunter2" not in json.dumps(row.data)
        assert "Traceback" not in json.dumps(row.data)
        assert "exception" not in row.data


# --- 2. masked ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_secrets_are_masked(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    url = "https://api.telegram.org/bot123456:AAAAsecretBotTokenValue/getUpdates"
    structlog.get_logger("trader.x").error(
        "poll.failed", token="abc123secret", url=url, nested={"password": "pw-secret-1", "ok": 1}
    )
    logging.getLogger("telegram.ext").error("GET %s failed", url)
    assert mirror.flush() == 2
    got = rows(db_factory)
    text = json.dumps([[r.message, r.data] for r in got])
    for secret in ("abc123secret", "AAAAsecretBotTokenValue", "pw-secret-1"):
        assert secret not in text
    assert got[0].data["token"] == "[REDACTED]"
    assert "[REDACTED]" in got[0].data["url"]
    assert got[0].data["nested"] == {"password": "[REDACTED]", "ok": 1}
    assert "[REDACTED]" in got[1].message


@pytest.mark.usefixtures("fresh_logging")
def test_message_and_data_are_cut(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock, process="p" * 80)
    fields = {f"k{i}": "v" * 300 for i in range(40)}
    structlog.get_logger("trader.x").error("e" * 900, **fields)
    assert mirror.flush() == 1
    (row,) = rows(db_factory)
    assert len(row.source) == 50 and row.source.startswith("log.")
    assert len(row.message) == 500
    assert len(json.dumps(row.data)) <= 4096
    assert row.data["truncated"] is True


# --- 3. non-blocking ----------------------------------------------------------------------------------------


def test_emit_never_blocks_on_a_paused_database(clock: FixedClock, mirrors: list[EventLogMirror]) -> None:
    release = threading.Event()
    entered = threading.Event()

    def paused_factory() -> Session:
        entered.set()
        release.wait(30)
        raise ConnectionError("database paused")

    mirror = make(mirrors, paused_factory, clock, queue_size=100)
    mirror.start()
    handler = mirror.handler
    assert handler is not None
    logger = logging.getLogger("trader.paused")
    records = [
        logger.makeRecord("trader.paused", logging.ERROR, __file__, 1, f"line {i}", (), None)
        for i in range(5000)
    ]
    try:
        started = time.perf_counter()
        for record in records[:10]:
            handler.emit(record)
        elapsed = time.perf_counter() - started
        assert entered.wait(5)  # the writer thread is now stuck in the paused connect
        started = time.perf_counter()
        for record in records[10:]:
            handler.emit(record)
        elapsed += time.perf_counter() - started
        assert elapsed < 1.0
        assert mirror.queued() <= 100
        assert mirror.dropped >= 5000 - 100 - 10
    finally:
        release.set()


# --- 4. no recursion ----------------------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_logging")
def test_a_failing_database_never_raises_or_logs(
    clock: FixedClock, mirrors: list[EventLogMirror], capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("test")  # here, so its handler writes to capsys's stdout

    def broken_factory() -> Session:
        logging.getLogger("trader.db").error("inside the write")  # would recurse if mirrored back
        raise RuntimeError("database down")

    mirror = make(mirrors, broken_factory, clock)
    mirror.start()
    capsys.readouterr()
    for i in range(5):
        structlog.get_logger("trader.x").error(f"thing.failed.{i}")
    assert mirror.flush() == 0
    mirror.close()
    structlog.get_logger("trader.x").error("after.close")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [line["event"] for line in lines]
    # the five lines and the post-close line reach stdout; the mirror added only the one line its
    # factory logs per failed write attempt, none of which came back into the queue
    assert [e for e in events if e.startswith("thing.")] == [f"thing.failed.{i}" for i in range(5)]
    assert events[-1] == "after.close"
    assert {e for e in events if not e.startswith("thing.")} <= {"after.close", "inside the write"}
    assert mirror.dropped >= 5
    assert mirror.queued() == 0


def test_errors_inside_emit_are_swallowed(clock: FixedClock, mirrors: list[EventLogMirror]) -> None:
    class Unprintable:
        def __str__(self) -> str:
            raise RuntimeError("no str")

    mirror = make(mirrors, None, clock)
    handler = mirror.handler
    assert handler is not None
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "%s %s", (Unprintable(),), None)
    handler.handle(record)  # a broken message still gives a row, and never an exception
    bad = logging.LogRecord("x", logging.ERROR, __file__, 1, {"event": Unprintable()}, (), None)
    handler.handle(bad)
    assert mirror.flush() == 0  # a None factory: the batch fails and is counted, never raised


# --- 5. rate limits -----------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_repeats_are_folded_into_the_next_row(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    log = structlog.get_logger("trader.x")
    for i in range(10):
        log.error("thing.failed", i=i)
        clock.advance(timedelta(seconds=5))
    assert mirror.flush() == 1
    clock.set(T0 + timedelta(seconds=61))
    log.error("thing.failed", i=10)
    assert mirror.flush() == 1
    got = rows(db_factory)
    assert len(got) == 2
    assert "repeated" not in got[0].data
    assert got[1].data["repeated"] == 9


@pytest.mark.usefixtures("fresh_logging")
def test_at_most_max_per_minute_rows_then_one_dropped_summary(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock, max_per_minute=30)
    log = structlog.get_logger("trader.x")
    for i in range(100):
        log.error(f"thing.{i}")
    assert mirror.flush() == 30
    clock.advance(timedelta(seconds=60))
    assert mirror.flush() == 1
    got = rows(db_factory)
    assert len(got) == 31
    summary = got[-1]
    assert (summary.level, summary.source) == ("error", "log.test")
    assert summary.message == "log mirror dropped 70 lines"
    assert mirror.flush() == 0  # the summary is written once


# --- 6. skipped records -------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_logged_events_sqlalchemy_and_own_logger_are_skipped(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    structlog.get_logger("trader.x").error("already.logged", event_logged=True)
    logging.getLogger("trader.y").error("also logged", extra={"event_logged": True})
    logging.getLogger("sqlalchemy.engine").error("connection lost")
    logging.getLogger("sqlalchemy.pool.impl").error("pool reset failed")
    logging.getLogger("alembic.runtime").error("migration")
    logging.getLogger("trader.logging_mirror").error("mirror trouble")
    structlog.get_logger("trader.x").error("kept")
    assert mirror.flush() == 1
    assert [r.message for r in rows(db_factory)] == ["trader.x: kept"]


@pytest.mark.usefixtures("fresh_logging")
def test_only_exact_or_dotted_child_loggers_are_skipped(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    """Fix round 1: `sqlalchemy_utils` or `alembic_helpers` are other libraries, not the DB layer."""
    mirror = make(mirrors, db_factory, clock)
    logging.getLogger("sqlalchemy").error("skipped")
    logging.getLogger("sqlalchemy_utils").error("kept 1")
    logging.getLogger("alembic_helpers").error("kept 2")
    logging.getLogger("trader.logging_mirror_extra").error("kept 3")
    assert mirror.flush() == 3
    assert [r.message for r in rows(db_factory)] == [
        "sqlalchemy_utils: kept 1",
        "alembic_helpers: kept 2",
        "trader.logging_mirror_extra: kept 3",
    ]


# --- fix round 1: rendered tracebacks, depth, re-entrancy, flush timeout ------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_a_chained_multiline_traceback_keeps_the_last_type_and_masks_the_whole_message(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    try:
        try:
            raise KeyError("inner")
        except KeyError as inner:
            raise ConnectionError(
                'connection failed\nDETAIL: dsn "postgresql://u:SENTINEL-PW-FR1@h:5432/d"\nHINT: retry'
            ) from inner
    except ConnectionError as exc:
        structlog.get_logger("trader.db").error("db.failed", exc_info=exc)
    assert mirror.flush() == 1
    (row,) = rows(db_factory)
    assert "SENTINEL-PW-FR1" not in json.dumps(row.data)
    assert row.data["exc_type"] == "ConnectionError"
    assert row.data["exc_message"].startswith("connection failed DETAIL: dsn")
    assert "HINT: retry" in row.data["exc_message"]
    assert "Traceback" not in json.dumps(row.data)


@pytest.mark.usefixtures("fresh_logging")
def test_containers_below_the_depth_limit_become_a_placeholder(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    structlog.get_logger("trader.deep").error(
        "deep", a={"b": {"c": {"token": 12345678, "d": {"e": [b"SENTINEL-FR1-BYTES"]}}}}
    )
    assert mirror.flush() == 1
    (row,) = rows(db_factory)
    assert row.data["a"]["b"]["c"] == {"token": "[REDACTED]", "d": "[nested too deep]"}


def test_a_line_logged_inside_emit_is_dropped_and_counted(
    clock: FixedClock, mirrors: list[EventLogMirror], clean_logging: None
) -> None:
    class Loud:
        def __str__(self) -> str:
            logging.getLogger("trader.loud").error("inner")
            return "loud"

    mirror = make(mirrors, None, clock)
    logging.getLogger("trader.outer").error("outer", extra={"obj": Loud()})
    assert mirror.queued() == 1
    assert mirror.dropped >= 1  # the inner line (once per mirror handler that rendered Loud)


def test_flush_without_a_writer_thread_honours_its_timeout(
    clock: FixedClock, mirrors: list[EventLogMirror], clean_logging: None
) -> None:
    release = threading.Event()

    def hung() -> Session:
        release.wait(30)
        raise ConnectionError("paused")

    mirror = make(mirrors, hung, clock)
    try:
        logging.getLogger("trader.x").error("pending")
        started = time.perf_counter()
        assert mirror.flush(timeout=0.2) == 0
        assert time.perf_counter() - started < 1.5
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while mirror.dropped < 1 and time.monotonic() < deadline:  # the helper's failed write is counted once
        time.sleep(0.02)
    assert mirror.dropped == 1


# --- 7. levels and off --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_off_installs_nothing(db_factory: sessionmaker[Session], clock: FixedClock) -> None:
    before = list(logging.getLogger().handlers)
    threads = threading.active_count()
    assert (
        install_event_mirror(db_factory, clock, "test", settings(**{"logging.mirror_level": "off"})) is None
    )
    assert logging.getLogger().handlers == before
    assert threading.active_count() == threads


@pytest.mark.usefixtures("fresh_logging")
def test_critical_level_mirrors_only_critical_lines(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = install_event_mirror(
        db_factory,
        clock,
        "worker",
        settings(**{"logging.mirror_level": "critical", "logging.mirror_max_per_minute": 5}),
    )
    assert mirror is not None
    mirrors.append(mirror)
    assert mirror.max_per_minute == 5
    structlog.get_logger("trader.x").error("just.an.error")
    structlog.get_logger("trader.x").critical("really.bad")
    assert mirror.flush() == 1
    assert [(r.level, r.source, r.message) for r in rows(db_factory)] == [
        ("critical", "log.worker", "trader.x: really.bad")
    ]


# --- 8. close -----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_close_flushes_and_removes_the_handler(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = install_event_mirror(db_factory, clock, "test", settings())
    assert mirror is not None
    mirrors.append(mirror)
    for i in range(3):
        structlog.get_logger("trader.x").error(f"pending.{i}")
    started = time.perf_counter()
    mirror.close()
    assert time.perf_counter() - started < 2.0
    assert len(rows(db_factory)) == 3
    assert mirror.handler not in logging.getLogger().handlers
    structlog.get_logger("trader.x").error("after.close")
    assert mirror.flush() == 0
    time.sleep(0.05)
    assert len(rows(db_factory)) == 3


# --- 9. replay run id ---------------------------------------------------------------------------------------


@pytest.mark.usefixtures("fresh_logging")
def test_replay_mirror_stamps_its_run_id_on_every_row(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = install_event_mirror(
        db_factory, clock, "replay", settings(**{"logging.mirror_max_per_minute": 2}), run_id=7
    )
    assert mirror is not None
    mirrors.append(mirror)
    for i in range(5):
        structlog.get_logger("trader.replay.runner").error(f"step.{i}")
    mirror.flush()
    clock.advance(timedelta(minutes=1))
    mirror.flush()
    got = rows(db_factory)
    assert [r.message for r in got][-1] == "log mirror dropped 3 lines"
    assert len(got) == 3
    assert {r.run_id for r in got} == {7}
    assert {r.source for r in got} == {"log.replay"}
