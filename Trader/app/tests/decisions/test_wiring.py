"""P6-T11 acceptance tests 1, 3 and 7 (and the recorder-hook half of 2): the decisions loop, the worker task,
the post-close final pass and the runtime composition.

1. `DecisionsLoop` (fake clock, fake recorder): only session days, only 07:50 ET to the close + 30 min, at the
   configured cadence; skips while an `event:` row of today is `running`; starts no pass from 09:34:00 to
   09:38:00 ET (EDT and EST); one warning per failure streak and one info on recovery; never raises; its
   database work runs off the event loop (a gate that blocks 3 s in its thread does not delay a concurrent
   coroutine, nor the worker's step loop).
3. Post-close: the final pass runs after the archive and before the summary; a raising recorder leaves the
   archive and the summary unchanged and the detail says `error`; `prune` runs.
7. `run_worker` passes a `DecisionsLoop` to the worker.
"""

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.test_runtime import world  # noqa: F401 (the fixture of test 7)
from trader.db import models as m
from trader.decisions.loop import DecisionsLoop, FinalPass, final_pass, in_quiet_window
from trader.decisions.prune import PruneResult
from trader.decisions.types import RecorderDeps, RecordResult
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

ET = ZoneInfo("America/New_York")
CAL = SessionCalendar()
TUE = date(2026, 10, 6)  # EDT
EST_DAY = date(2026, 12, 1)  # Tue, EST (after the 2026-11-01 change)
EARLY = date(2026, 11, 27)  # 13:00 ET close
SAT = date(2026, 10, 3)
RUN = 7


def et(d: date, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, mi, s, tzinfo=ET).astimezone(UTC)


@dataclass
class FakeRecorder:
    calls: list[tuple[int, date, bool, datetime]] = field(default_factory=list)
    fail: int = 0  # raise on the next `fail` calls
    clock: FixedClock | None = None

    async def __call__(
        self,
        deps: RecorderDeps,
        run_id: int,
        session_date: date,
        *,
        final: bool = False,
        rebuild: bool = False,
    ) -> RecordResult:
        assert self.clock is not None
        self.calls.append((run_id, session_date, final, self.clock.now()))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("database gone: password=hunter2")
        return RecordResult(run_id, session_date, None, {"scan": 3, "day": 1}, final)


@dataclass
class Events:
    rows: list[tuple[str, str, dict[str, Any], int | None]] = field(default_factory=list)

    def __call__(self, level: str, message: str, data: dict[str, Any], run_id: int | None) -> None:
        self.rows.append((level, message, data, run_id))


class NoDb:
    """A session factory that must not be used (the gate reaches the database only for the `running`
    check, which these tests replace unless they test it)."""

    kw: dict[str, Any] = {}

    def __call__(self) -> Any:
        raise AssertionError("no database in this test")


def make_loop(
    clock: FixedClock,
    *,
    settings: RuntimeSettings | Callable[[], RuntimeSettings] | None = None,
    factory: Any = None,
    run_id: Callable[[], int | None] = lambda: RUN,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[DecisionsLoop, FakeRecorder, Events]:
    rec = FakeRecorder(clock=clock)
    events = Events()
    load: Callable[[], RuntimeSettings]
    if settings is None:
        load = RuntimeSettings
    elif isinstance(settings, RuntimeSettings):
        value = settings
        load = lambda: value  # noqa: E731
    else:
        load = settings
    deps = RecorderDeps(factory if factory is not None else NoDb(), clock, CAL, load, None)  # type: ignore[arg-type]
    kw: dict[str, Any] = {"record": rec, "event": events}
    if sleep is not None:
        kw["sleep"] = sleep
    return DecisionsLoop(deps, run_id, **kw), rec, events


@pytest.fixture
def no_running(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.decisions.loop as loop_mod

    monkeypatch.setattr(loop_mod, "event_running", lambda factory, day: False)


# --- 1. the window, the quiet minutes, the cadence ----------------------------------------------------------


@pytest.mark.usefixtures("no_running")
@pytest.mark.parametrize(
    ("when", "records"),
    [
        (et(TUE, 7, 49, 59), False),
        (et(TUE, 7, 50), True),
        (et(TUE, 8, 5), True),
        (et(TUE, 12, 0), True),
        (et(TUE, 16, 29, 59), True),
        (et(TUE, 16, 30), False),  # the close + 30 min
        (et(TUE, 20, 0), False),
        (et(EARLY, 13, 29, 59), True),  # an early close: 13:00 + 30 min
        (et(EARLY, 13, 30), False),
        (et(SAT, 10, 0), False),  # not a session
        (et(date(2026, 11, 26), 10, 0), False),  # Thanksgiving
    ],
)
async def test_the_loop_records_only_in_the_session_window(when: datetime, records: bool) -> None:
    clock = FixedClock(when)
    loop, rec, _ = make_loop(clock)
    step = await loop.run_once()
    assert (len(rec.calls) == 1) is records, step
    if records:
        assert rec.calls == [(RUN, when.astimezone(ET).date(), False, when)]
        assert step.skipped is None and step.result is not None
    else:
        assert step.skipped in ("outside_window", "not_session")


@pytest.mark.usefixtures("no_running")
@pytest.mark.parametrize("day", [TUE, EST_DAY])  # EDT and EST
@pytest.mark.parametrize(
    ("hms", "records"),
    [
        ((9, 33, 59), True),
        ((9, 34, 0), False),
        ((9, 35, 5), False),
        ((9, 36, 0), False),
        ((9, 37, 59), False),
        ((9, 38, 0), True),
    ],
)
async def test_no_pass_starts_from_0934_to_0938_et(
    day: date, hms: tuple[int, int, int], records: bool
) -> None:
    when = et(day, *hms)
    loop, rec, _ = make_loop(FixedClock(when))
    step = await loop.run_once()
    assert (len(rec.calls) == 1) is records
    assert in_quiet_window(when) is not records
    if not records:
        assert step.skipped == "scan_quiet"


@pytest.mark.usefixtures("no_running")
async def test_disabled_or_no_live_run_records_nothing() -> None:
    clock = FixedClock(et(TUE, 11, 0))
    loop, rec, _ = make_loop(clock, settings=RuntimeSettings(reports_decisions_enabled=False))
    assert (await loop.run_once()).skipped == "disabled"
    loop, rec2, _ = make_loop(clock, run_id=lambda: None)
    assert (await loop.run_once()).skipped == "no_run"
    assert rec.calls == [] and rec2.calls == []


@pytest.mark.usefixtures("no_running")
async def test_the_loop_runs_at_the_configured_cadence_until_stopped() -> None:
    clock = FixedClock(et(TUE, 10, 0))
    stop = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(timedelta(seconds=seconds))
        if len(slept) == 4:
            stop.set()

    loop, rec, _ = make_loop(
        clock, settings=RuntimeSettings(reports_decisions_refresh_seconds=120), sleep=sleep
    )
    await asyncio.wait_for(loop.run(stop), timeout=10)
    assert slept == [120.0] * 4
    assert [c[3] for c in rec.calls] == [et(TUE, 10, 0) + timedelta(seconds=120 * i) for i in range(4)]


@pytest.mark.db
async def test_a_running_event_row_of_today_skips_the_pass(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(et(TUE, 10, 0))
    loop, rec, _ = make_loop(clock, factory=db_factory)

    def job(name: str, day: date, status: str) -> None:
        with db_factory.begin() as s:
            s.add(m.JobRun(job=name, session_date=day, started_at=clock.now(), status=status))

    job("event:orb_open", TUE, "succeeded")
    job("event:flatten", date(2026, 10, 5), "running")  # another day's leftover
    job("premarket", TUE, "running")  # not an event
    assert (await loop.run_once()).skipped is None
    job("event:entry_cancel", TUE, "running")
    assert (await loop.run_once()).skipped == "event_running"
    assert len(rec.calls) == 1
    with db_factory.begin() as s:
        row = s.execute(select(m.JobRun).where(m.JobRun.job == "event:entry_cancel")).scalar_one()
        row.status = "succeeded"
    assert (await loop.run_once()).skipped is None
    assert len(rec.calls) == 2


# --- 1. failures: one warning per streak, one info on recovery, never raises -------------------------------


@pytest.mark.usefixtures("no_running")
async def test_one_warning_per_failure_streak_and_one_info_on_recovery() -> None:
    clock = FixedClock(et(TUE, 10, 0))
    loop, rec, events = make_loop(clock)
    rec.fail = 3
    for _ in range(5):
        step = await loop.run_once()
        clock.advance(timedelta(seconds=60))
    assert len(rec.calls) == 5
    assert [e[0] for e in events.rows] == ["warning", "info"]
    level, message, data, run_id = events.rows[0]
    assert "decision log pass for 2026-10-06 failed: RuntimeError" in message
    assert "hunter2" not in message and data["error_type"] == "RuntimeError" and run_id == RUN
    assert "after 3 failed passes" in events.rows[1][1]
    rec.fail = 1
    step = await loop.run_once()
    assert step.skipped == "error"
    assert [e[0] for e in events.rows] == ["warning", "info", "warning"]  # a new streak


@pytest.mark.usefixtures("no_running")
async def test_a_failing_settings_read_is_a_failure_not_a_crash() -> None:
    clock = FixedClock(et(TUE, 10, 0))

    def broken() -> RuntimeSettings:
        raise RuntimeError("settings table unreadable")

    loop, rec, events = make_loop(clock, settings=broken)
    for _ in range(3):
        assert (await loop.run_once()).skipped == "error"
    assert rec.calls == [] and [e[0] for e in events.rows] == ["warning"]


# --- 1. off the event loop ----------------------------------------------------------------------------------


async def _max_gap_while(work: Awaitable[Any]) -> tuple[float, Any]:
    """Run `work` beside a coroutine that wakes every 20 ms; return the longest real gap between its wakes."""
    done = asyncio.Event()
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        result = await work
    finally:
        done.set()
        await task
    return max(gaps), result


async def test_a_gate_blocking_3_s_in_its_thread_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trader.decisions.loop as loop_mod

    threads: set[str] = set()
    loop_thread = threading.current_thread().name

    def slow_running(factory: Any, day: date) -> bool:
        threads.add(threading.current_thread().name)
        time.sleep(1.5)
        return False

    def slow_settings() -> RuntimeSettings:
        threads.add(threading.current_thread().name)
        time.sleep(1.5)
        return RuntimeSettings()

    monkeypatch.setattr(loop_mod, "event_running", slow_running)
    loop, rec, _ = make_loop(FixedClock(et(TUE, 10, 0)), settings=slow_settings)
    started = time.monotonic()
    gap, step = await _max_gap_while(loop.run_once())
    assert time.monotonic() - started >= 3.0
    assert step.skipped is None and len(rec.calls) == 1
    assert gap < 0.5, gap
    assert loop_thread not in threads


@pytest.mark.db
async def test_the_worker_steps_on_while_the_decisions_loop_blocks_and_fails(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker runs the loop as its own supervised task: a pass blocking 3 s in its thread and then
    raising never delays the step loop, and shutdown cancels the task without waiting."""
    import trader.decisions.loop as loop_mod
    from tests.test_worker import Harness, VirtualTime, _run
    from trader.worker import Worker, WorkerDeps

    clock = FixedClock(et(TUE, 10, 0))
    vt = VirtualTime(clock)
    h = Harness(db_factory, clock, sleep=vt.sleep)
    blocked = threading.Event()

    def slow_running(factory: Any, day: date) -> bool:
        blocked.set()
        time.sleep(3)
        raise RuntimeError("stuck database")

    monkeypatch.setattr(loop_mod, "event_running", slow_running)
    loop, _, events = make_loop(clock, sleep=vt.sleep)
    stop = asyncio.Event()
    polls_while_blocked: list[int] = []

    def on_relay() -> None:
        if blocked.is_set():
            polls_while_blocked.append(1)
        if len(polls_while_blocked) >= 20:
            stop.set()

    h.on_relay = on_relay
    worker = Worker(WorkerDeps(**{**vars(h.deps()), "decisions": loop}))
    started = time.monotonic()
    await _run(worker, stop, vt)
    assert len(polls_while_blocked) >= 20  # the step loop and the relay went on meanwhile
    assert time.monotonic() - started < 3.0  # and the stop did not wait for the blocked pass
    assert h.engines and len(h.engines[-1].polls) >= 20


# --- 3. the post-close final pass ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_running")
async def test_final_pass_records_final_prunes_and_reads_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.decisions.loop as loop_mod
    from trader.decisions.summary import summarize

    order: list[str] = []
    clock = FixedClock(et(TUE, 16, 15))
    rec = FakeRecorder(clock=clock)

    async def record(
        deps: RecorderDeps, run_id: int, d: date, *, final: bool = False, rebuild: bool = False
    ) -> RecordResult:
        order.append(f"record final={final}")
        return await rec(deps, run_id, d, final=final)

    def fake_prune(factory: Any, c: Any, settings: RuntimeSettings) -> PruneResult:
        order.append("prune")
        return PruneResult(4, 2)

    summary = summarize([], run_id=RUN, session_date=TUE, final=True)

    def fake_state(factory: Any, run_id: int, d: date) -> tuple[Any, int]:
        order.append("read")
        return summary, 12

    monkeypatch.setattr(loop_mod, "prune", fake_prune)
    monkeypatch.setattr(loop_mod, "day_state", fake_state)
    deps = RecorderDeps(NoDb(), clock, CAL, RuntimeSettings, None)  # type: ignore[arg-type]
    out = await final_pass(deps, RUN, TUE, record=record)
    assert order == ["record final=True", "prune", "read"]
    assert out == FinalPass(rec_result(TUE), summary, 12, PruneResult(4, 2))

    def broken_prune(factory: Any, c: Any, settings: RuntimeSettings) -> PruneResult:
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(loop_mod, "prune", broken_prune)
    out = await final_pass(deps, RUN, TUE, record=record)
    assert out.pruned is None and out.prune_error == "RuntimeError" and out.summary is summary


def rec_result(d: date) -> RecordResult:
    return RecordResult(RUN, d, None, {"scan": 3, "day": 1}, True)


# --- 7 / static ---------------------------------------------------------------------------------------------


def test_the_loop_module_imports_no_trading_or_network_module() -> None:
    from tests.decisions.test_contracts import FORBIDDEN_MODULES

    code = "import sys\nimport trader.decisions.loop\nprint('\\n'.join(sorted(sys.modules)))\n"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = set(out.split())
    bad = sorted(x for x in loaded for f in FORBIDDEN_MODULES if x == f or x.startswith(f + "."))
    assert bad == []


# --- 3. the post-close: order, isolation, prune, the line ---------------------------------------------------


def _postclose_world(db_factory: sessionmaker[Session]) -> Any:
    """The P3 post-close test world (tests/jobs/test_postclose.py): AAA, BBB, CCC and an active live run."""
    from tests.factories import add_run, add_symbol
    from tests.fakes_telegram import FakeIssuer, FakeRenderer, RecordingNotifier
    from tests.jobs.test_postclose import FakeData, FakeEngine, World, member

    with db_factory() as s:
        ids = {t: add_symbol(s, t) for t in ("AAA", "BBB", "CCC", "SPY")}
        run_id = add_run(s)
        s.commit()
    data = FakeData([member(ids[t], t) for t in ("AAA", "BBB", "CCC")])
    return World(
        db_factory,
        run_id,
        ids,
        data,
        FakeEngine(),
        RecordingNotifier(),
        FakeRenderer(),
        FakeIssuer(),
        RuntimeSettings(),
    )


def _traced(w: Any, order: list[str]) -> None:
    """Record when the archive reads the universe and when the summary is rendered."""
    universe, render = w.data.universe, w.render.daily_summary

    async def traced_universe(d: date) -> Any:
        order.append("archive")
        return await universe(d)

    def traced_render(v: Any, buttons: Any) -> Any:
        order.append("summary")
        return render(v, buttons)

    w.data.universe = traced_universe
    w.render.daily_summary = traced_render


def _day_summary(run_id: int, d: date = TUE) -> Any:
    from trader.decisions.summary import summary_from_json

    return summary_from_json(
        {
            "run_id": run_id,
            "session_date": d.isoformat(),
            "final": True,
            "scanned": 812,
            "ranked": 14,
            "passed": 1,
            "rejects_by_rule": [["rvol_below_min", 790], ["catalyst_low_quality", 6], ["doji", 3], ["x", 1]],
            "proposals": 1,
            "approvals": {"manual": 1},
            "fills": 2,
            "trades": 1,
        }
    )


@pytest.mark.db
async def test_the_final_pass_runs_after_the_archive_and_before_the_summary(
    db_factory: sessionmaker[Session],
) -> None:
    import dataclasses as dc

    from trader.jobs.postclose import run_postclose

    w = _postclose_world(db_factory)
    order: list[str] = []
    _traced(w, order)

    async def decisions(d: date) -> FinalPass:
        order.append("decisions")
        result = RecordResult(w.run_id, d, None, {"day": 1}, True)
        return FinalPass(result, _day_summary(w.run_id), 5, PruneResult(3, 1))

    deps = dc.replace(w.deps(et(TUE, 16, 15)), decisions=decisions)
    out = await run_postclose(deps, TUE)
    assert order == ["archive", "decisions", "summary"]
    assert out["decisions"] == {"final": True, "rows": 5, "pruned": {"live": 3, "replay": 1}}
    (view,) = w.summaries()
    assert view.decision_log is not None
    assert (view.decision_log.scanned, view.decision_log.manual, view.decision_log.link) == (
        812,
        1,
        "/reports?day=2026-10-06",
    )
    assert view.decision_log.top_rejects == (
        ("rvol_below_min", 790),
        ("catalyst_low_quality", 6),
        ("doji", 3),
    )


@pytest.mark.db
async def test_a_raising_final_pass_leaves_the_archive_and_the_summary_and_says_error(
    db_factory: sessionmaker[Session],
) -> None:
    import dataclasses as dc

    from trader.jobs.postclose import run_postclose

    w = _postclose_world(db_factory)
    now = et(TUE, 16, 15)
    plain = await run_postclose(w.deps(now), TUE)  # without the hook
    (plain_view,) = w.summaries()

    async def boom(d: date) -> FinalPass:
        raise RuntimeError("deadlock detected: token=abc123")

    out = await run_postclose(dc.replace(w.deps(now), decisions=boom), TUE)  # a forced re-run, with it
    assert out["decisions"] == {"error": "RuntimeError"}
    assert out["archive"] == plain["archive"]
    _, view = w.summaries()
    assert view.decision_log is None and view == plain_view
    with db_factory() as s:
        events = s.execute(select(m.EventLog).where(m.EventLog.source == "decisions")).scalars().all()
    assert [e.level for e in events] == ["warning"] and "abc123" not in events[0].message


@pytest.mark.db
@pytest.mark.parametrize("case", ["in_summary_off", "no_day_row", "no_hook"])
async def test_no_decisions_line_when_off_or_without_a_day_row(
    db_factory: sessionmaker[Session], case: str
) -> None:
    import dataclasses as dc

    from trader.jobs.postclose import run_postclose

    w = _postclose_world(db_factory)
    summary = None if case == "no_day_row" else _day_summary(w.run_id)
    if case == "in_summary_off":
        w.settings = RuntimeSettings(reports_decisions_in_summary=False)

    async def decisions(d: date) -> FinalPass:
        return FinalPass(RecordResult(w.run_id, d, "disabled", {}, False), summary, 0, PruneResult(0, 0))

    deps = w.deps(et(TUE, 16, 15))
    if case != "no_hook":
        deps = dc.replace(deps, decisions=decisions)
    out = await run_postclose(deps, TUE)
    (view,) = w.summaries()
    assert view.decision_log is None
    assert ("decisions" in out) is (case != "no_hook")


@pytest.mark.db
async def test_the_real_final_pass_freezes_the_day_prunes_and_feeds_the_line(
    db_factory: sessionmaker[Session],
) -> None:
    """Through `runtime.decisions_final_pass` (the post-close job's wiring) over the test database."""
    import dataclasses as dc
    from types import SimpleNamespace

    import trader.runtime as rt
    from tests.decisions.test_read import seed_day
    from trader.jobs.postclose import run_postclose

    w = _postclose_world(db_factory)
    with db_factory.begin() as s:
        seed_day(s, w.run_id, TUE - timedelta(days=500), prefix="OLD")  # past the 400-day retention
    now = et(TUE, 16, 15)
    core = SimpleNamespace(
        factory=db_factory,
        clock=FixedClock(now),
        calendar=CAL,
        settings=SimpleNamespace(load=lambda: w.settings),
    )
    deps = dc.replace(w.deps(now), decisions=rt.decisions_final_pass(core, w.run_id))  # type: ignore[arg-type]
    out = await run_postclose(deps, TUE)
    assert out["decisions"]["final"] is True and out["decisions"]["rows"] >= 2
    assert out["decisions"]["pruned"] == {"live": 9, "replay": 0}
    with db_factory() as s:
        rows = (
            s.execute(
                select(m.DecisionLog).where(m.DecisionLog.session_date == TUE).order_by(m.DecisionLog.seq)
            )
            .scalars()
            .all()
        )
    assert rows and all(r.final for r in rows) and rows[-1].stage == "day"
    (view,) = w.summaries()
    assert view.decision_log is not None and view.decision_log.link == "/reports?day=2026-10-06"


# --- 5. the summary line ------------------------------------------------------------------------------------


def _summary_view(**kw: Any) -> Any:
    from decimal import Decimal

    from trader.notify.types import DailySummaryView

    return DailySummaryView(
        session_date=TUE,
        trades=(),
        realized_pnl=Decimal(0),
        fees=Decimal(0),
        equity=Decimal("10000"),
        drawdown_pct=Decimal(0),
        open_positions=(),
        decisions=1,
        avg_decision_seconds=42.0,
        unprotected_seconds=0,
        blocking_switches=(),
        archive={"5m": 3},
        **kw,
    )


def test_the_decisions_line_is_escaped_linked_and_additive() -> None:
    from trader.jobs.postclose import decisions_line
    from trader.notify.messages import TELEGRAM_LIMIT, MessageRenderer
    from trader.notify.types import DecisionsLineView

    clock = FixedClock(et(TUE, 16, 15))
    render = MessageRenderer("https://trader.test/", ZoneInfo("America/Edmonton"), clock=clock)
    plain = render.daily_summary(_summary_view(), ())
    line = decisions_line(_day_summary(RUN), TUE)
    with_line = render.daily_summary(_summary_view(decision_log=line), ())
    assert (with_line.kind, with_line.silent, with_line.buttons) == (plain.kind, plain.silent, plain.buttons)
    extra = [x for x in with_line.text.split("\n") if x not in plain.text.split("\n")]
    assert extra == [
        "Decisions: 812 scanned · 14 ranked · 1 passed · 1 proposal (1 manual) · 2 fills · 1 trade · "
        "top rejects rvol_below_min 790, catalyst_low_quality 6, doji 3 · "
        '<a href="https://trader.test/reports?day=2026-10-06">details</a>'
    ]
    assert with_line.text.replace(extra[0] + "\n", "") == plain.text

    nasty = DecisionsLineView(0, 0, 0, 0, 0, 0, 0, 0, (("<b>&x", 2),), "/reports?day=2026-10-06")
    text = render.daily_summary(_summary_view(decision_log=nasty), ()).text
    assert "top rejects &lt;b&gt;&amp;x 2" in text and "0 proposals" in text and "<b>&x" not in text

    huge = DecisionsLineView(
        9, 9, 9, 9, 9, 0, 9, 9, tuple(("r" * 60, 10**6) for _ in range(3)), "/reports?day=x"
    )
    assert len(render.daily_summary(_summary_view(decision_log=huge), ()).text) <= TELEGRAM_LIMIT


# --- 7. the runtime composition -----------------------------------------------------------------------------


@pytest.mark.db
async def test_run_worker_passes_a_decisions_loop_for_its_live_run(
    monkeypatch: pytest.MonkeyPatch,
    world: Any,  # noqa: F811
) -> None:
    import trader.runtime as rt
    from trader.decisions.recorder import LiveScanData
    from trader.worker import Worker, WorkerDeps

    captured: list[WorkerDeps] = []

    async def capture(self: Worker, stop: Any, *, once: bool = False) -> None:
        captured.append(self.deps)

    monkeypatch.setattr(Worker, "run", capture)
    assert await rt.run_worker(once=True) == 0
    (deps,) = captured
    loop = deps.decisions
    assert isinstance(loop, DecisionsLoop)
    assert isinstance(loop.deps.scan_data, LiveScanData)
    assert loop.deps.factory is world.core.factory and loop.deps.clock is world.core.clock
    with world.core.factory() as s:
        live = s.execute(select(m.Run.id).where(m.Run.mode == "live", m.Run.status == "active")).scalar_one()
    assert loop.run_id() == live
    assert isinstance(loop.deps.settings(), RuntimeSettings)
