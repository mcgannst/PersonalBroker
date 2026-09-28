"""P5-GO gauntlet (Breaker, attempt 1): operations group O.

- P5-T14 error log mirror (`trader/logging_mirror.py`): recursion, secrets (tracebacks, structlog kwargs),
  drop counting, replay run ids, never relayed, threads, shutdown.
- P5-T15 in-process job retries (`trader/jobs/runner.py`): interrupts, the lock, "already succeeded" per
  attempt, one alert, the scheduler never retried twice, waits only through the injected sleep.
- P5-T16 compose limits (`docker/docker-compose.*.yml`): parsed as YAML, every limit and every earlier
  setting.

Sentinel secrets below are throwaway strings, never real credentials.
"""

import ast
import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog
import yaml
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from tests.fakes_telegram import FakeMessenger, RecordingNotifier
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.jobs import runner as runner_module
from trader.jobs.runner import JobOutcome, RetryPolicy, run_job, run_job_async
from trader.logging_mirror import EventLogMirror
from trader.logging_setup import configure_logging  # the real one (conftest patches the module attribute)
from trader.market.clock import FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.relay import NotificationRelay
from trader.settings_store import RuntimeSettings

APP = Path(__file__).resolve().parents[2]
TRADER_PKG = APP / "trader"
DOCKER = APP.parent / "docker"
T0 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
D = date(2026, 10, 6)
THREE = RetryPolicy(attempts=3, first_delay_s=120.0, backoff=2.0)
TOUCHED_LOGGERS = ("", "httpx", "httpcore", "telegram", "sqlalchemy", "anthropic", "httpx2")


# --- fixtures and helpers -----------------------------------------------------------------------------------


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Whatever the test does to the root logger and structlog is undone afterwards."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    root_level = root.level
    levels = {name: logging.getLogger(name).level for name in TOUCHED_LOGGERS}
    structlog.reset_defaults()
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
    root.setLevel(root_level)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    structlog.reset_defaults()


@pytest.fixture
def fresh_logging(clean_logging: None) -> None:
    configure_logging("test")


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def mirrors() -> Iterator[list[EventLogMirror]]:
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
    flush_seconds: float = 1.0,
) -> EventLogMirror:
    mirror = EventLogMirror(
        factory,
        clock,
        process=process,
        level=level,
        max_per_minute=max_per_minute,
        run_id=run_id,
        queue_size=queue_size,
        flush_seconds=flush_seconds,
    )
    mirrors.append(mirror)
    mirror.install()
    return mirror


def event_rows(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.scalars(select(m.EventLog).order_by(m.EventLog.id)))


def dumped(rows: list[m.EventLog]) -> str:
    return json.dumps([[r.level, r.source, r.message, r.data] for r in rows])


def job_rows(factory: sessionmaker[Session]) -> list[str]:
    with factory() as s:
        return list(s.scalars(select(m.JobRun.status).order_by(m.JobRun.id)))


def levels(factory: sessionmaker[Session]) -> list[str]:
    return [r.level for r in event_rows(factory)]


def advisory_locks(factory: sessionmaker[Session]) -> int:
    with factory.kw["bind"].connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
                )
            ).scalar_one()
        )


def relay_for(
    factory: sessionmaker[Session], clock: FixedClock
) -> tuple[NotificationRelay, RecordingNotifier]:
    with session_scope(factory) as s:
        run_id = add_run(s)
    notifier = RecordingNotifier()
    relay = NotificationRelay(
        factory,
        clock,
        notifier,
        MessageRenderer("http://trader.home:8080", ZoneInfo("America/Edmonton"), clock=clock),
        FakeMessenger(),
        run_id,
        settings=RuntimeSettings,
    )
    return relay, notifier


def blocking_factory(release: threading.Event, entered: threading.Event) -> Callable[[], Session]:
    def factory() -> Session:
        entered.set()
        release.wait(30)
        raise ConnectionError("database paused")

    return factory


# ============================================================================================================
# P5-T14: log mirror
# ============================================================================================================


@pytest.mark.db
@pytest.mark.usefixtures("fresh_logging")
def test_01_structlog_traceback_with_a_multiline_message_leaks_no_dsn_password(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    """`log.error(..., exc_info=exc)` (the worker's pattern, trader/worker.py `worker.part_failed`) reaches
    the mirror as a rendered traceback with no record.exc_info, and the mirror takes the type and message
    from its LAST line split at the first colon. A multi-line exception message (psycopg and SQLAlchemy
    errors are multi-line) whose last line holds a DSN is split inside the URL's scheme, so the credential
    pattern no longer matches and the password lands in `exc_message` (and `exc_type` is wrong). The
    stdout line (one redact over the whole traceback) masks it. (`log.exception` is safe: stdlib sets
    record.exc_info, and the whole str(exc) is masked.)"""
    mirror = make(mirrors, db_factory, clock)
    try:
        raise RuntimeError("connect failed\nurl=postgresql://trader:SENTINEL-DSN-PW-01@db:5432/trader_dev")
    except RuntimeError as exc:
        structlog.get_logger("trader.db_probe").error("db.connect_failed", exc_info=exc)
    assert mirror.flush() == 1
    (row,) = event_rows(db_factory)
    assert "SENTINEL-DSN-PW-01" not in dumped([row])
    assert row.data["exc_type"] == "RuntimeError"


@pytest.mark.db
@pytest.mark.usefixtures("fresh_logging")
def test_02_structlog_kwargs_and_stdlib_extras_are_masked_on_every_path(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    mirror = make(mirrors, db_factory, clock)
    log = structlog.get_logger("trader.upstream")
    log.error(
        "fetch %s failed",
        "https://login.questrade.com/oauth2/token?grant_type=refresh_token&refresh_token=SENTINEL-Q02",
        headers={"Authorization": "Bearer SENTINELbearer0202"},
        Refresh_Token="SENTINEL-RT02",
        body='{"access_token": "SENTINEL-AT02", "ok": true}',
        dsn="postgresql+psycopg://app:SENTINEL-PW02@db:5432/x",
        items=[{"api_key": "SENTINEL-AK02"}, ("x", {"password": "SENTINEL-PWL02"})],
        raw=b"password=SENTINEL-BYTES02",
    )
    logging.getLogger("finviz").error(
        "GET %s",
        "https://api.telegram.org/bot123456:AAAAsentinelTokenValue02/getMe",
        extra={"password": "SENTINEL-EXTRA02", "client_secret": "SENTINEL-CS02"},
    )
    try:
        raise ValueError('refused: {"refresh_token": "SENTINEL-EXC02"}')
    except ValueError:
        logging.getLogger("trader.qt").exception("token exchange failed")
    assert mirror.flush() == 3
    text_ = dumped(event_rows(db_factory))
    for sentinel in (
        "SENTINEL-Q02",
        "SENTINELbearer0202",
        "SENTINEL-RT02",
        "SENTINEL-AT02",
        "SENTINEL-PW02",
        "SENTINEL-AK02",
        "SENTINEL-PWL02",
        "SENTINEL-BYTES02",
        "AAAAsentinelTokenValue02",
        "SENTINEL-EXTRA02",
        "SENTINEL-CS02",
        "SENTINEL-EXC02",
    ):
        assert sentinel not in text_, sentinel


@pytest.mark.db
@pytest.mark.usefixtures("fresh_logging")
def test_03_deeply_nested_non_string_secrets_are_masked_like_the_log_line(
    db_factory: sessionmaker[Session],
    clock: FixedClock,
    mirrors: list[EventLogMirror],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Below DEPTH_MAX (4) the mirror falls back to redact_text(str(value)), which only masks quoted string
    values: a secret-named key holding bytes or a number is kept. The stdout line has no depth limit and
    masks it by key, so the mirror is weaker than "masked like the log line" (T14 Behaviour)."""
    mirror = make(mirrors, db_factory, clock)
    capsys.readouterr()
    structlog.get_logger("trader.deep").error(
        "deep.failed",
        a={"b": {"c": {"d": {"password": b"SENTINEL-DEEP-03", "secret": 987650321234}}}},
    )
    stdout = capsys.readouterr().out
    assert "SENTINEL-DEEP-03" not in stdout and "987650321234" not in stdout  # the log line masks both
    assert mirror.flush() == 1
    text_ = dumped(event_rows(db_factory))
    assert "SENTINEL-DEEP-03" not in text_
    assert "987650321234" not in text_


@pytest.mark.usefixtures("clean_logging")
def test_04_a_real_unreachable_database_drops_exactly_the_mirrored_lines_and_never_recurses(
    clock: FixedClock, mirrors: list[EventLogMirror], capsys: pytest.CaptureFixture[str]
) -> None:
    """The database is down for real (connection refused on localhost): psycopg and SQLAlchemy raise and
    may log inside the write. Nothing escapes, nothing is mirrored back (dropped stays exactly the three
    lines, no fourth from a library line fed back), no password is printed."""
    configure_logging("test")
    engine = create_engine(
        "postgresql+psycopg://app:SENTINEL-PW-04@127.0.0.1:1/trader_dev", connect_args={"connect_timeout": 2}
    )
    try:
        mirror = make(mirrors, sessionmaker(bind=engine), clock, flush_seconds=0.05)
        mirror.start()
        capsys.readouterr()
        for i in range(3):
            structlog.get_logger("trader.x").error(f"down.{i}")
        assert mirror.flush(timeout=10) == 0
        time.sleep(0.2)  # a few more writer cycles: anything fed back would be written (and dropped) now
        assert mirror.flush(timeout=10) == 0
        mirror.close()
        assert mirror.dropped == 3
        assert mirror.queued() == 0
        out = capsys.readouterr().out
        assert "SENTINEL-PW-04" not in out
        events = [json.loads(line)["event"] for line in out.splitlines() if line.startswith("{")]
        assert [e for e in events if e.startswith("down.")] == ["down.0", "down.1", "down.2"]
    finally:
        engine.dispose()


@pytest.mark.usefixtures("clean_logging")
def test_05_a_log_line_emitted_during_emit_does_not_recurse_into_the_mirror(
    clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    """ "Never recurses": a field whose str() logs an error (a library object, a lazy proxy) re-enters the
    mirror's handler from inside its own emit. Without a re-entrancy guard every level queues a row until
    RecursionError, so one log line becomes dozens of rows."""
    root = logging.getLogger()
    root.setLevel(logging.ERROR)

    class Loud:
        def __str__(self) -> str:
            logging.getLogger("trader.loud").error("str.called", extra={"obj": self})
            return "loud"

    mirror = make(mirrors, None, clock, queue_size=1000)
    logging.getLogger("trader.outer").error("outer.failed", extra={"obj": Loud()})
    assert mirror.queued() <= 2


@pytest.mark.db
def test_06_queue_full_drops_are_counted_and_reported_once_at_close_with_the_replay_run_id(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    with session_scope(db_factory) as s:
        replay_id = add_run(s, mode="replay", status="running")
    mirror = make(mirrors, db_factory, clock, queue_size=10, run_id=replay_id, process="replay")
    handler = mirror.handler
    assert handler is not None
    for i in range(25):
        handler.handle(
            logging.makeLogRecord(
                {"name": "trader.replay.x", "levelno": 40, "levelname": "ERROR", "msg": f"step.{i}"}
            )
        )
    assert mirror.queued() == 10 and mirror.dropped == 15
    mirror.close()
    mirror.close()  # idempotent: no second summary
    got = event_rows(db_factory)
    assert len(got) == 11
    assert {r.run_id for r in got} == {replay_id}
    assert {r.source for r in got} == {"log.replay"}
    assert got[-1].message == "log mirror dropped 15 lines" and got[-1].data == {"dropped": 15}
    assert [r.message for r in got[:10]] == [f"trader.replay.x: step.{i}" for i in range(10)]


@pytest.mark.db
async def test_07_mirror_rows_are_never_relayed_and_replay_rows_stay_off_the_system_page(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    from trader.api.routers.system import _errors  # the System page's error list

    relay, notifier = relay_for(db_factory, clock)
    await relay.pump()  # the cursors start here
    with session_scope(db_factory) as s:
        replay_id = add_run(s, mode="replay", status="running")
    live = make(mirrors, db_factory, clock, process="worker")
    replay = make(mirrors, db_factory, clock, process="replay", run_id=replay_id)
    logging.getLogger("trader.relay").critical("relay.crashed")
    logging.getLogger("trader.api").error("api.500")
    assert live.flush() == 2
    assert replay.flush() == 2  # both handlers see every root record
    with session_scope(db_factory) as s:  # the control: a real job failure alerts
        log_event(s, clock, "error", "job.nightly", "nightly failed for 2026-10-06", {"error": "x"})
    await relay.pump()
    await relay.pump()
    assert [msg.kind for msg in notifier.sent] == ["job_failure"]
    with db_factory() as s:
        shown = [(e.source, e.message) for e in _errors(s)]
    assert ("log.worker", "trader.relay: relay.crashed") in shown
    assert ("log.worker", "trader.api: api.500") in shown
    assert all(source != "log.replay" for source, _ in shown)


@pytest.mark.db
def test_08_many_threads_every_line_is_a_row_or_counted_as_dropped(
    db_factory: sessionmaker[Session], clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    threads_n, per_thread = 16, 250
    mirror = make(mirrors, db_factory, clock, queue_size=200, max_per_minute=10**6, flush_seconds=0.01)
    mirror.start()
    barrier = threading.Barrier(threads_n)
    errors: list[BaseException] = []

    def spam(n: int) -> None:
        try:
            barrier.wait(5)
            logger = logging.getLogger(f"trader.t{n}")
            for i in range(per_thread):
                logger.error("line %d", i)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    workers = [threading.Thread(target=spam, args=(n,)) for n in range(threads_n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(30)
    assert errors == []
    mirror.close()
    got = event_rows(db_factory)
    mirrored = [r for r in got if not r.message.startswith("log mirror dropped")]
    summaries = [r for r in got if r.message.startswith("log mirror dropped")]
    assert len({r.message for r in mirrored}) == len(mirrored)  # no line written twice
    assert len(mirrored) + mirror.dropped == threads_n * per_thread
    assert sum(r.data["dropped"] for r in summaries) == mirror.dropped


def test_09_close_with_a_hung_database_returns_promptly_and_removes_the_handler(
    clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    release, entered = threading.Event(), threading.Event()
    mirror = make(mirrors, blocking_factory(release, entered), clock, flush_seconds=0.05)
    mirror.start()
    try:
        logging.getLogger("trader.x").error("pending")
        assert entered.wait(5)
        logging.getLogger("trader.x").error("pending.2")
        started = time.perf_counter()
        mirror.close()
        assert time.perf_counter() - started < 3.5
        assert mirror.handler not in logging.getLogger().handlers
    finally:
        release.set()


def test_10_flush_and_close_honour_their_timeout_without_a_writer_thread(
    clock: FixedClock, mirrors: list[EventLogMirror]
) -> None:
    """`flush(timeout)` promises 0 "when the write did not finish in time". Without a running writer thread
    (never started, or called from a process that installed but did not start it) the inline path ignores
    the timeout once it has the lock: a hung database hangs flush() and close() (a process's shutdown)."""
    release, entered = threading.Event(), threading.Event()
    mirror = make(mirrors, blocking_factory(release, entered), clock)
    logging.getLogger("trader.x").error("pending")
    took: dict[str, float] = {}

    def call(name: str, fn: Callable[[], Any]) -> None:
        started = time.perf_counter()
        fn()
        took[name] = time.perf_counter() - started

    try:
        t = threading.Thread(target=call, args=("flush", lambda: mirror.flush(timeout=0.5)), daemon=True)
        t.start()
        t.join(4)
        assert "flush" in took and took["flush"] < 2.0, "flush(timeout=0.5) did not return within 2 s"
        logging.getLogger("trader.x").error("pending.2")
        t = threading.Thread(target=call, args=("close", mirror.close), daemon=True)
        t.start()
        t.join(6)
        assert "close" in took and took["close"] < 4.0, "close() did not return within 4 s"
    finally:
        release.set()


# ============================================================================================================
# P5-T15: retries
# ============================================================================================================


@pytest.mark.db
async def test_11_interrupt_in_the_second_attempt_releases_the_lock_and_alerts_once(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 6, 0, tzinfo=UTC))
    relay, notifier = relay_for(db_factory, clock)
    await relay.pump()
    calls: list[int] = []

    def body() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("FinViz down")
        raise KeyboardInterrupt

    slept: list[float] = []
    with pytest.raises(KeyboardInterrupt):
        run_job(db_factory, clock, "nightly", D, body, retry=THREE, sleep=slept.append)
    assert calls == [1, 1] and slept == [120.0]
    assert job_rows(db_factory) == ["failed", "failed"]
    assert levels(db_factory) == ["warning", "error"]
    assert advisory_locks(db_factory) == 0
    await relay.pump()
    assert [msg.kind for msg in notifier.sent] == ["job_failure"]
    # the next run is not refused as "already running"
    out = run_job(db_factory, clock, "nightly", D, lambda: {"ok": 1}, retry=THREE, sleep=lambda _: None)
    assert out.status == "succeeded"


@pytest.mark.db
async def test_12_async_cancel_inside_the_second_attempt_releases_the_lock(
    db_factory: sessionmaker[Session],
) -> None:
    clock = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    calls: list[int] = []
    inside = asyncio.Event()

    async def body() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Questrade 503")
        inside.set()
        await asyncio.Event().wait()  # until cancelled
        return {}

    async def fake_sleep(_: float) -> None:
        await asyncio.sleep(0)

    task = asyncio.create_task(
        run_job_async(db_factory, clock, "preopen", D, body, retry=THREE, sleep=fake_sleep)
    )
    await asyncio.wait_for(inside.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job_rows(db_factory) == ["failed", "failed"]
    assert levels(db_factory) == ["warning", "error"]
    assert advisory_locks(db_factory) == 0


@pytest.mark.db
def test_13_each_attempt_rechecks_already_succeeded(db_factory: sessionmaker[Session]) -> None:
    """A success recorded for the (job, session) while this run waits (an operator's manual record, a
    success another path recorded) is seen by the next attempt: the body is not run a second time."""
    clock = FixedClock(datetime(2026, 10, 6, 6, 0, tzinfo=UTC))
    calls: list[int] = []

    def body() -> dict[str, Any]:
        calls.append(1)
        raise RuntimeError("transient")

    def sleep(_: float) -> None:
        with session_scope(db_factory) as s:
            s.add(m.JobRun(job="nightly", session_date=D, started_at=clock.now(), status="succeeded"))

    out = run_job(db_factory, clock, "nightly", D, body, retry=THREE, sleep=sleep)
    assert out == JobOutcome("skipped", {"reason": "already succeeded"})
    assert calls == [1]
    assert levels(db_factory) == ["warning"]  # no alert: the session's job did succeed


@pytest.mark.db
async def test_14_waits_go_only_through_the_injected_sleep(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half-hour waits must cost nothing in a test: the runner never falls back to the real time.sleep or
    asyncio.sleep when a sleep is injected."""

    def no_real_sleep(*_: Any) -> None:
        raise AssertionError("a real sleep was called")

    monkeypatch.setattr(runner_module.time, "sleep", no_real_sleep)
    clock = FixedClock(datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    policy = RetryPolicy(attempts=5, first_delay_s=1800.0, backoff=2.0)
    started = time.perf_counter()
    slept: list[float] = []

    def broken() -> dict[str, Any]:
        raise RuntimeError("x")

    out = run_job(db_factory, clock, "postclose", D, broken, retry=policy, sleep=slept.append)
    assert out.status == "failed" and slept == [1800.0, 3600.0, 7200.0, 14400.0]

    aslept: list[float] = []

    async def fake(seconds: float) -> None:
        aslept.append(seconds)

    async def failing() -> dict[str, Any]:
        raise RuntimeError("y")

    out = await run_job_async(db_factory, clock, "premarket", D, failing, retry=policy, sleep=fake)
    assert out.status == "failed" and aslept == [1800.0, 3600.0, 7200.0, 14400.0]
    assert time.perf_counter() - started < 5.0
    assert levels(db_factory).count("error") == 2  # one alert per job


def test_15_scheduler_and_worker_events_never_pass_a_retry_policy() -> None:
    """Events keep the scheduler's own 30/60/120 s retries (`_failure_history` counts failed
    `event:<key>` rows); an in-process retry on top would double the attempts and the failed rows. Every
    run_job / run_job_async call outside the CLI day-level jobs passes no `retry`."""
    offenders: list[str] = []
    event_paths = [TRADER_PKG / "worker.py", *sorted((TRADER_PKG / "engine").rglob("*.py"))]
    for path in event_paths:
        rel = path.relative_to(TRADER_PKG).as_posix()
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in ("run_job", "run_job_async"):
                continue
            if any(kw.arg == "retry" for kw in node.keywords):
                offenders.append(f"{rel}:{node.lineno}")
    scheduler = (TRADER_PKG / "engine" / "scheduler.py").read_text()
    assert "RetryPolicy" not in scheduler
    assert offenders == []


# ============================================================================================================
# P5-T16: compose limits
# ============================================================================================================


@pytest.mark.parametrize("name", ["docker-compose.dev.yml", "docker-compose.prod.yml"])
def test_16_compose_has_every_limit_and_every_earlier_setting(name: str) -> None:
    compose = yaml.safe_load((DOCKER / name).read_text())
    assert list(compose["services"]) == ["trader"]
    svc = compose["services"]["trader"]
    # the P5-T16 limits
    assert str(svc["mem_limit"]).lower() in ("1g", "1024m")
    assert str(svc["memswap_limit"]).lower() == str(svc["mem_limit"]).lower()
    assert float(svc["cpus"]) == 2.0
    assert svc["pids_limit"] == 256
    assert svc["ulimits"]["nofile"] == {"soft": 4096, "hard": 8192}
    assert svc["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}
    # no competing limit block that Compose would merge or prefer
    assert "deploy" not in svc and "mem_reservation" not in svc and "oom_kill_disable" not in svc
    # every earlier (P4) setting
    assert svc["stop_grace_period"] == "120s"
    assert svc["init"] is True
    assert svc["read_only"] is True
    assert svc["restart"] == "unless-stopped"
    assert "ports" not in svc and svc.get("network_mode") is None and not svc.get("privileged", False)
    assert "proxy" in svc["networks"] and compose["networks"]["proxy"] == {"external": True}
    assert str(svc["env_file"]).startswith("${TRADER_ENV_FILE:-")
    assert any(str(t).startswith("/tmp:size=256m") for t in svc["tmpfs"])
    # TRADER_FORWARDED_ALLOW_IPS reaches the API from the env file: nothing in `environment` pins or blanks it
    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    value = env.get("TRADER_FORWARDED_ALLOW_IPS")
    assert value is None or str(value).startswith("${TRADER_FORWARDED_ALLOW_IPS")
    assert set(env) <= {"APP_ENV", "TRADER_FORWARDED_ALLOW_IPS"}, env
