"""P4-BB gauntlet breaker: backend group B (P4-T6 decisions, P4-T9 system and jobs, P4-T10 watchlist, P4-T17
the Docker files).

Review Focus 1 (a web approval bypassing the kill switches, or a double decision) and 3 (deployment breaking
trading), plus the hostile-input edges of the manual job runs, the token paste and the CSV upload. DB tests
use the testcontainers database; the shell scripts run with stub executables first on PATH (no Docker).
"""

import asyncio
import configparser
import gzip
import io
import logging
import os
import re
import signal
import stat
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from tests.api.conftest import BASE_URL, DEFAULT_USER, make_client
from tests.api.test_proposals import World, _signal, new_entry, open_position, orders, proposal_row
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_api import (
    FakeCredentialStore,
    FakeJobLauncher,
    RecordingDeciderFor,
    make_services,
    test_core,
)
from tests.jobs.test_nightly import CLOCK as NIGHTLY_CLOCK
from tests.jobs.test_nightly import TARGET, FakeFinviz, StableMarket, deps, snapshot
from trader import runtime
from trader.adapters.questrade.auth import QuestradeAuthError
from trader.api.deps import current_user
from trader.api.errors import ApiError, install_error_handlers
from trader.api.launcher import CLI_ARGS, NO_OPTIONS, SubprocessJobLauncher
from trader.api.routers import credentials, jobs, killswitch, proposals, system, watchlist
from trader.broker.types import OrderSpec
from trader.cli import app as cli_app
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.proposals import Decision, DecisionResult, ProposalService, Via
from trader.engine.risk import SizedOrder
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.jobs.nightly import run_nightly
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.watchlist import get_watchlist, store_watchlist
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Cancel, Exit

DAY = date(2026, 10, 6)  # a Tuesday session
CAL = SessionCalendar()
TOKEN = "qtBreakerREFRESHtoken9f8e7d6c5b4a"
TRADER = Path(__file__).resolve().parents[3]  # Trader/
DOCKER = TRADER / "docker"


def et(h: int, mi: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, mi, s, tzinfo=ET).astimezone(UTC)


# --- a trading world (the same shape as tests/api/test_proposals.py) ---------------------------------


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    clock = FixedClock(et(9, 40))
    core = test_core(db_factory, clock)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA", questrade_id=11)
        cfg = add_strategy_config(s)
        signal_id = _signal(s, run.id, sym, cfg, clock)
        s.commit()
    broker = runtime.build_sim_broker(core, run.id, RuntimeSettings())
    svc = ProposalService(db_factory, clock, core.settings, broker, run.id)
    return World(core, clock, run.id, sym, cfg, signal_id, svc)


def _trip(w: World, switch: str) -> None:
    with w.factory() as s:
        s.add(
            m.KillSwitchEvent(
                run_id=w.run_id,
                switch=switch,
                session_date=DAY,
                tripped_at=w.clock.now() - timedelta(minutes=2),
                value=Decimal("0.2"),
                threshold=Decimal("0.1"),
            )
        )
        s.commit()


def _audits(factory: sessionmaker[Session], prefix: str = "") -> list[m.AuditLog]:
    with factory() as s:
        return list(
            s.scalars(select(m.AuditLog).where(m.AuditLog.action.startswith(prefix)).order_by(m.AuditLog.id))
        )


def _gated(recording: RecordingDeciderFor, barrier: threading.Barrier) -> Callable[..., Any]:
    def gated_for(run_id: int) -> Callable[[int, Decision, Via, str], DecisionResult]:
        inner = recording(run_id)

        def decide(proposal_id: int, decision: Decision, via: Via, actor: str) -> DecisionResult:
            barrier.wait()
            return inner(proposal_id, decision, via, actor)

        return decide

    return gated_for


# ==========================================================================================================
# P4-T6: decisions and kill switches
# ==========================================================================================================


@pytest.mark.db
def test_paused_and_tripped_web_approvals_block_entries_but_exits_and_cancels_go_through(
    world: World,
) -> None:
    """Review Focus 1: with /pause AND daily_loss_pct active, a web entry approval is refused with no order,
    while an exit (flatten) and a cancel of a working entry order still execute."""
    client = make_client(make_services(world.core), proposals.router)
    position_id = open_position(world)
    working = new_entry(world)  # approved BEFORE the pause: a working buy-stop order
    assert client.post(f"/api/proposals/{working}/approve").json()["message"] == "Approved"
    (working_order,) = orders(world, working)
    assert working_order.status == "working"

    assert KillSwitches(world.factory, world.clock).pause(world.run_id, DAY, "telegram:1")
    _trip(world, "daily_loss_pct")
    entry = new_entry(world)
    exit_spec = OrderSpec(
        world.sym, "sell", "market", 10, purpose="exit", position_id=position_id, reason="flat"
    )
    exit_pid = world.svc.create(
        world.signal_id,
        SizedOrder(Exit(position_id, "market", None, "flat"), "exit", 10, exit_spec, position_id=position_id),
        "exit",
    ).id
    cancel_pid = world.svc.create(
        world.signal_id,
        SizedOrder(
            Cancel(working_order.id, "entry_cancel_at"), "cancel", 10, None, cancel_order_id=working_order.id
        ),
        "cancel",
    ).id
    before = len(orders(world))

    blocked = client.post(f"/api/proposals/{entry}/approve").json()
    assert blocked["blocked"] is not None and blocked["blocked"].startswith("kill switch ")
    assert blocked["message"].startswith("Entry blocked: kill switch ")
    assert blocked["proposal"]["status"] == "rejected" and orders(world, entry) == []

    for pid in (exit_pid, cancel_pid):
        r = client.post(f"/api/proposals/{pid}/approve")
        assert r.status_code == 200, r.text
        assert r.json()["message"] == "Approved" and r.json()["blocked"] is None, pid
        assert proposal_row(world, pid).status == "submitted", pid
    (exit_order,) = orders(world, exit_pid)
    assert exit_order.purpose == "exit"
    with world.factory() as s:
        assert s.get_one(m.Order, working_order.id).status == "cancelled"
    assert len(orders(world)) == before + 1  # only the exit order; the entry placed nothing


@pytest.mark.db
def test_web_approve_web_reject_and_telegram_tap_racing_give_exactly_one_decision(world: World) -> None:
    """Three taps reach ProposalService.decide at the same instant: a web approve, a second tab's web
    reject, and a Telegram approve. Exactly one decides; the other two get already_decided."""
    pid = new_entry(world)
    barrier = threading.Barrier(3, timeout=20)
    client = make_client(
        make_services(world.core, decider_for=_gated(RecordingDeciderFor(world.core), barrier)),
        proposals.router,
    )
    telegram = runtime.build_decider(world.core, world.run_id)
    results: dict[str, Any] = {}

    def web(decision: str) -> None:
        results[f"web-{decision}"] = client.post(f"/api/proposals/{pid}/{decision}").json()

    def tap() -> None:
        barrier.wait()
        results["telegram"] = telegram(pid, "approve", "telegram", "telegram:4242")

    threads = [
        threading.Thread(target=web, args=("approve",)),
        threading.Thread(target=web, args=("reject",)),
        threading.Thread(target=tap),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert set(results) == {"web-approve", "web-reject", "telegram"}
    decided = [
        results["web-approve"]["already_decided"],
        results["web-reject"]["already_decided"],
        results["telegram"].already_decided,
    ]
    assert decided.count(False) == 1, results
    final = proposal_row(world, pid)
    assert len(orders(world, pid)) == (1 if final.status == "submitted" else 0)
    assert len(_audits(world.factory, "proposal.")) == 1


@pytest.mark.db
def test_other_run_expiry_boundary_and_bad_ids(world: World) -> None:
    """A proposal of another (running) run is a 404 on every route and stays pending; approving at EXACTLY
    expires_at is `Already expired` with no order and no decision audit; out-of-range ids are 422, never a
    database error."""
    with world.factory() as s:
        other_run = add_run(s, mode="replay", status="running")
        other_signal = _signal(s, other_run, world.sym, world.cfg, world.clock)
        s.commit()
    other_svc = ProposalService(
        world.factory,
        world.clock,
        world.core.settings,
        runtime.build_sim_broker(world.core, other_run, RuntimeSettings()),
        other_run,
    )
    other = new_entry(world, other_svc, other_signal)
    stale = new_entry(world)
    client = make_client(make_services(world.core), proposals.router, raise_server_exceptions=False)

    for path in (f"/api/proposals/{other}/approve", f"/api/proposals/{other}/reject"):
        assert client.post(path).status_code == 404, path
    assert client.get(f"/api/proposals/{other}").status_code == 404
    assert other not in [
        p["id"] for p in client.get("/api/proposals", params={"status": "all"}).json()["items"]
    ]
    assert proposal_row(world, other).status == "pending"

    for bad in ("0", "-1", str(2**63), "1e3", "abc"):
        r = client.post(f"/api/proposals/{bad}/approve")
        assert r.status_code == 422, (bad, r.status_code, r.text)

    world.clock.set(proposal_row(world, stale).expires_at)  # exactly at the expiry instant
    body = client.post(f"/api/proposals/{stale}/approve").json()
    assert body["already_decided"] is True and body["message"] == "Already expired"
    again = client.post(f"/api/proposals/{stale}/reject").json()
    assert again["message"] == "Already expired"
    assert orders(world, stale) == []
    assert [a.action for a in _audits(world.factory, "proposal.")] == []


def _csrf_client(services: Any) -> TestClient:
    """The real CSRF check (T4's `require_csrf`): only the session lookup is overridden."""
    app = FastAPI()
    install_error_handlers(app)
    for r in (proposals.router, killswitch.router, jobs.router, credentials.router, system.router):
        app.include_router(r, prefix="/api")
    app.include_router(watchlist.router, prefix="/api")
    app.state.services = services
    app.dependency_overrides[current_user] = lambda: DEFAULT_USER
    return TestClient(app, base_url=BASE_URL)


@pytest.mark.db
def test_every_state_changing_route_of_group_b_needs_the_csrf_header(world: World) -> None:
    """Review Focus 1 (cross-site request): every POST/DELETE of T6, T9 and T10 refuses a request without
    the header, with a wrong token, or with the right token from a foreign Origin, and changes nothing."""
    pid = new_entry(world)
    _trip(world, "max_drawdown_pct")
    store_watchlist(world.factory, world.clock, date(2026, 10, 7), ["AAPL"], "a.csv", "web:stephen")
    jobs_fake, creds = FakeJobLauncher(), FakeCredentialStore()
    services = make_services(world.core, jobs=jobs_fake, credentials=creds)
    client = _csrf_client(services)
    requests: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", f"/api/proposals/{pid}/approve", {}),
        ("POST", f"/api/proposals/{pid}/reject", {}),
        ("POST", "/api/killswitch/pause", {}),
        ("POST", "/api/killswitch/resume", {}),
        ("POST", "/api/killswitch/max_drawdown_pct/reset", {"json": {"reason": "reviewed it"}}),
        ("POST", "/api/jobs/nightly/run", {"json": {"force": True}}),
        ("POST", "/api/credentials/questrade", {"json": {"refresh_token": TOKEN}}),
        ("POST", "/api/system/telegram-test", {}),
        ("POST", "/api/watchlist", {"files": {"file": ("l.csv", b"MSFT\n", "text/csv")}}),
        ("DELETE", "/api/watchlist/2026-10-07", {}),
    ]
    # the list covers every unsafe route of these routers
    unsafe = {
        (method, route.path)
        for route in client.app.routes  # type: ignore[attr-defined]
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "DELETE", "PATCH"}
    }
    templated = {
        (meth, re.sub(r"/(\d+|2026-10-07|max_drawdown_pct|nightly)(?=/|$)", "/{x}", path))
        for meth, path, _ in requests
    }
    assert {(meth, re.sub(r"\{[^}]+\}", "{x}", p)) for meth, p in unsafe} <= templated, unsafe

    audits_before = len(_audits(world.factory))
    header_sets = (
        {},
        {"X-CSRF-Token": "not-the-token"},
        {"X-CSRF-Token": DEFAULT_USER.csrf_token, "Origin": "https://evil.example"},
    )
    for headers in header_sets:
        for method, path, kw in requests:
            r = client.request(method, path, headers=headers, **kw)
            assert r.status_code == 403, (headers, method, path, r.status_code, r.text)
    assert proposal_row(world, pid).status == "pending"
    assert KillSwitches(world.factory, world.clock).blocking(world.run_id, DAY) == "max_drawdown_pct"
    assert jobs_fake.launches == [] and creds.seeded == []
    assert services.notifier.sent == []  # type: ignore[attr-defined]
    assert get_watchlist(world.factory, date(2026, 10, 7)) is not None
    assert len(_audits(world.factory)) == audits_before
    # the harness itself works: the right token passes
    ok = client.post("/api/killswitch/pause", headers={"X-CSRF-Token": DEFAULT_USER.csrf_token})
    assert ok.status_code == 200, ok.text


@pytest.mark.db
def test_reset_reason_bounds_unicode_audit_and_a_real_reset_race(world: World) -> None:
    """The typed reason: stripped, 3-500 characters counted as characters (unicode), never echoed in a
    422; the audit row and the trip row carry the stripped reason and the web actor. Two real concurrent
    resets of one trip: one 200, one 409, one audit row."""
    client = make_client(make_services(world.core), killswitch.router)
    _trip(world, "max_drawdown_pct")
    for reason in ("  Zq  ", "Z" * 501, "\t\n "):
        r = client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": reason})
        assert r.status_code == 422, (reason, r.text)
        if reason.strip():
            assert reason.strip() not in r.text
    assert client.post("/api/killswitch/max_drawdown_pct/reset", json={}).status_code == 422
    assert client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": None}).status_code == 422
    assert _audits(world.factory, "killswitch.") == []

    ok = client.post("/api/killswitch/max_drawdown_pct/reset", json={"reason": "  ✓éß  "})
    assert ok.status_code == 200, ok.text
    (audit,) = _audits(world.factory, "killswitch.")
    assert (audit.actor, audit.after) == ("web:stephen", {"reason": "✓éß"})

    _trip(world, "max_drawdown_pct")
    long_reason = "r" * 500
    barrier = threading.Barrier(2, timeout=20)
    codes: list[int] = []
    lock = threading.Lock()

    def reset() -> None:
        barrier.wait()
        code = client.post(
            "/api/killswitch/max_drawdown_pct/reset", json={"reason": f"  {long_reason}  "}
        ).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=reset) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(codes) == [200, 409], codes
    resets = _audits(world.factory, "killswitch.reset")
    assert len(resets) == 2 and resets[-1].after == {"reason": long_reason}


@pytest.mark.db
def test_pause_and_resume_are_idempotent_under_concurrency(world: World) -> None:
    """Two tabs press Pause together with a Telegram /pause: one pause row and one audit row; then two
    Resumes together: one 200 and one 409."""
    client = make_client(make_services(world.core), killswitch.router)
    ks = KillSwitches(world.factory, world.clock)
    barrier = threading.Barrier(3, timeout=20)
    codes: list[Any] = []
    lock = threading.Lock()

    def web(path: str) -> None:
        barrier.wait()
        code = client.post(path).status_code
        with lock:
            codes.append(code)

    def telegram_pause() -> None:
        barrier.wait()
        done = ks.pause(world.run_id, DAY, "telegram:4242")
        with lock:
            codes.append(done)

    threads = [threading.Thread(target=web, args=("/api/killswitch/pause",)) for _ in range(2)]
    threads.append(threading.Thread(target=telegram_pause))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(1 for c in codes if c in (200, True)) == 1, codes
    with world.factory() as s:
        pauses = list(s.scalars(select(m.KillSwitchEvent).where(m.KillSwitchEvent.switch == "manual_pause")))
    assert len(pauses) == 1
    assert len(_audits(world.factory, "killswitch.pause")) == 1

    barrier2 = threading.Barrier(2, timeout=20)
    codes2: list[int] = []

    def resume() -> None:
        barrier2.wait()
        code = client.post("/api/killswitch/resume").status_code
        with lock:
            codes2.append(code)

    threads = [threading.Thread(target=resume) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(codes2) == [200, 409], codes2
    assert len(_audits(world.factory, "killswitch.resume")) == 1
    assert ks.blocking(world.run_id, DAY) is None


# ==========================================================================================================
# P4-T9: jobs, events, token paste, Telegram test
# ==========================================================================================================


class ThreadProc:
    """A fake child process finished from any thread."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.code = 0
        self.done = threading.Event()
        self.raise_on_wait: BaseException | None = None

    def finish(self, code: int) -> None:
        self.code = code
        self.done.set()

    async def wait(self) -> int:
        while not self.done.is_set():
            await asyncio.sleep(0.01)
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        return self.code


class RecordingSpawn:
    def __init__(self, delay: float = 0.0) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.procs: list[ThreadProc] = []
        self.delay = delay

    async def __call__(self, *argv: Any, **kwargs: Any) -> ThreadProc:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append((argv, kwargs))
        proc = ThreadProc(4000 + len(self.procs))
        self.procs.append(proc)
        return proc


JOBS_NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET


@pytest.mark.db
def test_manual_job_run_allowlist_and_argv_are_injection_proof(db_factory: sessionmaker[Session]) -> None:
    """Only the ManualJob names run; nothing from the job name or body reaches the argv except a validated
    date and `--force`; argv is a list of plain strings with no shell; token-refresh takes no options."""
    spawn = RecordingSpawn()
    core = test_core(db_factory, FixedClock(JOBS_NOW))
    launcher = SubprocessJobLauncher(db_factory, core.clock, CAL, spawn=spawn)
    with make_client(make_services(core, jobs=launcher), jobs.router) as client:
        for name in (
            "nightly;rm -rf ~",
            "nightly --force",
            "nightly%20--date%202026-10-07",
            "NIGHTLY",
            "nightly ",
            "$(id)",
            "event",
            "questrade-seed",
            "token-refresh;id",
        ):
            r = client.post(f"/api/jobs/{name}/run", json={})
            assert r.status_code == 404, (name, r.status_code, r.text)
        bad_bodies: list[tuple[str, dict[str, Any]]] = [
            ("nightly", {"date": "2026-10-07; rm -rf /"}),
            ("nightly", {"date": "2026-10-07 --force"}),
            ("nightly", {"date": "--help"}),
            ("nightly", {"date": "2026-11-26"}),  # Thanksgiving
            ("premarket", {"date": "2026-10-10"}),  # a Saturday
            ("nightly", {"date": "2199-01-02"}),  # outside the calendar
            ("nightly", {"force": "true; rm"}),
            ("token-refresh", {"force": True}),
            ("token-refresh", {"date": "2026-10-07"}),
        ]
        for job, body in bad_bodies:
            r = client.post(f"/api/jobs/{job}/run", json=body)
            assert r.status_code == 422, (job, body, r.status_code, r.text)
        assert spawn.calls == []
        assert _audits(db_factory, "job.run_manual") == []

        r = client.post(
            "/api/jobs/premarket/run", json={"date": "2026-10-07", "force": True, "args": ["--evil"]}
        )
        assert r.status_code == 202, r.text
        r = client.post("/api/jobs/token-refresh/run", json={"force": False})
        assert r.status_code == 202, r.text
        assert r.json()["session_date"] is None  # (types.ts has it non-null: see the review)
        for proc in spawn.procs:
            proc.finish(0)
        for _ in range(200):
            if not launcher.children():
                break
            time.sleep(0.01)
    argvs = [argv for argv, _ in spawn.calls]
    assert argvs == [
        ("trader", "premarket", "--date", "2026-10-07", "--force"),
        ("trader", "token-refresh"),
    ]
    for argv, kwargs in spawn.calls:
        assert all(type(a) is str for a in argv)
        assert set(kwargs) <= {"stdin"} and "shell" not in kwargs and "env" not in kwargs
    # every launched command line is one the CLI really accepts (with its options), as cron's are below
    runner = CliRunner()
    for job, args in CLI_ARGS.items():
        extra = [] if job in NO_OPTIONS else ["--date", "2026-10-07", "--force"]
        result = runner.invoke(cli_app, [*args, *extra, "--help"])
        assert result.exit_code == 0, (job, result.output)


@pytest.mark.db
def test_concurrent_launches_start_one_child_and_a_failing_child_writes_one_warning(
    db_factory: sessionmaker[Session],
) -> None:
    """Five launches of `nightly` at once (the spawn is slow): exactly one child, one audit row, four 409s.
    Then children ending with 1, -15 (killed by a signal) and 0, and one whose wait() raises: exactly one
    warning each for the two failures, none for the clean exit, and the job can be launched again."""
    spawn = RecordingSpawn(delay=0.05)
    launcher = SubprocessJobLauncher(db_factory, FixedClock(JOBS_NOW), CAL, spawn=spawn)

    async def scenario() -> list[Any]:
        results = await asyncio.gather(
            *(launcher.launch("nightly", None, False, "web:stephen") for _ in range(5)),
            return_exceptions=True,
        )
        out: list[Any] = list(results)
        codes = [1, -15, 0]
        for code in codes:
            if not spawn.procs[-1].done.is_set():
                spawn.procs[-1].finish(code)
            for _ in range(300):
                if not launcher.running("nightly"):
                    break
                await asyncio.sleep(0.01)
            assert not launcher.running("nightly")
            if code != codes[-1]:
                await launcher.launch("nightly", None, False, "web:stephen")
        await launcher.launch("postclose", None, False, "web:stephen")
        spawn.procs[-1].raise_on_wait = OSError("no such process")
        spawn.procs[-1].finish(0)
        for _ in range(300):
            if not launcher.running("postclose"):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)  # let the reaper's DB write (in a thread) land
        return out

    results = asyncio.run(scenario())
    oks = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, ApiError)]
    assert len(oks) == 1 and oks[0].launched is True
    assert len(conflicts) == 4 and all(c.status == 409 for c in conflicts)
    assert len(_audits(db_factory, "job.run_manual")) == 3 + 1  # the first + two relaunches + postclose
    with db_factory() as s:
        events = list(
            s.scalars(select(m.EventLog).where(m.EventLog.source == "jobs.manual").order_by(m.EventLog.id))
        )
    assert [(e.level, e.data["exit_code"]) for e in events] == [("warning", 1), ("warning", -15)]
    assert all(set(e.data) == {"job", "date", "exit_code"} for e in events)


@pytest.mark.db
@pytest.mark.parametrize(
    "case",
    ["whitespace", "unicode_upstream_error", "auth_error_quoting_token", "huge_body", "too_long"],
)
def test_token_paste_never_leaks_the_token(
    db_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    store = FakeCredentialStore()
    token = TOKEN
    body: Any = None
    expect = 200
    if case == "whitespace":
        body = {"refresh_token": f"\t\r\n  {token} \n"}
    elif case == "unicode_upstream_error":
        token = "Токен-üñî-" + TOKEN
        store.access_error = RuntimeError(f"cannot send header value {token!r} ({token})")
        body, expect = {"refresh_token": token}, 502
    elif case == "auth_error_quoting_token":
        store.access_error = QuestradeAuthError(f"The refresh token {token} was rejected (400)")
        body, expect = {"refresh_token": f"  {token}  "}, 422
    elif case == "huge_body":
        token = TOKEN * 150_000  # about 5 MB
        body, expect = {"refresh_token": token}, 422
    else:
        token = "T" * 401
        body, expect = {"refresh_token": token}, 422
    services = make_services(test_core(db_factory, FixedClock(JOBS_NOW)), credentials=store)
    client = make_client(services, credentials.router, raise_server_exceptions=False)
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        r = client.post("/api/credentials/questrade", json=body)
    assert r.status_code == expect, (case, r.status_code, r.text[:300])
    if case == "whitespace":
        assert store.seeded == [token]
    everything = [r.text, repr(logs), caplog.text, *capsys.readouterr()]
    with db_factory() as s:
        for a in s.scalars(select(m.AuditLog)):
            everything.append(repr((a.actor, a.action, a.before, a.after)))
        for e in s.scalars(select(m.EventLog)):
            everything.append(repr((e.message, e.data)))
    probe = TOKEN[:16]  # any recognisable piece of the token
    for text in everything:
        assert probe not in text, (case, text[:300])
    if expect in (422, 502) and case not in ("huge_body", "too_long"):
        with db_factory() as s:
            (audit,) = s.scalars(select(m.AuditLog)).all()
        assert audit.after == {"ok": False}


@pytest.mark.db
def test_events_bounds_and_telegram_not_configured(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(JOBS_NOW)
    with db_factory() as s:
        for i, level in enumerate(["debug", "info", "warning", "error", "critical"] * 2):
            log_event(s, clock, level, "breaker", f"event {i}")
        s.commit()
    services = make_services(test_core(db_factory, clock), telegram_configured=False)
    client = make_client(services, system.router, raise_server_exceptions=False)

    window = client.get("/api/events", params={"since": 3, "before": 8}).json()["items"]
    assert [e["id"] for e in window] == [4, 5, 6, 7]
    assert {e["level"] for e in client.get("/api/events", params={"level": "warning"}).json()["items"]} == {
        "warning",
        "error",
        "critical",
    }
    for params in (
        {"since": -1},
        {"before": 0},
        {"limit": 0},
        {"limit": 501},
        {"level": "WARNING"},
        {"level": "fatal"},
        {"source": "s" * 51},
        {"since": "1; DROP TABLE trader.event_log"},
    ):
        r = client.get("/api/events", params=params)
        assert r.status_code == 422, (params, r.status_code)
    # an id beyond bigint is a client error, never a 500 from the database
    for params in ({"since": str(2**63)}, {"before": str(2**64)}):
        r = client.get("/api/events", params=params)
        assert r.status_code in (200, 422), (params, r.status_code, r.text[:200])

    r = client.post("/api/system/telegram-test")
    assert r.status_code == 409 and r.json()["error"]["message"] == "Telegram is not configured"
    assert services.notifier.sent == []  # type: ignore[attr-defined]
    assert _audits(db_factory, "telegram.") == []


# ==========================================================================================================
# P4-T10: the watchlist upload
# ==========================================================================================================

WL_NOW = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)  # Tuesday 6 Oct, 23:00 ET (already Wednesday in UTC)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("list.csv", b"AAPL\n" * 2_000_000)
    return buf.getvalue()


HOSTILE: list[tuple[str, bytes, list[str] | None]] = [
    # (case, file bytes, expected tickers or None for a 422 with nothing stored)
    ("formulas", b"Ticker\n=cmd|' /C calc'!A0\n+1+1\n-2\n@SUM(A1)\n=HYPERLINK(\"x\")\nAAPL\n", ["AAPL"]),
    ("nul", b"AAPL\x00\nMSFT\n", None),
    (
        "bom_crlf",
        b"\xef\xbb\xbfSymbol,Name\r\nmsft,Microsoft\r\nBF-B,Brown\r\nbf.b,dup\r\n\r\n",
        ["MSFT", "BF.B"],
    ),
    ("ten_mb", b"AAPL\n" * 2_100_000, None),
    ("ten_k_rows", b"".join(f"T{i}\n".encode() for i in range(10_000)), None),
    ("duplicates", b"BF.B\nBF-B\nbf-b\nBRK.B\nBRK-B\n", ["BF.B", "BRK.B"]),
    ("latin1", "Symbol\nNESTLÉ\nAAPL\n".encode("latin-1"), None),
    ("utf16", "Symbol\nAAPL\n".encode("utf-16"), None),
    ("gzip_bomb", gzip.compress(b"AAPL\n" * 4_000_000), None),
    ("zip_bomb", _zip_bytes(), None),
]


@pytest.mark.db
@pytest.mark.parametrize(("case", "data", "expected"), HOSTILE, ids=[h[0] for h in HOSTILE])
def test_hostile_csv_uploads(
    db_factory: sessionmaker[Session], case: str, data: bytes, expected: list[str] | None
) -> None:
    services = make_services(test_core(db_factory, FixedClock(WL_NOW)))
    client = make_client(services, watchlist.router, raise_server_exceptions=False)
    r = client.post(
        "/api/watchlist", files={"file": ("list.csv", data, "text/csv")}, data={"date": "2026-10-07"}
    )
    stored = get_watchlist(db_factory, date(2026, 10, 7))
    if expected is None:
        assert r.status_code == 422, (case, r.status_code, r.text[:300])
        assert stored is None and _audits(db_factory, "watchlist.") == []
    else:
        assert r.status_code == 200, (case, r.text[:300])
        assert r.json()["watchlist"]["tickers"] == expected
        assert stored is not None and list(stored.tickers) == expected
        if case == "formulas":
            assert {x["reason"] for x in r.json()["rejected"]} == {"invalid ticker"}
            assert len(r.json()["rejected"]) == 5


@pytest.mark.db
def test_two_files_and_the_date_rules_in_eastern_time(db_factory: sessionmaker[Session]) -> None:
    """Two files in one multipart → a 4xx with nothing stored (never a 500). The date is checked against
    today's ET session: at 23:00 ET (already tomorrow in UTC) today's date is still allowed; yesterday,
    a holiday, a weekend and a date beyond the calendar are 422."""
    services = make_services(test_core(db_factory, FixedClock(WL_NOW)))
    client = make_client(services, watchlist.router, raise_server_exceptions=False)
    two = [("file", ("a.csv", b"AAPL\n", "text/csv")), ("file", ("b.csv", b"MSFT\n", "text/csv"))]
    r = client.post("/api/watchlist", files=two)
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
    other = [("file", ("a.csv", b"AAPL\n", "text/csv")), ("extra", ("b.csv", b"MSFT\n", "text/csv"))]
    r = client.post("/api/watchlist", files=other)
    assert 400 <= r.status_code < 500, (r.status_code, r.text)
    assert _audits(db_factory, "watchlist.") == []

    def up(day: str) -> int:
        return client.post(
            "/api/watchlist", files={"file": ("l.csv", b"AAPL\n", "text/csv")}, data={"date": day}
        ).status_code

    assert up("2026-10-06") == 200  # today in ET (UTC says 7 Oct)
    for day in ("2026-10-05", "2026-11-26", "2026-12-25", "2026-10-10", "2199-01-05", "2026-02-30"):
        assert up(day) == 422, day
    assert get_watchlist(db_factory, date(2026, 10, 5)) is None


@pytest.mark.db
async def test_upload_replaces_finviz_in_nightly_and_delete_restores_it_with_audit_rows(
    db_factory: sessionmaker[Session],
) -> None:
    """Resolved decision 5, end to end through the API: the uploaded list IS the universe (no FinViz call,
    no merge); a second upload replaces the first; DELETE goes back to FinViz. The audit rows chain."""
    services = make_services(test_core(db_factory, NIGHTLY_CLOCK))
    client = make_client(services, watchlist.router)
    market = StableMarket()

    def post(data: bytes) -> Any:
        return client.post("/api/watchlist", files={"file": ("w.csv", data, "text/csv")})

    assert post(b"ticker\nAAPL\nMSFT\n").json()["watchlist"]["session_date"] == TARGET.isoformat()
    assert post(b"ticker\nAMD\nBF-B\n").status_code == 200  # replaces, no merge
    finviz = FakeFinviz(["NVDA"])
    detail = await run_nightly(deps(db_factory, finviz, market), TARGET)
    assert finviz.calls == 0 and detail["source"] == "manual"
    assert set(snapshot(db_factory, TARGET)) == {"AMD", "BF.B", "SPY"}

    assert client.delete(f"/api/watchlist/{TARGET.isoformat()}").status_code == 200
    finviz = FakeFinviz(["NVDA"])
    detail = await run_nightly(deps(db_factory, finviz, market), TARGET)
    assert finviz.calls == 1 and detail["source"] == "finviz"
    assert set(snapshot(db_factory, TARGET)) == {"NVDA", "SPY"}

    rows = _audits(db_factory, "watchlist.")
    assert [(a.action, a.actor) for a in rows] == [
        ("watchlist.upload", "web:stephen"),
        ("watchlist.upload", "web:stephen"),
        ("watchlist.delete", "web:stephen"),
    ]
    first, second, gone = rows
    assert first.before is None and first.after["tickers"] == ["AAPL", "MSFT"]
    assert second.before["tickers"] == ["AAPL", "MSFT"] and second.after["tickers"] == ["AMD", "BF.B"]
    assert gone.before["tickers"] == ["AMD", "BF.B"] and gone.after is None


# ==========================================================================================================
# P4-T17: the Docker files and scripts
# ==========================================================================================================

SECRETS = {
    "DATABASE_URL": "postgresql+psycopg://app:BrkApp-5511@db/x",
    "MIGRATION_DATABASE_URL": "postgresql+psycopg://own:BrkOwner-7722@db/x",
    "APP_ENCRYPTION_KEY": "BrkEncKey-9933",
    "SESSION_SECRET": "BrkSession-4444",
    "ADMIN_USERNAME": "stephen",
    "ADMIN_PASSWORD_INITIAL": "BrkAdminPw-6655",
}
SECRET_BITS = ("BrkApp-5511", "BrkOwner-7722", "BrkEncKey-9933", "BrkSession-4444", "BrkAdminPw-6655")


def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text(f'#!/bin/bash\necho "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _no_secret(text: str) -> None:
    for bit in SECRET_BITS:
        assert bit not in text, bit


@dataclass
class DeployRun:
    code: int
    out: str


def _deploy(tmp_path: Path, lines: list[str], **env: str) -> DeployRun:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    _stub(stubs, "docker", 'if [ "$3" = "build" ] && [ -n "${STUB_BUILD_FAIL:-}" ]; then exit 1; fi\nexit 0')
    _stub(stubs, "ssh", "cat > /dev/null\nexit 0")
    _stub(stubs, "curl", 'printf "%s" 200')
    _stub(stubs, "sleep", "exit 0")
    env_file = tmp_path / "env.breaker"
    env_file.write_text("".join(f"{line}\n" for line in lines))
    run_env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "TRADER_ENV_FILE": str(env_file),
        **env,
    }
    result = subprocess.run(
        ["bash", str(DOCKER / "deploy.sh"), "dev"],
        env=run_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    return DeployRun(result.returncode, result.stdout + result.stderr + log)


def test_deploy_never_echoes_env_values_and_names_only_the_missing_key(tmp_path: Path) -> None:
    full = [f"{k}={v}" for k, v in SECRETS.items()]
    (tmp_path / "ok").mkdir()
    ok = _deploy(tmp_path / "ok", full)
    assert ok.code == 0, ok.out
    _no_secret(ok.out)

    (tmp_path / "fail").mkdir()
    failed = _deploy(tmp_path / "fail", full, STUB_BUILD_FAIL="1")
    assert failed.code != 0
    _no_secret(failed.out)

    cases = {
        "empty": [line if not line.startswith("SESSION_SECRET=") else "SESSION_SECRET=" for line in full],
        "prefixed": [
            line if not line.startswith("SESSION_SECRET=") else "XSESSION_SECRET=v1" for line in full
        ],
        "commented": [
            line if not line.startswith("SESSION_SECRET=") else "# SESSION_SECRET=v2" for line in full
        ],
    }
    for name, lines in cases.items():
        (tmp_path / name).mkdir()
        run = _deploy(tmp_path / name, lines)
        assert run.code == 1, (name, run.out)
        assert "SESSION_SECRET" in run.out, name
        _no_secret(run.out)
        assert "build" not in run.out.split("SESSION_SECRET", 1)[1], name  # stopped before building

    (tmp_path / "export").mkdir()
    exported = _deploy(
        tmp_path / "export",
        [f"export {line}" if line.startswith("SESSION_SECRET") else line for line in full],
    )
    assert exported.code == 0, exported.out
    _no_secret(exported.out)


def _entry_stubs(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    presence = (
        'me="$(basename "$0")"\n'
        "for k in MIGRATION_DATABASE_URL ADMIN_PASSWORD_INITIAL DATABASE_URL; do\n"
        '  if [ -n "${!k+x}" ]; then echo "$me has $k" >> "$STUB_LOG"\n'
        '  else echo "$me lacks $k" >> "$STUB_LOG"; fi\n'
        "done\nexit 0"
    )
    alembic = 'if [ "$3" = "current" ]; then echo "0005 (head)"; fi\n' + presence
    _stub(stubs, "alembic", alembic)
    _stub(stubs, "trader", presence)
    _stub(stubs, "python", presence)
    _stub(stubs, "supervisord", presence)
    _stub(stubs, "run-worker.sh", presence)
    _stub(stubs, "sleep", "exit 0")
    return {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "HOME": str(tmp_path / "home"),
        "TRADER_DOCKER_DIR": str(stubs),
        **{k: v for k, v in SECRETS.items() if k != "ADMIN_USERNAME"},
    }


@pytest.mark.parametrize("mode", ["all", "api", "worker"])
def test_entrypoint_unsets_the_owner_url_and_admin_password_before_the_long_running_process(
    tmp_path: Path, mode: str
) -> None:
    env = _entry_stubs(tmp_path)
    result = subprocess.run(
        ["bash", str(DOCKER / "entrypoint.sh"), mode], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    _no_secret(result.stdout + result.stderr)
    calls = (tmp_path / "calls.log").read_text().splitlines()
    final = {"all": "supervisord", "api": "python", "worker": "run-worker.sh"}[mode]
    lines = [c for c in calls if c.split(" ")[0].endswith(final) and (" has " in c or " lacks " in c)]
    assert any(c.endswith("lacks MIGRATION_DATABASE_URL") for c in lines), calls
    assert any(c.endswith("lacks ADMIN_PASSWORD_INITIAL") for c in lines), calls
    assert any(c.endswith("has DATABASE_URL") for c in lines), calls
    if mode != "worker":  # the migration and the admin bootstrap still got what they need
        assert any(
            c.split(" ")[0].endswith("alembic") and c.endswith("has MIGRATION_DATABASE_URL") for c in calls
        )
        assert any(
            c.split(" ")[0].endswith("trader") and c.endswith("has ADMIN_PASSWORD_INITIAL") for c in calls
        )
    else:
        assert not any(c.startswith("alembic") or c.startswith("trader ") for c in calls)


STUB_WORKER = """#!/bin/bash
echo "python $*" >> "$STUB_LOG"
if [ -n "${STUB_WAIT:-}" ]; then
  trap 'echo TERM >> "$STUB_LOG"; exit "${STUB_TERM_EXIT:-0}"' TERM
  trap 'echo INT >> "$STUB_LOG"; exit 99' INT
  echo started >> "$STUB_LOG"
  i=0
  while [ "$i" -lt 600 ]; do /bin/sleep 0.1; i=$((i + 1)); done
fi
exit "${STUB_EXIT:-0}"
"""


def _worker_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    path = stubs / "python"
    path.write_text(STUB_WORKER)
    path.chmod(0o755)
    return {"PATH": f"{stubs}:/usr/bin:/bin", "STUB_LOG": str(tmp_path / "calls.log"), **extra}


def _wait_for(path: Path, needle: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared in {path}")


def test_run_worker_exit_codes_and_signal_forwarding(tmp_path: Path) -> None:
    script = str(DOCKER / "run-worker.sh")
    # 0 and 4 return at once; 2 and 3 wait WORKER_RESTART_DELAY (a real 1 s sleep here)
    # P5-RC ruling: "at once" allows 5.0 s (0.9 s failed under full-suite load, a wall-clock flake)
    for code, min_s, max_s in ((0, 0.0, 5.0), (4, 0.0, 5.0), (2, 0.9, 10.0), (3, 0.9, 10.0)):
        case = tmp_path / f"code{code}"
        case.mkdir()
        env = _worker_env(case, STUB_EXIT=str(code), WORKER_RESTART_DELAY="1")
        start = time.monotonic()
        result = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=30)
        took = time.monotonic() - start
        assert result.returncode == code, (code, result.stderr)
        assert min_s <= took <= max_s, (code, took)

    # a stop during the 2/3 restart delay ends the script at once with the worker's code, no restart
    case = tmp_path / "stop_in_delay"
    case.mkdir()
    out = case / "out.txt"
    env = _worker_env(case, STUB_EXIT="2", WORKER_RESTART_DELAY="30")
    with out.open("w") as fh:
        proc = subprocess.Popen(["bash", script], env=env, stdout=fh, stderr=fh, start_new_session=True)
        _wait_for(out, "restarting in 30s")
        start = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=15)
    assert code == 2 and time.monotonic() - start < 10
    assert (case / "calls.log").read_text().splitlines() == ["python -m trader.worker"]

    # INT (Ctrl-C, or a stop signal of INT) reaches the worker as TERM, and its exit code is returned
    case = tmp_path / "int"
    case.mkdir()
    env = _worker_env(case, STUB_WAIT="1", STUB_TERM_EXIT="0", WORKER_RESTART_DELAY="30")
    proc = subprocess.Popen(
        ["bash", script],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for(case / "calls.log", "started")
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=15) == 0
    assert "TERM" in (case / "calls.log").read_text().splitlines()


def _supervisord() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(DOCKER / "supervisord.conf")
    return parser


def _seconds(value: object) -> int:
    match = re.fullmatch(r"(\d+)(s|m)?", str(value))
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def test_worker_stop_time_and_compose_grace_in_every_compose_file() -> None:
    from annotated_types import Le

    from trader.worker import BOT_STOP_GRACE_SECONDS, RELAY_STOP_SECONDS

    sv = _supervisord()
    waits = {p: int(sv[f"program:{p}"]["stopwaitsecs"]) for p in ("api", "worker", "cron")}
    poll_max = next(
        c.le
        for c in RuntimeSettings.model_fields["telegram_poll_timeout_seconds"].metadata
        if isinstance(c, Le)
    )
    worker = sv["program:worker"]
    assert waits["worker"] >= 60
    assert waits["worker"] >= max(RELAY_STOP_SECONDS, int(poll_max) + BOT_STOP_GRACE_SECONDS)
    assert worker["stopsignal"] == "TERM" and worker["autorestart"] == "true"
    assert int(worker["startretries"]) >= 100  # exit 2 loops must never exhaust supervisord's retries
    for name in ("docker-compose.dev.yml", "docker-compose.prod.yml"):
        svc = yaml.safe_load((DOCKER / name).read_text())["services"]["trader"]
        assert _seconds(svc["stop_grace_period"]) >= sum(waits.values()), name
        assert svc.get("init") is True, name
    smoke = yaml.safe_load((DOCKER / "docker-compose.smoke.yml").read_text())["services"]["trader"]
    assert smoke["command"] == ["api"] and _seconds(smoke["stop_grace_period"]) >= waits["api"]


def test_compose_files_never_publish_ports_and_join_the_external_proxy_network() -> None:
    for name, container in (("docker-compose.dev.yml", "trader-dev"), ("docker-compose.prod.yml", "trader")):
        doc = yaml.safe_load((DOCKER / name).read_text())
        assert set(doc["services"]) == {"trader"}, name
        svc = doc["services"]["trader"]
        assert svc["container_name"] == container
        for forbidden in (
            "ports",
            "network_mode",
            "privileged",
            "build",
            "cap_add",
            "pid",
            "ipc",
            "userns_mode",
        ):
            assert forbidden not in svc, (name, forbidden)
        assert svc["read_only"] is True and "proxy" in svc["networks"]
        assert doc["networks"]["proxy"] == {"external": True}
        assert set(svc.get("environment", {})) == {"APP_ENV"}  # secrets only through the env file
        assert "volumes" in svc and all(not str(v).startswith(("/", ".")) for v in svc["volumes"])
    smoke = yaml.safe_load((DOCKER / "docker-compose.smoke.yml").read_text())
    for svc_name, svc in smoke["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (svc_name, port)
        assert "network_mode" not in svc and "proxy" not in svc.get("networks", [])


def test_crontab_lines_are_the_trunk_schedule_and_real_cli_commands() -> None:
    text = (DOCKER / "crontab").read_text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert lines[0] == "CRON_TZ=America/New_York"
    entries = [re.split(r"\s+", ln, maxsplit=5) for ln in lines[1:]]
    schedule = {(int(e[1]), int(e[0]), " ".join(e[5].split())) for e in entries}  # (hour, minute, command)
    for hour, minute, command in (
        (12, 32, "trader event --due"),
        (12, 55, "trader event flatten"),
        (12, 58, "trader event flatten"),
        (15, 32, "trader event --due"),
        (15, 55, "trader event flatten"),
        (15, 58, "trader event flatten"),
        (9, 36, "trader event orb_open"),
        (20, 0, "trader nightly"),
        (2, 0, "trader token-refresh"),
    ):
        assert (hour, minute, command) in schedule, (hour, minute, command)
    runner = CliRunner()
    for *_, command in entries:
        argv = command.split()
        assert argv[0] == "trader", command
        result = runner.invoke(cli_app, [*argv[1:], "--help"])
        assert result.exit_code == 0, (command, result.output)
    # the image runs exactly this file, in ET
    assert "/app/docker/crontab" in _supervisord()["program:cron"]["command"]
    dockerfile = (DOCKER / "Dockerfile").read_text()
    assert re.search(r"COPY[^\n]*docker/crontab[^\n]*/app/docker/", dockerfile)
    assert re.search(r"apt-get install[^\n]*tzdata", dockerfile.replace("\\\n", " "))
