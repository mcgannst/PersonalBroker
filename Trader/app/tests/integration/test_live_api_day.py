"""DB-T12 acceptance tests 1-3 (live dashboard plan): the whole feature on a seeded "normal day", end to end
through the real routes, the production service composition and a testcontainer database.

The normal day (plan DB-T12 Behaviour): a live run started 40 sessions ago with 60 closed trades, one
prior-close equity snapshot per session plus fill snapshots (about 180 rows), 20 open positions (the D2
maximum) with `quote_marks` and 120 minutes of `mark_bars` each, 543 candidates with 12 passed, 1,000
`decision_log` rows for today, 2,000 `event_log` rows, 30 `job_runs` for today, 5 kill-switch events, and
(test 3) a replay run with its own rows on the same day.

1. Performance: the median of 10 `GET /api/live` (after 2 warm-ups), read from the route's own
   `Server-Timing` `app;dur`, is under 300 ms (today, and `?range=run&expand=<3 ids>`); every request runs at
   most `LIVE_MAX_STATEMENTS` statements, the same number as on the day trimmed to 1 open position and 10
   activity items; `GET /api/control` (soak cache warm) median under 1 s. The numbers are printed.
2. No Questrade: both routes, every parameter variant, with the services' `quotes`/`candles`, the client
   builder and the client's market-data methods all failing the test if called.
3. Numbers end to end: periods equal hand-computed values, books check ok, 20 positions, 100 activity items,
   rejections by rule, and a replay run's rows change nothing.

Timing under the shared gate: the gate runs 2 lanes x 4 xdist workers, which inflates wall time several-fold.
A series whose median misses the budget is measured again (at most `SERIES_TRIES` series of 10); the test
passes when one series' median is under the budget, and every series is printed. Parallel-safe: every
database access goes through this worker's own `db_factory`; the in-process soak cache is cleared around
each test.
"""

import asyncio
import dataclasses
import statistics
import time
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as rt
from tests.api.conftest import make_client
from tests.api.test_trading import _proposal, _signal, at_et
from tests.factories import add_run, add_symbol
from tests.fakes_api import test_core
from tests.live.test_activity_db import _symbols
from tests.live.test_live_data_db import (
    _catalyst,
    _weekly,
    fees,
    ledger,
    open_position,
    round_trip,
    seed_run,
)
from trader.adapters.questrade.client import QuestradeClient
from trader.api.deps import ApiServices
from trader.api.livedata import health
from trader.api.livedata.types import LIVE_MAX_STATEMENTS
from trader.api.routers import control as control_router
from trader.api.routers import live
from trader.api.services import build_services
from trader.bootstrap import Core
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # a Tuesday session (EDT)
NOW = at_et(DAY, 14, 0)
STARTING = Decimal("10000.0000")
BUDGET_MS = 300.0
CONTROL_BUDGET_MS = 1000.0
SERIES_TRIES = 3
REQUESTS = 10
WARMUPS = 2


def _sessions() -> list[date]:
    """The run's 41 sessions: [0] is the session the run started (40 sessions ago), [40] is DAY."""
    days = [DAY]
    while len(days) < 41:
        days.append(CAL.previous_session(days[-1]))
    return days[::-1]


SESSIONS = _sessions()


# --- the production composition -----------------------------------------------------------------------------


@dataclass
class Prod:
    core: Core
    clock: FixedClock
    services: ApiServices
    questrade: list[str]

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory

    def client(self) -> TestClient:
        return make_client(self.services, live.router, control_router.router, raise_server_exceptions=False)


@pytest.fixture
def prod(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Prod]:
    """`build_services` (the API process) on a test Core. Every way to Questrade fails the test and is
    recorded: the client builder, the client's market-data methods, and the services' `quotes`/`candles`."""
    health.clear_soak_cache()
    calls: list[str] = []

    def no_client(core: Core) -> Any:
        calls.append("questrade_client")
        raise AssertionError("the dashboard opened a Questrade client")

    def forbidden(name: str) -> Callable[..., Any]:
        async def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"the dashboard called Questrade {name}")

        return call

    monkeypatch.setattr(rt, "questrade_client", no_client)
    for name in ("quotes", "candles", "candles_many"):
        monkeypatch.setattr(QuestradeClient, name, forbidden(f"client.{name}"))
    clock = FixedClock(NOW)
    core = test_core(db_factory, clock)
    loop = asyncio.new_event_loop()
    stack = AsyncExitStack()
    try:
        built = loop.run_until_complete(build_services(core, stack))
        services = dataclasses.replace(
            built, quotes=forbidden("services.quotes"), candles=forbidden("services.candles")
        )
        yield Prod(core, clock, services, calls)
    finally:
        loop.run_until_complete(stack.aclose())
        loop.close()
        health.clear_soak_cache()


def _statements(factory: sessionmaker[Session], fn: Callable[[], Any]) -> tuple[Any, list[str]]:
    engine = factory.kw["bind"]
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        out = fn()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return out, seen


def _timings(resp: Any) -> list[tuple[str, float]]:
    """The `Server-Timing` entries as (name, ms), in order (`app` first)."""
    out: list[tuple[str, float]] = []
    for entry in resp.headers["server-timing"].split(","):
        name, _, dur = entry.strip().partition(";dur=")
        out.append((name, float(dur)))
    return out


# --- the normal day -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Day:
    run_id: int
    position_ids: list[int]
    replay_symbol: int
    cfg: int


def trade_spec(today_trades: int = 2) -> list[tuple[int, date, str]]:
    """(index, session, exit price) of the closed trades: two per session back from DAY over 30 sessions, each
    10 shares bought at 10.00 with 0.35 of fees per side; every third (index % 3 == 0) exits at 9.80, the
    rest at 10.50. `today_trades` = 1 drops DAY's second trade (the trimmed day)."""
    out = []
    for i in range(60):
        if today_trades == 1 and i == 1:
            continue
        out.append((i, SESSIONS[40 - i // 2], "9.80" if i % 3 == 0 else "10.50"))
    return out


def _snapshot(s: Session, run_id: int, ts: datetime) -> None:
    s.add(
        m.EquitySnapshot(
            run_id=run_id,
            ts=ts,
            equity=STARTING,
            cash=STARTING,
            settled_cash=STARTING,
            peak_equity=STARTING,
            drawdown_pct=Decimal(0),
        )
    )


def seed_normal_day(
    prod: Prod,
    *,
    prefix: str = "N",
    positions_n: int = 20,
    today_trades: int = 2,
    alerts: int = 100,
    failed_jobs: int = 6,
    weekly: bool = True,
) -> Day:
    """The plan's normal day for a new active live run (tickers under `prefix`, so a second day can follow a
    retired first one in the same database)."""
    cfg = prod.services.registry.current("orb_sip").id
    with prod.factory() as s:
        run_id = seed_run(s, started_at=at_et(SESSIONS[0], 8, 0), cash=STARTING)
        hist = add_symbol(s, f"{prefix}HIST")
        # 60 closed trades, each with a fill snapshot at entry and exit (120 rows)
        for i, day, exit_ in trade_spec(today_trades):
            opened = at_et(day, 9, 40 + i % 2)
            closed = at_et(day, 10, i % 2)
            round_trip(
                s,
                run_id,
                hist,
                entry="10.00",
                exit_=exit_,
                qty=10,
                opened=opened,
                closed=closed,
                entry_fees=fees("0.35"),
                exit_fees=fees("0.35"),
                pnl_r="-0.4000" if i % 3 == 0 else "0.5000",
            )
            _snapshot(s, run_id, opened)
            _snapshot(s, run_id, closed)
        # one prior-close snapshot per past session (40 rows)
        for day in SESSIONS[:-1]:
            _snapshot(s, run_id, at_et(day, 16, 1))
        # 20 open positions entered today (fee 0.35), each with a live mark 0.40 above the entry, a fill
        # snapshot, and 120 minutes of mark bars
        position_ids: list[int] = []
        for i, sym in enumerate(_symbols(s, f"{prefix}P", positions_n)):
            price = Decimal("20.00") + i
            opened = at_et(DAY, 9, 50) + timedelta(seconds=i)
            pid = open_position(
                s,
                run_id,
                sym,
                qty=10 + i,
                price=price,
                ts=opened,
                fees=fees("0.35"),
                stop=price - Decimal("0.50"),
            )
            pos = s.get(m.Position, pid)
            assert pos is not None
            pos.strategy_config_id = cfg
            position_ids.append(pid)
            _snapshot(s, run_id, opened)
            mark = price + Decimal("0.40")
            s.add(
                m.QuoteMark(
                    run_id=run_id,
                    symbol_id=sym,
                    bid=mark - Decimal("0.01"),
                    ask=mark + Decimal("0.01"),
                    last=mark,
                    quote_time=NOW - timedelta(seconds=4),
                    observed_at=NOW - timedelta(seconds=3),
                    written_at=NOW - timedelta(seconds=2),
                    is_halted=False,
                )
            )
            start = at_et(DAY, 9, 50)
            s.execute(
                insert(m.MarkBar),
                [
                    {
                        "run_id": run_id,
                        "symbol_id": sym,
                        "minute_start": start + timedelta(minutes=k),
                        "open": price,
                        "high": price + Decimal("0.50"),
                        "low": price - Decimal("0.05"),
                        "close": price + Decimal("0.01") * (k % 40),
                        "samples": 30,
                        "updated_at": start + timedelta(minutes=k + 1),
                    }
                    for k in range(120)
                ],
            )
        # Claude spend: catalysts today, Monday and 20 sessions ago, and a weekly report updated last Saturday
        _catalyst(s, hist, DAY, "0.10")
        _catalyst(s, hist, SESSIONS[39], "0.20")
        _catalyst(s, hist, SESSIONS[20], "0.30")
        if weekly:  # one per week_ending (the primary key), so only for the first day of a database
            _weekly(s, run_id, date(2026, 10, 2), at_et(date(2026, 10, 3), 12, 0), "0.40")
        # 2,000 event_log rows: `alerts` error rows of this run today (activity items), the rest older
        s.execute(
            insert(m.EventLog),
            [
                {
                    "ts": at_et(DAY, 11, 0) + timedelta(seconds=i),
                    "level": "error",
                    "source": "engine",
                    "run_id": run_id,
                    "message": f"alert {i}",
                    "data": None,
                }
                for i in range(alerts)
            ]
            + [
                {
                    "ts": at_et(SESSIONS[39], 20, 0) - timedelta(minutes=i),  # before today
                    "level": ("info", "warning", "error")[i % 3],
                    "source": "engine" if i % 2 else "log.worker",
                    "run_id": run_id if i % 4 else None,
                    "message": f"old {i}",
                    "data": None,
                }
                for i in range(2000 - alerts)
            ],
        )
        # 543 candidates (12 passed), and 1,000 decision-log rows today: 800 scan rejections over 3 rules
        # (500 / 200 / 100) and 200 passes
        cands = _symbols(s, f"{prefix}K", 800)
        s.execute(
            insert(m.Candidate),
            [
                {
                    "run_id": run_id,
                    "session_date": DAY,
                    "strategy_key": "orb_sip",
                    "symbol_id": sid,
                    "rank": i + 1 if i < 12 else None,
                    "passed": i < 12,
                    "reject_reason": None if i < 12 else "rvol_below_min",
                    "created_at": at_et(DAY, 9, 35) + timedelta(milliseconds=i),
                }
                for i, sid in enumerate(cands[:543])
            ],
        )
        rules = ["rvol_below_min"] * 500 + ["no_opening_bar"] * 200 + ["outside_top_n"] * 100
        rows: list[dict[str, Any]] = [
            {
                "stage": "scan",
                "outcome": "rejected",
                "rule": r,
                "symbol_id": sid,
                "ticker": f"{prefix}K{i:03d}",
            }
            for i, (sid, r) in enumerate(zip(cands, rules, strict=True))
        ]
        rows += [
            {
                "stage": "scan",
                "outcome": "passed",
                "rule": None,
                "symbol_id": cands[i],
                "ticker": f"{prefix}K{i:03d}",
            }
            for i in range(200)
        ]
        s.execute(
            insert(m.DecisionLog),
            [
                {
                    "run_id": run_id,
                    "session_date": DAY,
                    "seq": i + 1,
                    "ts": at_et(DAY, 9, 35),
                    "recorded_at": at_et(DAY, 9, 40),
                    "final": False,
                    "ref": {},
                    "data": {},
                    **r,
                }
                for i, r in enumerate(rows)
            ],
        )
        # 30 job runs today, `failed_jobs` of them failed
        for i in range(30):
            failed = i < failed_jobs
            s.add(
                m.JobRun(
                    job=f"event:job{i}",
                    session_date=DAY,
                    started_at=at_et(DAY, 8, 0) + timedelta(minutes=i),
                    finished_at=at_et(DAY, 8, 1) + timedelta(minutes=i),
                    status="failed" if failed else "succeeded",
                    error="boom" if failed else None,
                    detail={"n": i},
                )
            )
        # 5 kill-switch events on earlier sessions, each reset (history on Control; nothing blocks today)
        for k, switch in enumerate(
            ("daily_loss_pct", "expectancy", "manual_pause", "daily_loss_pct", "max_drawdown_pct")
        ):
            day = SESSIONS[30 + k]
            s.add(
                m.KillSwitchEvent(
                    run_id=run_id,
                    switch=switch,
                    session_date=day,
                    tripped_at=at_et(day, 11, 0),
                    value=Decimal("0.06") if switch != "manual_pause" else None,
                    threshold=Decimal("0.05") if switch != "manual_pause" else None,
                    reset_at=at_et(day, 12, 0),
                    reset_reason="checked",
                    reset_by="web:stephen",
                )
            )
        # a fresh worker heartbeat
        s.merge(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=NOW - timedelta(hours=6),
                beat_at=NOW - timedelta(seconds=5),
                session_date=DAY,
                phase="session",
                detail={
                    "marks": {
                        "written_at": (NOW - timedelta(seconds=2)).isoformat(),
                        "symbols": 20,
                        "failing": False,
                    }
                },
            )
        )
        replay_symbol = add_symbol(s, f"{prefix}RPL")
        s.commit()
    return Day(run_id, position_ids, replay_symbol, cfg)


def seed_replay(prod: Prod, day: Day) -> None:
    """A replay run with its own rows of every kind on DAY: trades, an open position with a mark and bars, a
    pending proposal, an active manual pause, a snapshot, candidates, decision-log rows and events, some on a
    symbol the live run holds."""
    live_sym = day.replay_symbol
    with prod.factory() as s:
        held = s.execute(
            select(m.Position.symbol_id).where(m.Position.id == day.position_ids[0])
        ).scalar_one()
        replay = add_run(s, mode="replay", status="running", started_at=at_et(DAY, 6, 0))
        s.add(
            m.SimAccount(
                run_id=replay,
                currency="USD",
                starting_cash=Decimal("777"),
                source_amount=Decimal("777"),
                source_currency="USD",
                created_at=at_et(DAY, 6, 0),
            )
        )
        ledger(s, replay, at_et(DAY, 6, 0), Decimal("777"), "deposit", "deposit")
        s.flush()
        for sym in (held, live_sym):
            pid = open_position(
                s, replay, sym, qty=3, price=Decimal("50.00"), ts=at_et(DAY, 9, 45), fees=fees("0.35")
            )
            round_trip(
                s,
                replay,
                sym,
                entry="50.00",
                exit_="90.00",
                qty=3,
                opened=at_et(DAY, 9, 50),
                closed=at_et(DAY, 10, 5),
                entry_fees=fees("0.35"),
                exit_fees=fees("0.35"),
                pnl_r="9.0000",
            )
            s.add(
                m.QuoteMark(
                    run_id=replay,
                    symbol_id=sym,
                    bid=Decimal("99.98"),
                    ask=Decimal("100.00"),
                    last=Decimal("99.99"),
                    observed_at=NOW - timedelta(seconds=1),
                    written_at=NOW - timedelta(seconds=1),
                    is_halted=False,
                )
            )
            s.execute(
                insert(m.MarkBar),
                [
                    {
                        "run_id": replay,
                        "symbol_id": sym,
                        "minute_start": at_et(DAY, 9, 40) + timedelta(minutes=k),
                        "open": Decimal("99"),
                        "high": Decimal("99.5"),
                        "low": Decimal("98.5"),
                        "close": Decimal("99.2"),
                        "samples": 1,
                        "updated_at": at_et(DAY, 9, 41) + timedelta(minutes=k),
                    }
                    for k in range(60)
                ],
            )
            sig = _signal(s, replay, day.cfg, sym, DAY, at_et(DAY, 9, 35))
            _proposal(
                s, replay, sig, sym, kind="entry", created_at=NOW - timedelta(seconds=10), status="pending"
            )
            _proposal(s, replay, sig, sym, kind="exit", created_at=at_et(DAY, 10, 0), position_id=pid)
            s.add(
                m.Candidate(
                    run_id=replay,
                    session_date=DAY,
                    strategy_key="orb_sip",
                    symbol_id=sym,
                    passed=False,
                    reject_reason="replay_rule",
                    created_at=at_et(DAY, 9, 35),
                )
            )
        s.add(
            m.KillSwitchEvent(
                run_id=replay, switch="manual_pause", session_date=DAY, tripped_at=at_et(DAY, 9, 0)
            )
        )
        _snapshot(s, replay, at_et(DAY, 9, 0))
        s.add(
            m.DecisionLog(
                run_id=replay,
                session_date=DAY,
                seq=1,
                stage="scan",
                symbol_id=live_sym,
                ticker="RPL",
                outcome="rejected",
                rule="replay_rule",
                ts=at_et(DAY, 9, 36),
                recorded_at=at_et(DAY, 9, 40),
            )
        )
        for level in ("critical", "error", "warning"):
            s.add(
                m.EventLog(
                    ts=at_et(DAY, 12, 0),
                    level=level,
                    source="engine",
                    run_id=replay,
                    message=f"replay {level}",
                )
            )
        s.commit()


# --- 1. the performance budget ------------------------------------------------------------------------------


def _series(client: TestClient, path: str, report: list[str], budget_ms: float) -> float:
    """At most SERIES_TRIES series of REQUESTS requests (after WARMUPS); the best median. Every series is
    reported with its per-part Server-Timing (the slowest request's)."""
    for _ in range(WARMUPS):
        assert client.get(path).status_code == 200
    best = float("inf")
    for attempt in range(1, SERIES_TRIES + 1):
        timings: list[float] = []
        slowest: list[tuple[str, float]] = []
        for _ in range(REQUESTS):
            resp = client.get(path)
            assert resp.status_code == 200, resp.text
            assert resp.json()["part_errors"] == [], resp.json()["part_errors"]
            entries = _timings(resp)
            assert entries[0][0] == "app", entries
            ms = entries[0][1]
            if not slowest or ms > slowest[0][1]:
                slowest = entries
            timings.append(ms)
        median = statistics.median(timings)
        best = min(best, median)
        parts = ", ".join(f"{n} {d:.1f}" for n, d in slowest[1:])
        report.append(
            f"{path} series {attempt}: median {median:.1f} ms, min {min(timings):.1f}, max {max(timings):.1f}"
            f" (slowest request's parts: {parts})"
        )
        if median < budget_ms:
            break
    return best


def _control_series(client: TestClient, report: list[str]) -> float:
    """GET /api/control wall time (perf_counter around the in-process request: it has no Server-Timing), with
    the soak cache warm."""
    for _ in range(WARMUPS):
        assert client.get("/api/control").status_code == 200
    best = float("inf")
    for attempt in range(1, SERIES_TRIES + 1):
        timings: list[float] = []
        for _ in range(REQUESTS):
            t0 = time.perf_counter()
            resp = client.get("/api/control")
            timings.append((time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200 and resp.json()["part_errors"] == [], resp.text[:500]
        median = statistics.median(timings)
        best = min(best, median)
        report.append(f"/api/control series {attempt}: median {median:.1f} ms, max {max(timings):.1f}")
        if median < CONTROL_BUDGET_MS:
            break
    return best


def test_1_live_is_under_300_ms_with_a_statement_count_that_does_not_grow(prod: Prod) -> None:
    client = prod.client()
    report: list[str] = []
    # the trimmed day first: 1 open position and 10 activity items (1 entry order and fill, 1 round trip
    # today = 5, the 09:35 scan, 1 failed job, 1 alert)
    trimmed = seed_normal_day(
        prod, prefix="T", positions_n=1, today_trades=1, alerts=1, failed_jobs=1, weekly=True
    )
    assert client.get("/api/live").status_code == 200  # warm-up
    small, small_sql = _statements(
        prod.factory, lambda: client.get(f"/api/live?expand={trimmed.position_ids[0]}")
    )
    sb = small.json()
    assert sb["part_errors"] == [] and sb["run_id"] == trimmed.run_id
    assert (len(sb["positions"]), len(sb["activity"])) == (1, 10), [a["kind"] for a in sb["activity"]]
    # retire it; the full normal day under a new active live run
    with prod.factory() as s:
        s.execute(update(m.Run).where(m.Run.id == trimmed.run_id).values(status="completed"))
        s.commit()
    day = seed_normal_day(prod, weekly=False)
    expand = ",".join(map(str, day.position_ids[:3]))
    assert client.get("/api/live").status_code == 200
    big, big_sql = _statements(prod.factory, lambda: client.get(f"/api/live?expand={expand}"))
    bb = big.json()
    assert bb["part_errors"] == [] and bb["run_id"] == day.run_id
    assert (len(bb["positions"]), len(bb["activity"])) == (20, 100)
    report.append(
        f"statements per GET /api/live: {len(big_sql)} on the normal day, {len(small_sql)} trimmed"
        f" (ceiling {LIVE_MAX_STATEMENTS})"
    )
    today = _series(client, "/api/live", report, BUDGET_MS)
    run = _series(client, f"/api/live?range=run&expand={expand}", report, BUDGET_MS)
    control = _control_series(client, report)
    print("\n".join(report))
    assert len(big_sql) == len(small_sql), report
    assert len(big_sql) <= LIVE_MAX_STATEMENTS, report
    assert today < BUDGET_MS and run < BUDGET_MS, report
    assert control < CONTROL_BUDGET_MS, report
    assert prod.questrade == []


# --- 2. no Questrade ----------------------------------------------------------------------------------------


def test_2_no_route_or_parameter_calls_questrade(prod: Prod) -> None:
    day = seed_normal_day(prod)
    client = prod.client()
    ids = day.position_ids
    paths = [
        "/api/live",
        "/api/live?range=today",
        "/api/live?range=run",
        f"/api/live?expand={ids[0]}",
        f"/api/live?expand={ids[0]},{ids[1]},{ids[2]}",
        f"/api/live?range=run&expand={ids[5]},{ids[19]}",
        "/api/control",
    ]
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text[:500])
        assert resp.json()["part_errors"] == [], (path, resp.json()["part_errors"])
    assert prod.questrade == []


# --- 3. the numbers end to end ------------------------------------------------------------------------------


def _period(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(p for p in body["periods"] if p["period"] == key)


def _hand_numbers() -> dict[str, dict[str, Decimal | int]]:
    """The periods from the seed's own arithmetic (not the app's): per trade (exit - 10.00) x 10 - 0.70; the
    open value per position (0.40 x qty - the 0.35 entry fee); fees 0.35 per fill in the period; Claude spend
    by date."""
    week_start = DAY - timedelta(days=DAY.weekday())
    spans = {"today": (DAY, DAY), "week": (week_start, DAY), "run": (SESSIONS[0], DAY)}
    unrealized = sum((Decimal("0.40") * (10 + i) - Decimal("0.35") for i in range(20)), Decimal(0))
    claude = {DAY: Decimal("0.10"), SESSIONS[39]: Decimal("0.20"), SESSIONS[20]: Decimal("0.30")}
    weekly = (date(2026, 10, 3), Decimal("0.40"))
    out: dict[str, dict[str, Decimal | int]] = {}
    for key, (lo, hi) in spans.items():
        trades = [(i, exit_) for i, d, exit_ in trade_spec() if lo <= d <= hi]
        pnls = [(Decimal(exit_) - Decimal("10.00")) * 10 - Decimal("0.70") for _, exit_ in trades]
        realized = sum(pnls, Decimal(0))
        fee = Decimal("0.70") * len(trades) + Decimal("0.35") * 20  # the 20 entries are all today
        spend = sum((v for d, v in claude.items() if lo <= d <= hi), Decimal(0))
        spend += weekly[1] if lo <= weekly[0] <= hi else Decimal(0)
        out[key] = {
            "realized": realized,
            "unrealized": unrealized,
            "pnl_after_fees": realized + unrealized,
            "fees": fee,
            "claude_usd": spend,
            "net_after_ai": realized + unrealized - spend,
            "trades": len(trades),
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p < 0),
        }
    return out


def test_3_the_numbers_end_to_end_and_a_replay_changes_nothing(prod: Prod) -> None:
    day = seed_normal_day(prod)
    client = prod.client()
    body = client.get("/api/live").json()
    assert body["part_errors"] == []
    assert body["session_day"] == DAY.isoformat() and body["run_id"] == day.run_id

    # the periods, against the seed's own arithmetic and against literal hand sums
    hand = _hand_numbers()
    for key in ("today", "week", "run"):
        block = _period(body, key)
        for field, want in hand[key].items():
            got = block[field] if isinstance(want, int) else Decimal(block[field])
            assert got == want, (key, field, block[field], want)
        assert block["unrealized_partial"] is False
    literal = {
        "today": ("1.6000", "149.0000", "150.6000", "8.4000", "0.1000", "150.5000"),
        "week": ("3.2000", "149.0000", "152.2000", "9.8000", "0.3000", "151.9000"),
        "run": ("118.0000", "149.0000", "267.0000", "49.0000", "1.0000", "266.0000"),
    }
    money = ("realized", "unrealized", "pnl_after_fees", "fees", "claude_usd", "net_after_ai")
    for key, values in literal.items():
        assert tuple(Decimal(_period(body, key)[f]) for f in money) == tuple(map(Decimal, values)), key
    assert (_period(body, "run")["trades"], _period(body, "run")["wins"]) == (60, 40)
    assert (_period(body, "week")["date_from"], _period(body, "run")["date_from"]) == (
        "2026-10-05",
        SESSIONS[0].isoformat(),
    )
    assert Decimal(body["claude_today"]["spent_usd"]) == Decimal("0.10")
    # the S4 invariant (every position marked) and the books, exact
    assert Decimal(_period(body, "run")["pnl_after_fees"]) == Decimal(body["risk"]["equity"]) - STARTING
    books = body["books"]
    assert books["ok"] is True and Decimal(books["difference"]) == 0, books
    assert books["open_positions"] == 20
    assert Decimal(books["starting_cash"]) == STARTING
    assert Decimal(books["fees_paid"]) == Decimal("49.00")
    assert Decimal(books["realized_gross"]) == Decimal("160.00")  # 40 x 5.00 - 20 x 2.00

    # 20 positions, 100 activity items newest first, rejections by rule
    assert len(body["positions"]) == 20
    assert all(p["mark_state"] == "live" for p in body["positions"])
    assert len(body["activity"]) == 100
    times = [a["ts"] for a in body["activity"]]
    assert times == sorted(times, reverse=True)
    rej = body["rejections"]
    assert rej["total"] == 800 and rej["source"] == "decision_log"
    assert {(r["stage"], r["rule"]): r["count"] for r in rej["rules"]} == {
        ("scan", "rvol_below_min"): 500,
        ("scan", "no_opening_bar"): 200,
        ("scan", "outside_top_n"): 100,
    }
    assert all(len(r["tickers"]) <= 50 and r["truncated"] for r in rej["rules"])
    assert body["closed_today"] == 2 and body["worker_stale"] is False

    # a replay run's rows on the same day (and on a held symbol) change nothing, on either route
    paths = ("/api/live", "/api/live?range=run", f"/api/live?expand={day.position_ids[0]}", "/api/control")
    before = [client.get(p).json() for p in paths]
    seed_replay(prod, day)
    health.clear_soak_cache()
    after = [client.get(p).json() for p in paths]
    for body_ in (before[-1], after[-1]):  # the database round trip varies per request
        body_["health"]["db_latency_ms"] = None
    for path, b, a in zip(paths, before, after, strict=True):
        assert b["part_errors"] == [] and a["part_errors"] == [], path
        changed = sorted(k for k in b if b[k] != a[k] and k != "server_time")
        assert changed == [], f"{path}: the replay rows changed {changed}"
    assert prod.questrade == []
