"""DB-T10 gauntlet (Breaker): the worker wiring of the quote tap and the mark publisher, and the D2 proofs.

1. Mutants (throwaway, monkeypatched here only, never in production code) must be caught by the D2 day proof
   (`tests/integration/test_marks_live_unchanged.py`: trading rows, chat, Questrade call log) or by the two
   probes added here on the same simulated worker day: the event-loop iterations of every worker step (the
   tap adds no scheduling point) and the row/table locks other backends hold once a publisher pass returned
   (the publisher leaves nothing the trading path could wait on). The real composition is identical on both
   probes with the marks on and off.
2. SIGTERM mid-pass: the worker returns promptly, the publisher task is cancelled, `close` runs after
   `Worker.run` returned; a stuck publisher thread and process exit (subprocess).
3. Heartbeat: every part failing independently keeps the beat written (and /api/health's worker check ok);
   the heartbeat JSON stays small with 1000 symbols observed.
4. One shared client: the tap wraps the one LazyQuestrade used by the engines and the bot's market data.
5. D2 deploy diff: TRADER_D2_BASE=459e172 (what trader-dev runs) passes; a garbage or non-ancestor base fails.
6. `--once` never starts the publisher; replay and the other processes never build a tap or a publisher.

Parallel-safe: every simulated day runs in its own uniquely named database; no fixed ports or files.
"""

import ast
import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

import trader.bootstrap
import trader.runtime as rt
from tests.conftest import alembic_config
from tests.fakes_questrade import FakeQuestrade
from tests.integration import test_marks_live_unchanged as tl
from tests.integration import test_worker_day as wd
from tests.integration.test_decisions_day import trading_rows
from tests.test_runtime import World, make_env, use_fake_engine, world  # noqa: F401 (the fixture)
from tests.test_worker import _heartbeat
from trader.adapters.questrade.client import CallStats
from trader.adapters.questrade.models import CandleRequest, QtQuote
from trader.api import views
from trader.bootstrap import Core
from trader.db.session import make_engine, make_session_factory
from trader.market.clock import ET, FixedClock
from trader.market.data_service import MarketDataService
from trader.marks.publisher import MarkPublisher
from trader.marks.tap import QuoteTap
from trader.marks.types import PublishStep
from trader.worker import StepReport, Worker

pytestmark = pytest.mark.db

APP = Path(__file__).resolve().parents[2]
TRADER = APP / "trader"
SOAK_BASE = "459e172"  # the commit trader-dev runs (Phase 6 LIVE deploy, 09-28)
STOPWAITSECS = 60  # docker/supervisord.conf [program:worker]


# --- a simulated worker day with probes ---


@contextmanager
def fresh_factory(pg_url: str) -> Iterator[sessionmaker[Session]]:
    """A fresh, migrated database with a unique name in this worker's own container."""
    name = f"t10b_{uuid4().hex[:12]}"
    admin = create_engine(pg_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        url = make_url(pg_url).set(database=name).render_as_string(hide_password=False)
        command.upgrade(alembic_config(url), "head")
        engine = make_engine(url)
        try:
            yield make_session_factory(engine)
        finally:
            engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        admin.dispose()


FOREIGN_LOCKS = text(
    """
    SELECT count(*) FROM pg_locks l
    JOIN pg_class c ON c.oid = l.relation
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'trader' AND l.pid <> pg_backend_pid() AND l.granted
    """
)


@dataclass
class Summary:
    """Everything the D2 proof compares, plus the two probes."""

    rows: dict[str, Any]
    chat: Any
    replies: Any
    calls: Any
    iterations: list[int]  # event-loop iterations of each worker step (step + beat + relay pump)
    foreign_locks: list[int]  # trader-schema locks held by other backends right after each publisher pass
    batch_sizes: list[int]  # the size of every candles_many that reached the fake Questrade


def proof_differs(a: Summary, b: Summary) -> bool:
    """`test_marks_live_unchanged` test 1's comparison: trading rows, chat, replies, Questrade calls."""
    return (a.rows, a.chat, a.replies, a.calls) != (b.rows, b.chat, b.replies, b.calls)


async def probed_day(factory: sessionmaker[Session], mode: str) -> Summary:
    """`test_marks_live_unchanged.run_day` with the loop-iteration and lock probes around it."""
    loop = asyncio.get_running_loop()
    sel: Any = loop._selector  # type: ignore[attr-defined]
    real_select = sel.select
    count = [0]

    def select(timeout: float | None = None) -> Any:
        count[0] += 1
        return real_select(timeout)

    iterations: list[int] = []
    locks: list[int] = []
    real_at = wd.Driver.at
    real_run_once = MarkPublisher.run_once

    async def at(self: wd.Driver, when: datetime) -> StepReport:
        before = count[0]
        report = await real_at(self, when)
        iterations.append(count[0] - before)
        return report

    async def run_once(self: MarkPublisher) -> PublishStep:
        step = await real_run_once(self)
        with factory() as s:
            locks.append(int(s.execute(FOREIGN_LOCKS).scalar_one()))
        return step

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sel, "select", select)
        mp.setattr(wd.Driver, "at", at)
        mp.setattr(MarkPublisher, "run_once", run_once)
        day = await tl.run_day(factory, mode)
    calls, detail = day.calls
    return Summary(
        rows=trading_rows(factory),
        chat=day.chat,
        replies=day.replies,
        calls=(calls, detail),
        iterations=iterations,
        foreign_locks=locks,
        batch_sizes=[len(c[1]) for c in detail if c[0] in ("candles_many", "quotes")],
    )


_OFF: dict[str, Summary] = {}  # the "off" day, once per test process (loadfile keeps this module on one)


async def off_day(pg_url: str) -> Summary:
    if "off" not in _OFF:
        with fresh_factory(pg_url) as f:
            _OFF["off"] = await probed_day(f, "off")
    return _OFF["off"]


# --- 1. the real composition is identical on the proof AND both probes ---


async def test_the_real_composition_matches_off_on_the_proof_the_loop_iterations_and_the_locks(
    pg_url: str,
) -> None:
    off = await off_day(pg_url)
    with fresh_factory(pg_url) as f:
        on = await probed_day(f, "on")
    assert not proof_differs(on, off)
    # the tap adds no event-loop iteration to any worker step, across the whole day (9:35 batch included)
    assert len(on.iterations) == len(off.iterations) > 100
    assert on.iterations == off.iterations
    # a publisher pass leaves no lock behind that a trading transaction could wait on
    assert on.foreign_locks and set(on.foreign_locks) == {0}
    assert max(on.batch_sizes) > 1  # the opening-bar batch has several requests (reordering is observable)


# --- 1b. mutants ---


def _mutant_yield(mp: pytest.MonkeyPatch) -> None:
    real = QuoteTap.quotes

    async def quotes(self: QuoteTap, ids: Sequence[int]) -> list[QtQuote]:
        out = await real(self, ids)
        await asyncio.sleep(0)  # one scheduling point of its own
        return out

    mp.setattr(QuoteTap, "quotes", quotes)


def _mutant_delay(mp: pytest.MonkeyPatch) -> None:
    real = QuoteTap.quotes

    async def quotes(self: QuoteTap, ids: Sequence[int]) -> list[QtQuote]:
        await asyncio.sleep(0.003)  # a quote delayed by a few milliseconds
        return await real(self, ids)

    mp.setattr(QuoteTap, "quotes", quotes)


def _mutant_reorder(mp: pytest.MonkeyPatch) -> None:
    # QUOTEBAR: the 9:35 opening-bar batch is now one quotes request (several ids), no longer candles_many
    real = QuoteTap.quotes

    async def quotes(self: QuoteTap, ids: Sequence[int]) -> list[QtQuote]:
        return await real(self, list(reversed(list(ids))))

    mp.setattr(QuoteTap, "quotes", quotes)


def _mutant_lock(mp: pytest.MonkeyPatch) -> None:
    """The publisher leaves a lock on `orders` behind for a moment after each pass (released by a thread)."""
    real = MarkPublisher.run_once

    async def run_once(self: MarkPublisher) -> PublishStep:
        step = await real(self)
        taken = threading.Event()
        bind = self.deps.factory.kw["bind"]

        def hold() -> None:
            with bind.connect() as conn:
                conn.execute(text("LOCK TABLE trader.orders IN SHARE ROW EXCLUSIVE MODE"))
                taken.set()
                time.sleep(0.2)
                conn.rollback()

        thread = threading.Thread(target=hold, daemon=True)
        HOLDERS.append(thread)
        thread.start()
        assert taken.wait(5)
        return step

    mp.setattr(MarkPublisher, "run_once", run_once)


HOLDERS: list[threading.Thread] = []  # the lock mutant's threads, joined before its database is dropped


MUTANTS = {
    "yield": _mutant_yield,
    "delay": _mutant_delay,
    "reorder": _mutant_reorder,
    "lock": _mutant_lock,
}


@pytest.mark.parametrize("name", list(MUTANTS))
async def test_a_mutant_tap_or_publisher_is_caught(pg_url: str, name: str) -> None:
    off = await off_day(pg_url)
    with pytest.MonkeyPatch.context() as mp:
        MUTANTS[name](mp)
        with fresh_factory(pg_url) as f:
            try:
                on = await probed_day(f, "on")
            except AssertionError:
                return  # the day's own assertions caught it
            finally:
                for thread in HOLDERS:
                    thread.join(10)
                HOLDERS.clear()
    by_proof = proof_differs(on, off)
    by_loop = on.iterations != off.iterations
    by_locks = any(on.foreign_locks)
    caught = {"proof": by_proof, "loop": by_loop, "locks": by_locks}
    print(f"mutant {name}: caught by {caught}")  # the gauntlet log records which evidence caught it
    # every mutant is caught by the D2 evidence (the proof or a probe on the same day)
    assert by_proof or by_loop or by_locks, caught
    if name == "reorder":
        assert by_proof, caught  # the Questrade call log keeps every argument
    if name in ("yield", "delay"):
        assert by_loop, caught
    if name == "lock":
        assert by_locks, caught


# --- 2. SIGTERM mid-pass ---


async def test_sigterm_mid_pass_cancels_the_publisher_and_closes_it_after_the_worker_returns(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = Core(**{**world.core.__dict__, "env": make_env(telegram=False)})  # no bot: no long poll
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: bare)
    use_fake_engine(world, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []
    cancelled: list[bool] = []
    real_close, real_prun, real_wrun = MarkPublisher.close, MarkPublisher.run, Worker.run

    def stuck_pass(self: MarkPublisher, observed: Any) -> PublishStep:
        entered.set()
        release.wait(30)  # a pass stuck in its thread (e.g. a slow statement)
        return PublishStep(datetime.now(), None, 0, 0)

    def drain(self: QuoteTap) -> list[Any]:
        return [object()]  # always something to write, so a pass starts at once

    async def prun(self: MarkPublisher, stop: asyncio.Event) -> None:
        try:
            await real_prun(self, stop)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def wrun(self: Worker, stop: asyncio.Event, *, once: bool = False) -> None:
        async def kill_mid_pass() -> None:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            assert signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, None)  # the worker's handler
            order.append("sigterm")
            os.kill(os.getpid(), signal.SIGTERM)

        killer = asyncio.create_task(kill_mid_pass())
        try:
            await real_wrun(self, stop, once=once)
        finally:
            killer.cancel()
        order.append("run_end")

    def close(self: MarkPublisher) -> None:
        order.append("close")
        real_close(self)

    monkeypatch.setattr(MarkPublisher, "_pass", stuck_pass)
    monkeypatch.setattr(QuoteTap, "drain", drain)
    monkeypatch.setattr(MarkPublisher, "run", prun)
    monkeypatch.setattr(MarkPublisher, "close", close)
    monkeypatch.setattr(Worker, "run", wrun)
    try:
        t0 = time.monotonic()
        code = await asyncio.wait_for(rt.run_worker(), timeout=20)
        took = time.monotonic() - t0
    finally:
        release.set()
    assert code == 0
    assert order == ["sigterm", "run_end", "close"]
    assert cancelled == [True]  # the publisher task was cancelled, not waited for
    assert took < 10  # the stuck pass did not hold the worker's shutdown


STUCK_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys, threading, time
    from datetime import UTC, datetime
    from trader.marks.publisher import MarkPublisher, MarkPublisherDeps
    from trader.market.clock import FixedClock

    HOLD = float(sys.argv[1])

    class Tap:
        def drain(self):
            return [object()]

    def stuck(self, observed):
        started.set()
        time.sleep(HOLD) if HOLD >= 0 else threading.Event().wait()

    started = threading.Event()
    MarkPublisher._pass = stuck

    async def main():
        pub = MarkPublisher(MarkPublisherDeps(None, FixedClock(datetime(2026, 9, 28, tzinfo=UTC)), Tap(),
                                              lambda: 1))
        stop = asyncio.Event()
        task = asyncio.create_task(pub.run(stop))
        while not started.is_set():
            await asyncio.sleep(0.01)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pub.close()

    asyncio.run(main())
    print("returned", flush=True)
    """
)


def _exit_after_return(hold: float, timeout: float) -> float | None:
    """Seconds from `asyncio.run` returning to the process exiting; None if still alive at `timeout`."""
    proc = subprocess.Popen(  # noqa: S603 (fixed argv)
        [sys.executable, "-c", STUCK_SCRIPT, str(hold)],
        cwd=APP,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "returned", line
        t0 = time.monotonic()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_pass_bounded_by_its_statement_timeout_delays_exit_only_by_that_bound() -> None:
    took = _exit_after_return(2.0, timeout=20)
    assert took is not None and took < 5.0  # well inside stopwaitsecs


def test_a_pass_stuck_for_good_never_holds_process_exit() -> None:
    took = _exit_after_return(-1, timeout=8)
    assert took is not None and took < STOPWAITSECS / 2


# --- 3. heartbeat ---


class StatsQuestrade(FakeQuestrade):
    def __init__(self) -> None:
        super().__init__()
        self.rate_limit_remaining = {"market_data": 17, "account": 29}
        self.market, self.account = CallStats(), CallStats()

    @property
    def stats(self) -> dict[str, CallStats]:
        return {"market": self.market, "account": self.account}


class Ctx:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt, self.entered = qt, 0

    async def __aenter__(self) -> FakeQuestrade:
        self.entered += 1
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


def _boom() -> Any:
    raise RuntimeError("part unreadable")


@pytest.mark.parametrize("failing", ["rate_limit", "tap", "marks", "all"])
async def test_every_heartbeat_part_failing_on_its_own_keeps_the_beat_and_the_worker_healthy(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    qt = StatsQuestrade()
    monkeypatch.setattr(rt, "questrade_client", lambda core: Ctx(qt))
    use_fake_engine(world, monkeypatch)
    seen: dict[str, Any] = {}

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        publisher = self.deps.marks
        assert publisher is not None
        tap = publisher.deps.tap
        assert isinstance(tap, QuoteTap)
        await tap.quotes([101])  # opens the shared client: rate_limit and questrade appear
        lazy = tap._inner
        assert isinstance(lazy, rt.LazyQuestrade)
        if failing in ("rate_limit", "all"):
            monkeypatch.setattr(lazy, "rate_limit_remaining", _boom)
        if failing in ("tap", "all"):
            monkeypatch.setattr(tap, "health_detail", _boom)
        if failing in ("marks", "all"):
            monkeypatch.setattr(publisher, "health_detail", _boom)
        for i in range(3):
            world.clock.advance(timedelta(seconds=5))
            self._beat("session")
            hb = _heartbeat(world.factory)
            assert hb is not None and hb.beat_at == world.clock.now(), i
        seen["detail"] = dict(hb.detail) if hb is not None else {}
        stale = world.core.settings.load().worker_heartbeat_stale_seconds
        seen["health"] = views.worker_out(world.factory, world.clock.now(), stale)

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    detail = seen["detail"]
    keys = {"rate_limit": {"rate_limit"}, "tap": {"questrade", "candle_batches"}, "marks": {"marks"}}
    for part, names in keys.items():
        present = names <= set(detail)
        assert present is (failing not in (part, "all")), (part, sorted(detail))
    assert {"fills_today", "last_event"} <= set(detail)
    assert seen["health"].ok is True  # /api/health's worker check stays green


def test_the_heartbeat_detail_stays_small_with_1000_symbols_observed() -> None:
    clock = FixedClock(datetime(2026, 9, 29, 14, 0, tzinfo=ET))

    class Inner(StatsQuestrade):
        async def quotes(self, ids: Sequence[int]) -> list[QtQuote]:
            return [_quote(i) for i in ids]

    async def go() -> dict[str, Any]:
        inner = Inner()
        tap = QuoteTap(inner, clock)
        for _ in range(40):  # 1000 symbols, 40 observations each (over the per-symbol cap)
            await tap.quotes(list(range(1, 1001)))
        start = clock.now()
        for n in range(8):  # more batches than kept
            reqs = [CandleRequest(i, start, start + timedelta(minutes=5), "FiveMinutes") for i in range(500)]
            await tap.candles_many(reqs, deadline_s=45.0 + n)
        tapped, publisher = rt.live_marks(_core_stub(clock), inner, lambda: 1)
        assert publisher is not None
        publisher.close()
        return {
            "rate_limit": inner.rate_limit_remaining,
            **tap.health_detail(),
            "marks": {"written_at": clock.now().isoformat(), "symbols": 1000, "failing": False},
            "fills_today": 3,
            "last_event": "x" * 100,
        }

    detail = asyncio.run(go())
    body = json.dumps(detail, allow_nan=False)
    assert len(detail["candle_batches"]) == 5
    assert len(body) < 4096, len(body)


def _quote(i: int) -> QtQuote:
    return QtQuote(
        symbol_id=i,
        symbol=f"S{i}",
        bid=Decimal("10.00"),
        ask=Decimal("10.02"),
        last=Decimal("10.01"),
        last_regular=Decimal("10.01"),
        last_trade_time=None,
        volume=1,
        is_halted=False,
        delay=0,
        vwap=None,
    )


def _core_stub(clock: Any) -> Any:
    class Stub:
        factory: Any = None

    stub = Stub()
    stub.clock = clock  # type: ignore[attr-defined]
    return stub


# --- 4. one shared client ---


async def test_the_engines_and_the_bot_share_one_tap_over_one_lazy_client_and_one_token_chain(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt = StatsQuestrade()
    qt.quote_map.update({101: _quote(101), 102: _quote(102)})
    ctx = Ctx(qt)
    built: list[int] = []

    def questrade_client(core: Core) -> Ctx:
        built.append(1)
        return ctx

    monkeypatch.setattr(rt, "questrade_client", questrade_client)
    lazies: list[rt.LazyQuestrade] = []
    real_init = rt.LazyQuestrade.__init__

    def init(self: rt.LazyQuestrade, *a: Any, **k: Any) -> None:
        lazies.append(self)
        real_init(self, *a, **k)

    monkeypatch.setattr(rt.LazyQuestrade, "__init__", init)
    engine_clients: list[Any] = []

    async def fake_open_engine(core: Core, stack: Any, *, client: Any = None) -> Any:
        engine_clients.append(client)
        return world.runner

    monkeypatch.setattr(rt, "open_engine", fake_open_engine)
    services: list[MarketDataService] = []
    real_mds = MarketDataService.__init__

    def mds(self: MarketDataService, *a: Any, **k: Any) -> None:
        services.append(self)
        real_mds(self, *a, **k)

    monkeypatch.setattr(MarketDataService, "__init__", mds)

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        publisher = self.deps.marks
        assert publisher is not None
        tap = publisher.deps.tap
        await self.deps.engine_for(wd.et(10, 0).date())
        assert engine_clients == [tap]
        (data,) = services
        assert data._client is tap  # type: ignore[comparison-overlap]  # the bot's /positions and /quotes market data
        await data._client.quotes([101])
        await engine_clients[0].quotes([102])
        assert {o.qt_id for o in tap.drain()} >= {101, 102}

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    assert len(lazies) == 1 and len(built) == 1 and ctx.entered == 1  # one client, one token chain/bucket


# --- 5. the D2 deploy diff ---


def _d2(base: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "TRADER_D2_BASE"))}
    if base is not None:
        env["TRADER_D2_BASE"] = base
    return subprocess.run(  # noqa: S603 (fixed argv)
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-rs",
            "tests/live/test_d2_deploy_diff.py",
        ],
        cwd=APP,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _git_ok(*args: str) -> bool:
    try:
        return subprocess.run(["git", *args], cwd=APP, capture_output=True, timeout=30).returncode == 0  # noqa: S603, S607
    except (OSError, subprocess.SubprocessError):
        return False


def test_the_d2_deploy_diff_passes_from_what_trader_dev_runs() -> None:
    if not _git_ok("cat-file", "-e", f"{SOAK_BASE}^{{commit}}"):
        pytest.fail(f"{SOAK_BASE} (trader-dev's commit) is not in this checkout: the D2 check can't be made")
    done = _d2(SOAK_BASE)
    assert done.returncode == 0, done.stdout[-2000:]
    assert " passed" in done.stdout and "skipped" not in done.stdout, done.stdout[-2000:]


@pytest.mark.parametrize(
    "base",
    [
        "zzz-not-a-commit",
        " ",
        "4b825dc642cb6eb9a060e54bf8d69288fbee4904",  # the empty tree: an object, not a commit
        "HEAD",  # the build itself: nothing to compare, must not pass vacuously
    ],
)
def test_a_garbage_or_non_ancestor_d2_base_fails_and_never_skips(base: str) -> None:
    done = _d2(base)
    assert done.returncode == 1, (done.returncode, done.stdout[-2000:])  # 1 = tests failed (5 = none ran)
    assert "skipped" not in done.stdout, done.stdout[-2000:]


# --- 6. --once and the other processes ---


async def test_once_never_starts_the_publisher_nor_its_thread(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_fake_engine(world, monkeypatch)
    started: list[str] = []
    publishers: list[MarkPublisher] = []
    real_init = MarkPublisher.__init__

    def init(self: MarkPublisher, *a: Any, **k: Any) -> None:
        publishers.append(self)
        real_init(self, *a, **k)

    async def run(self: MarkPublisher, stop: asyncio.Event) -> None:
        started.append("run")

    async def run_once(self: MarkPublisher) -> PublishStep:
        started.append("run_once")
        raise AssertionError("no pass in --once")

    monkeypatch.setattr(MarkPublisher, "__init__", init)
    monkeypatch.setattr(MarkPublisher, "run", run)
    monkeypatch.setattr(MarkPublisher, "run_once", run_once)
    assert await rt.run_worker(once=True) == 0  # the REAL Worker.run: one step, one relay pump
    assert started == []
    for p in publishers:  # (built by the composition, never started) no executor thread was ever created
        assert p._executor._threads == set()
        assert p._executor._shutdown


def _calls_and_imports(path: Path) -> tuple[set[str], set[str]]:
    tree = ast.parse(path.read_text())
    calls: set[str] = set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            calls.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            mods.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
    return calls, mods


def test_only_the_worker_composition_builds_a_tap_or_a_publisher() -> None:
    builders: dict[str, list[str]] = {}
    for path in sorted(TRADER.rglob("*.py")):
        rel = path.relative_to(TRADER).as_posix()
        calls, mods = _calls_and_imports(path)
        hit = sorted(calls & {"QuoteTap", "MarkPublisher", "live_marks"})
        if hit:
            builders[rel] = hit
        if rel.startswith(("replay/", "jobs/", "api/", "decisions/", "reports/")):
            assert not any(m.startswith("trader.marks") for m in mods if m != "trader.marks.types"), rel
            assert "live_marks" not in calls, rel
    assert builders == {"runtime.py": ["MarkPublisher", "QuoteTap", "live_marks"]}
    # in runtime.py, live_marks is called from _run_worker only (no cron job path)
    tree = ast.parse((TRADER / "runtime.py").read_text())
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "live_marks"
    }
    assert callers == {"_run_worker"}
