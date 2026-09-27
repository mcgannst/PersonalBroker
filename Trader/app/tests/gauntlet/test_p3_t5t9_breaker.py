"""P3-B2: gauntlet breaker tests for P3-T5 (Telegram API client and TelegramNotifier) and P3-T9 (worker).

respx, fakes and the testcontainers database only: never real Telegram or Questrade. Time is a FixedClock
moved by hand or by the worker tests' `VirtualTime` fake sleep. The one real wait is a bounded poll (at most
10 s) for PostgreSQL to drop a terminated backend's advisory lock.
"""

import asyncio
import logging
import os
import re
import signal
import threading
import time
import traceback
from collections.abc import Awaitable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import respx
import telegram
from pydantic import SecretStr
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

from tests.fakes_telegram import FakeTelegramApi
from tests.test_worker import (
    CAL,
    EARLY,
    SAT,
    TUE,
    WED,
    FakeEngine,
    Harness,
    VirtualTime,
    _events,
    _heartbeat,
    et,
)
from trader.adapters.telegram.api import PtbTelegramApi
from trader.adapters.telegram.types import TelegramApiError
from trader.db.models import EventLog, JobRun, Notification
from trader.engine.scheduler import FireDeps, FireResult, day_plan, due_events, fire_event, fired_keys
from trader.market.clock import FixedClock, et_date
from trader.notify.notifier import TELEGRAM_LIMIT, TelegramNotifier, split_text
from trader.notify.types import Buttons, OutboundMessage
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSip
from trader.strategies.spy_overlay import SpyOverlay
from trader.worker import (
    FAILED_STEPS_CRITICAL,
    Worker,
    WorkerDeps,
    acquire_single_instance,
    release_single_instance,
)

# A made-up token shaped like a real one (never a real secret in a fixture).
TOKEN = "987654321:FAKE-breaker-token-abc"
SECRET = TOKEN.split(":")[1]
BASE = f"https://api.telegram.org/bot{TOKEN}"
CHAT = 515151
NOW = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)
SETTINGS = RuntimeSettings()


class FakeSleep:
    """Records waits and moves the clock forward, as real time would."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


def _notifier(factory: Any, api: Any) -> tuple[TelegramNotifier, FakeSleep]:
    clock = FixedClock(NOW)
    sleep = FakeSleep(clock)
    return TelegramNotifier(api, CHAT, cast(sessionmaker[Session], factory), clock, sleep=sleep), sleep


def _rows(factory: sessionmaker[Session]) -> list[Notification]:
    with factory() as s:
        return list(s.scalars(select(Notification).order_by(Notification.id)))


def _db_text(factory: sessionmaker[Session]) -> str:
    """Everything the notifier and the event log stored, as one string."""
    with factory() as s:
        notes = [(n.error, n.text) for n in s.scalars(select(Notification))]
        events = [(e.source, e.message, e.data) for e in s.scalars(select(EventLog))]
    return repr(notes) + repr(events)


def _telegram_error(code: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


class _RaisingBot:
    """A stand-in for telegram.Bot whose send_message raises a PTB exception."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def send_message(self, **_: Any) -> Any:
        raise self.exc

    async def shutdown(self) -> None:
        return None


async def _run(w: Worker, stop: asyncio.Event, vt: VirtualTime, **kw: Any) -> None:
    try:
        await asyncio.wait_for(w.run(stop, **kw), timeout=30)  # a real-time guard against a hung loop only
    finally:
        await vt.aclose()


def _with(h: Harness, **overrides: Any) -> WorkerDeps:
    return WorkerDeps(**{**vars(h.deps()), **overrides})


# =========================================================================================================
# P3-T5: Telegram API client and Notifier
# =========================================================================================================

LEAK_CASES = ["401", "404_url_in_description", "404_html_body", "connect_error", "invalid_token"]


@pytest.mark.db
@pytest.mark.parametrize("case", LEAK_CASES)
async def test_t5_token_never_leaks_through_any_error_path(
    case: str, db_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """Review Focus 3: whatever fails (401, a 404 that echoes the token URL, a transport error, PTB's
    InvalidToken), the token appears in no exception text, traceback, log line (root at DEBUG) or DB row."""
    caplog.set_level(logging.DEBUG)  # a DEBUG deployment: the client must still keep PTB and httpx quiet
    with respx.mock(assert_all_called=False) as router, capture_logs() as logs:
        route = router.post(f"{BASE}/sendMessage")
        if case == "401":
            route.mock(return_value=_telegram_error(401, "Unauthorized"))
        elif case == "404_url_in_description":
            route.mock(return_value=_telegram_error(404, f"Not Found: POST {BASE}/sendMessage"))
        elif case == "404_html_body":
            route.mock(return_value=httpx.Response(404, text=f"<html>No route {BASE}/sendMessage</html>"))
        elif case == "connect_error":
            route.mock(side_effect=httpx.ConnectError(f"[Errno 61] Connection refused: {BASE}/sendMessage"))
        if case == "invalid_token":
            bot = _RaisingBot(telegram.error.InvalidToken(f"Invalid token {TOKEN}"))
            api = PtbTelegramApi(SecretStr(TOKEN), bot=cast(telegram.Bot, bot))
        else:
            api = PtbTelegramApi(SecretStr(TOKEN))

        with pytest.raises(TelegramApiError) as caught:
            await api.send_message(CHAT, "x")
        notifier, _ = _notifier(db_factory, api)
        await notifier.send(OutboundMessage(kind="alert", text="keyed", dedupe_key=f"leak:{case}"))
        await notifier.send(OutboundMessage(kind="alert", text="plain"))

    err = caught.value
    if case in ("401", "invalid_token"):
        assert err.status == 401
    surfaces = {
        "str": str(err),
        "repr": repr(err),
        "description": err.description,
        "traceback": "".join(traceback.format_exception(err)),
        "stdlib logs": caplog.text,
        "structlog": repr(logs),
        "database": _db_text(db_factory),
    }
    leaked = [name for name, s in surfaces.items() if TOKEN in s or SECRET in s]
    assert leaked == [], f"the bot token leaked through: {leaked}"
    assert [r.status for r in _rows(db_factory)] == ["failed", "failed"]


@pytest.mark.db
async def test_t5_429_beyond_30s_and_409_conflict(db_factory: sessionmaker[Session]) -> None:
    """A 429 asking for 45 s waits at most 30 s, retries once and then fails cleanly (no third call, no
    longer wait: trading must not stall); a 409 from a second getUpdates consumer is a 409 (not retried,
    no retry_after) and leaves the client usable."""
    with respx.mock(assert_all_called=False) as router:
        send = router.post(f"{BASE}/sendMessage").mock(
            return_value=_telegram_error(429, "Too Many Requests: retry after 45", retry_after=45)
        )
        updates = router.post(f"{BASE}/getUpdates").mock(
            side_effect=[
                _telegram_error(
                    409,
                    "Conflict: terminated by other getUpdates request; "
                    "make sure that only one bot instance is running",
                ),
                httpx.Response(200, json={"ok": True, "result": []}),
            ]
        )
        api = PtbTelegramApi(SecretStr(TOKEN))
        notifier, sleep = _notifier(db_factory, api)

        await notifier.send(OutboundMessage(kind="alert", text="flood", dedupe_key="flood:1"))

        assert send.call_count == 2
        assert sleep.waits == [30.0]
        (row,) = _rows(db_factory)
        assert row.status == "failed"
        assert row.error is not None and row.error.startswith("429 Too Many Requests")

        with pytest.raises(TelegramApiError) as conflict:
            await api.get_updates(offset=None, timeout=0)
        assert (conflict.value.status, conflict.value.retry_after) == (409, None)
        assert "only one bot instance" in conflict.value.description
        assert updates.call_count == 1  # the client itself never retries a conflict
        assert await api.get_updates(offset=None, timeout=0) == []  # and survives it


SPLIT_CASES = {
    # hard cut at 4096 lands inside "&amp;": Telegram answers 400 "can't parse entities" for both parts
    "entity_at_hard_cut": "a" * 4093 + "&amp;" + "b" * 100,
    # hard cut inside "<b>bold</b>": an unclosed tag in part one, a stray close tag in part two
    "tag_at_hard_cut": "a" * 4090 + "<b>bold</b>" + "b" * 100,
    # multibyte text with a numeric entity (an emoji) straddling the cut
    "multibyte_entity": "é" * 4094 + "&#128512;" + "ü" * 50,
    # a message of exactly the limit plus a trailing line break: the last part (with the buttons) is empty
    "trailing_newline_at_limit": "x" * 4096 + "\n",
}


@pytest.mark.parametrize("case", list(SPLIT_CASES))
def test_t5_split_text_parts_are_all_sendable(case: str) -> None:
    """Every part must be something Telegram accepts with parse_mode=HTML: non-empty, within the limit,
    no HTML entity or tag cut in half, tags balanced. Otherwise the part fails with a 400 and (for the
    last part) the buttons are lost."""
    text = SPLIT_CASES[case]
    parts = split_text(text, TELEGRAM_LIMIT)
    problems: list[str] = []
    for i, part in enumerate(parts):
        if not part.strip():
            problems.append(f"part {i} is empty")
        if len(part) > TELEGRAM_LIMIT:
            problems.append(f"part {i} is {len(part)} characters")
        if re.search(r"&#?\w*$", part):
            problems.append(f"part {i} ends inside an HTML entity: {part[-6:]!r}")
        if re.search(r"<[^>]*$", part):
            problems.append(f"part {i} ends inside a tag: {part[-6:]!r}")
        if part.count("<b>") != part.count("</b>"):
            problems.append(f"part {i} has unbalanced <b> tags")
    assert problems == []


@pytest.mark.db
async def test_t5_same_dedupe_key_from_concurrent_threads_and_tasks_is_sent_once(
    db_factory: sessionmaker[Session],
) -> None:
    """Review Focus 3 (no message sent twice): eight processes' worth of notifiers (threads, each with its
    own event loop) racing on one key, and five concurrent tasks on two notifiers, reach the API once each."""
    api = FakeTelegramApi()
    barrier = threading.Barrier(8)
    crashed: list[BaseException] = []

    def one_process() -> None:
        try:
            notifier, _ = _notifier(db_factory, api)
            barrier.wait(timeout=10)
            asyncio.run(notifier.send(OutboundMessage(kind="fill", text="BOUGHT", dedupe_key="fill:42")))
        except BaseException as exc:  # surfaced below
            crashed.append(exc)

    threads = [threading.Thread(target=one_process) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert crashed == []

    a, _ = _notifier(db_factory, api)
    b, _ = _notifier(db_factory, api)
    await asyncio.gather(
        *(n.send(OutboundMessage(kind="fill", text="SOLD", dedupe_key="fill:43")) for n in (a, b, a, b, a))
    )

    assert sorted(c["text"] for c in api.calls_of("send_message")) == ["BOUGHT", "SOLD"]
    assert sorted((r.dedupe_key, r.status) for r in _rows(db_factory)) == [
        ("fill:42", "sent"),
        ("fill:43", "sent"),
    ]


class _FlakyFactory:
    """A session factory the test can take down and bring back."""

    def __init__(self, inner: sessionmaker[Session]) -> None:
        self.inner = inner
        self.down = False

    def __call__(self) -> Session:
        if self.down:
            raise ConnectionError("database is down")
        return self.inner()


class _DbDiesDuringSend(FakeTelegramApi):
    """The database goes down while the message is on the wire (after the claim, before the record)."""

    def __init__(self, factory: _FlakyFactory) -> None:
        super().__init__()
        self.factory = factory
        self.kill_db = True

    async def send_message(self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False) -> int:
        try:
            return await super().send_message(chat_id, text, buttons, silent)
        finally:
            if self.kill_db:
                self.factory.down = True


@pytest.mark.db
async def test_t5_db_down_with_and_without_a_key(db_factory: sessionmaker[Session]) -> None:
    """The DB failing after the claim (success or failure not recordable) never raises and never doubles a
    keyed message once the DB is back; with the DB fully down a keyed message is held back and an unkeyed
    one still goes out."""
    flaky = _FlakyFactory(db_factory)
    api = _DbDiesDuringSend(flaky)
    notifier, _ = _notifier(flaky, api)

    await notifier.send(OutboundMessage(kind="fill", text="k1", dedupe_key="fill:1"))  # sent, not recorded
    flaky.down = False
    await notifier.send(OutboundMessage(kind="fill", text="k1 again", dedupe_key="fill:1"))  # DB back

    api.fail("send_message", TelegramApiError(400, "Bad Request: can't parse entities"))
    await notifier.send(OutboundMessage(kind="alert", text="k2", dedupe_key="alert:2"))  # failure unrecorded
    flaky.down = False

    api.kill_db = False
    flaky.down = True  # the database is down for the whole send
    await notifier.send(OutboundMessage(kind="alert", text="no key"))
    await notifier.send(OutboundMessage(kind="alert", text="k3", dedupe_key="alert:3"))
    flaky.down = False

    assert [c["text"] for c in api.calls_of("send_message")] == ["k1", "k2", "no key"]
    assert {r.dedupe_key for r in _rows(db_factory)} == {"fill:1", "alert:2"}


# =========================================================================================================
# P3-T9: Worker process
# =========================================================================================================


@pytest.mark.db
async def test_t9_second_worker_refused_crashed_holder_does_not_block_and_once_never_polls(
    db_factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    """Review Focus 4: a second worker exits 2 while the first holds the lock; when the first dies without
    unlocking (kill -9: its backend is terminated) a new worker gets the lock, not a lock that blocks
    forever; and a --once run never calls getUpdates (no 409 for the polling worker)."""
    first = acquire_single_instance(migrated_engine)
    assert first is not None
    pid = first.execute(text("SELECT pg_backend_pid()")).scalar_one()
    clock = FixedClock(et(TUE, 10, 0))
    refused = Harness(db_factory, clock)
    with pytest.raises(SystemExit) as exit_info:
        await Worker(refused.deps()).run(asyncio.Event(), once=True)
    assert exit_info.value.code == 2 and refused.relay_calls == [] and refused.engines == []

    with migrated_engine.connect() as admin:  # the first worker is killed: no pg_advisory_unlock
        admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        admin.commit()
    first.invalidate()
    first.close()
    second = acquire_single_instance(migrated_engine)
    deadline = time.monotonic() + 10
    while second is None and time.monotonic() < deadline:
        time.sleep(0.05)
        second = acquire_single_instance(migrated_engine)
    assert second is not None, "a crashed worker's lock still blocks a new worker"
    release_single_instance(second)

    api = FakeTelegramApi()

    async def bot(stop: asyncio.Event) -> None:
        while not stop.is_set():
            await api.get_updates(offset=None, timeout=30)

    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep, bot=bot)
    before = signal.getsignal(signal.SIGTERM)
    await _run(Worker(h.deps()), asyncio.Event(), vt, once=True)
    assert api.calls_of("get_updates") == []
    assert len(h.relay_calls) == 1 and len(h.engines[0].polls) == 1
    assert signal.getsignal(signal.SIGTERM) == before
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.phase == "stopped"


@pytest.mark.db
async def test_t9_sigterm_mid_step_finishes_that_step_then_stops_cleanly(
    db_factory: sessionmaker[Session], migrated_engine: Engine
) -> None:
    """A real SIGTERM arriving while poll_quotes runs: that step still ticks and relays, no further step
    starts, the heartbeat says stopped, the lock is released and the signal disposition is restored."""
    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    stop_seen_mid_step: list[bool] = []

    class SigtermEngine(FakeEngine):
        async def poll_quotes(self) -> list[Any]:
            result = await super().poll_quotes()
            if len(self.polls) == 3:
                os.kill(os.getpid(), signal.SIGTERM)
                for _ in range(500):
                    if stop.is_set():
                        break
                    await asyncio.sleep(0)
                stop_seen_mid_step.append(stop.is_set())
            return result

    async def engine_for(d: date) -> FakeEngine:
        eng = SigtermEngine(d, clock)
        h.engines.append(eng)
        return eng

    before = signal.getsignal(signal.SIGTERM)
    await _run(Worker(_with(h, engine_for=engine_for)), stop, vt)

    eng = h.engines[0]
    assert stop_seen_mid_step == [True]
    assert (len(eng.polls), len(eng.ticks), len(h.relay_calls)) == (3, 3, 3)
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.phase == "stopped"
    again = acquire_single_instance(migrated_engine)
    assert again is not None
    release_single_instance(again)
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.db
async def test_t9_heartbeat_stays_fresh_during_a_long_step(db_factory: sessionmaker[Session]) -> None:
    """A slow step (orb_open taking 150 s: Questrade retries, many symbols) must not let the heartbeat go
    older than worker.heartbeat_stale_seconds, or the pre-open/check-in jobs report a dead worker while
    it is busy trading."""
    clock = FixedClock(et(TUE, 9, 35, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    ages: list[float] = []

    async def slow_fire(key: str, d: date) -> FireResult:
        h.fire_calls.append((key, d, clock.now()))
        for _ in range(10):
            await vt.sleep(15)
            hb = _heartbeat(db_factory)
            assert hb is not None
            ages.append((clock.now() - hb.beat_at).total_seconds())
        h.settled.setdefault(d, set()).add(key)
        stop.set()
        return FireResult(key, d, "fired")

    await _run(Worker(_with(h, fire=slow_fire)), stop, vt)
    assert [k for k, _, _ in h.fire_calls] == ["orb_open"]
    stale = h.settings.worker_heartbeat_stale_seconds
    assert max(ages) <= stale, f"heartbeat was {max(ages):.0f}s old mid-step (stale after {stale}s)"


@pytest.mark.db
async def test_t9_persistent_step_failure_gives_one_critical_and_no_alert_flood(
    db_factory: sessionmaker[Session],
) -> None:
    """Two minutes of a quote feed that fails on every 2 s step: exactly one critical event, and not one
    alert-level event per step (the relay turns every worker error/critical event into a Telegram alert,
    so 60 events would be 60 phone alerts in two minutes)."""
    clock = FixedClock(et(TUE, 10, 0))
    h = Harness(db_factory, clock)

    async def engine_for(d: date) -> FakeEngine:
        eng = await Harness.engine_for(h, d)
        eng.poll_error = RuntimeError("Questrade 503")
        return eng

    w = Worker(_with(h, engine_for=engine_for))
    for _ in range(60):
        await w.step()
        clock.advance(timedelta(seconds=2))
    levels = [e.level for e in _events(db_factory)]
    assert levels.count("critical") == 1
    alerts = [lv for lv in levels if lv in ("error", "critical")]
    assert len(alerts) <= FAILED_STEPS_CRITICAL + 1, (
        f"{len(alerts)} alert-level worker events in two minutes; each becomes a Telegram alert"
    )


@pytest.mark.db
async def test_t9_run_survives_the_settings_read_failing(db_factory: sessionmaker[Session]) -> None:
    """The loop never dies on a database error: runtime settings are read from the DB (T12), and a DB
    blip while `run` reads them between steps (heartbeat due, sleep interval) must not kill the worker."""
    clock = FixedClock(et(SAT, 11, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    stop = asyncio.Event()
    outage = {"on": False}

    def settings() -> RuntimeSettings:
        if outage["on"]:
            raise ConnectionError("database is down")
        return h.settings

    def on_relay() -> None:
        n = len(h.relay_calls)
        outage["on"] = 3 <= n < 6
        if n >= 10:
            stop.set()

    h.on_relay = on_relay
    try:
        await _run(Worker(_with(h, settings=settings)), stop, vt)
    except ConnectionError as exc:
        pytest.fail(f"the worker loop died on a database error while reading settings: {exc!r}")
    assert len(h.relay_calls) >= 10


@pytest.mark.db
async def test_t9_restart_mid_session_does_not_refire_settled_events(
    db_factory: sessionmaker[Session],
) -> None:
    """Review Focus 1 and 4 with the real T3 fire_event and fired_keys on the DB: a worker restarted after
    orb_open fired doesn't run it again; a worker that was down at 09:35 records orb_open missed once, and
    restarting it again neither re-records nor re-alerts it."""
    strategies = [OrbSip(), SpyOverlay()]
    clock = FixedClock(et(TUE, 9, 35, 6))
    runs: list[tuple[str, date]] = []

    class Runner:
        async def run_event(self, event_key: str, session_date: date) -> Any:
            runs.append((event_key, session_date))
            return None

    async def build_runner() -> Runner:
        return Runner()

    fdeps = FireDeps(
        factory=db_factory,
        clock=clock,
        calendar=CAL,
        settings=lambda: SETTINGS,
        plan=lambda d: day_plan(strategies, CAL, d, SETTINGS),
        runner=build_runner,
    )

    def fire(k: str, d: date) -> Awaitable[FireResult]:
        return fire_event(fdeps, k, d)

    def new_worker() -> Worker:
        h = Harness(db_factory, clock)
        return Worker(_with(h, plan=fdeps.plan, fire=fire, fired=lambda d: fired_keys(db_factory, d)))

    await new_worker().step()  # the first worker fires orb_open, then crashes
    assert runs == [("orb_open", TUE)]
    clock.set(et(TUE, 10, 0))
    restarted = new_worker()
    for _ in range(5):
        await restarted.step()
        clock.advance(timedelta(seconds=2))
    assert runs == [("orb_open", TUE)]

    clock.set(et(WED, 9, 40))  # down at 09:35 on Wednesday, started 295 s late
    for _ in range(3):
        again = new_worker()
        for _ in range(3):
            await again.step()
            clock.advance(timedelta(seconds=2))
        clock.advance(timedelta(seconds=30))
    assert runs == [("orb_open", TUE)]
    with db_factory() as s:
        wed = s.execute(
            select(JobRun.status, JobRun.error).where(
                JobRun.session_date == WED, JobRun.job == "event:orb_open"
            )
        ).all()
        alerts = s.scalars(
            select(EventLog).where(EventLog.source == "job.event:orb_open", EventLog.level == "error")
        ).all()
    assert len(wed) == 1 and wed[0][0] == "failed" and (wed[0][1] or "").startswith("missed:")
    assert len(alerts) == 1


AGREEMENT_CASES = [
    pytest.param(et(EARLY, 12, 55), {"orb_open"}, id="early_close_before_the_close"),
    pytest.param(et(EARLY, 13, 5), set(), id="early_close_late_restart_after_the_close"),
    pytest.param(et(date(2026, 11, 2), 9, 35, 5), set(), id="dst_fall_back_first_session"),
    pytest.param(et(date(2026, 11, 2), 9, 35, 4), set(), id="dst_fall_back_one_second_early"),
    pytest.param(et(date(2026, 3, 9), 9, 35, 5), set(), id="dst_spring_forward_first_session"),
    pytest.param(et(TUE, 15, 58), {"orb_open", "entry_cancel"}, id="late_restart_before_the_close"),
    pytest.param(et(TUE, 16, 30), {"orb_open"}, id="late_restart_after_the_close"),
    pytest.param(et(TUE, 9, 29, 30), set(), id="pre_open_lead"),
]


@pytest.mark.db
@pytest.mark.parametrize(("now", "fired"), AGREEMENT_CASES)
async def test_t9_worker_due_selection_agrees_with_scheduler_due_events(
    now: datetime, fired: set[str], db_factory: sessionmaker[Session]
) -> None:
    """The worker re-implements the due-event filter; it must offer `fire` exactly the keys T3's
    `due_events` returns (same order) on an early-close day, across both DST changes and on late restarts."""
    strategies = [OrbSip(), SpyOverlay()]
    day = et_date(now)
    plan = day_plan(strategies, CAL, day, SETTINGS)
    expected = [(e.key, day) for e in due_events(plan, now, fired)]
    offered: list[tuple[str, date]] = []

    async def spy_fire(k: str, d: date) -> FireResult:
        offered.append((k, d))
        return FireResult(k, d, "skipped")

    def plan_for(d: date) -> Any:
        return day_plan(strategies, CAL, d, SETTINGS)

    h = Harness(db_factory, FixedClock(now))
    await Worker(_with(h, plan=plan_for, fire=spy_fire, fired=lambda d: set(fired))).step()
    assert offered == expected
