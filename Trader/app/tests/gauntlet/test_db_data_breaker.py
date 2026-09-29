"""DB-GDATA gauntlet (attempt 1): breaker tests for the live dashboard's data (DB-T3 periods, books, equity;
DB-T4 positions, risk; DB-T5 activity, rejections, GET /api/live; DB-T6 GET /api/control).

Every route test runs the PRODUCTION composition: `trader.api.services.build_services` on a test Core (the
real day-plan builder, registry, kill switches, settings, credentials and market-data wrappers), with the
Questrade client builder and the client's market-data methods replaced by functions that fail the test and
record the call. Only the authentication dependency is overridden (tests/api/conftest.make_client).

B1-B2   read-only: /api/live and /api/control issue SELECTs only (no INSERT/UPDATE/DELETE, advisory lock or
        FOR UPDATE), in production wiring.
B3      a plug-in with a broken config: opening the page writes no (relayable) event.
B4      never Questrade: both routes, every parameter, with live, stale and missing marks.
B5      replay isolation: a replay run's rows on the SAME symbol and day change nothing in either response.
B6-B7   money: today/week/run across the DST week boundary with a position carried over the weekend, fees
        and Claude spend and net after AI to the cent; Thanksgiving and the weekend after.
B8      the books check flips on a 1-cent ledger mismatch and passes on exact books.
B9      unrealised uses the marks, flags partial, and the S4 invariant holds once every position is marked.
B10     the statement count is the same for 1 and 20 positions and for 10 and 100 activity items, and at
        most LIVE_MAX_STATEMENTS.
B11     the 300 ms budget on a seeded normal day (median of 10 Server-Timing app;dur).
B12     part isolation: each part failing on its own is null plus a masked part_errors entry.
B13     pending proposals add no statements on the normal day (which already sits at the ceiling).

B1-B3 and B13 fail on the code as built (findings F1-F4 of the gauntlet log); they are strict xfails so the
shared gate stays green, and the fix round removes the marks.

Parallel-safe: every database access goes through the worker's own `db_factory` (its own container); the
in-process soak cache is cleared around each test.
"""

import asyncio
import statistics
from collections.abc import Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as rt
from tests.api.conftest import make_client
from tests.api.test_trading import _proposal, _signal, at_et
from tests.factories import add_run, add_symbol
from tests.fakes_api import test_core
from tests.live.test_activity_db import _event, _symbols
from tests.live.test_live_data_db import (
    _catalyst,
    _weekly,
    et,
    fees,
    open_position,
    round_trip,
    seed_run,
)
from trader.adapters.questrade.client import QuestradeClient
from trader.api import views as api_views
from trader.api.deps import ApiServices
from trader.api.livedata import activity, books, control, equity, health, periods, positions, risk
from trader.api.livedata.types import LIVE_MAX_STATEMENTS
from trader.api.routers import control as control_router
from trader.api.routers import live
from trader.api.services import build_services
from trader.bootstrap import Core
from trader.broker.types import Fees
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # a Tuesday session (EDT)
NOW = at_et(DAY, 14, 0)
RUN_START = at_et(date(2026, 9, 1), 8, 0)
STARTING = Decimal("10000.0000")
BUDGET_MS = 300.0
SECRETS = ("Sup3rS3cretPW", "RT-abcdef123456", "abc123def456ghi")
BOOM = f"boom postgresql://trader:{SECRETS[0]}@db:5432/trader refresh_token={SECRETS[1]} Bearer {SECRETS[2]}"
REGISTRY_SOURCE = "strategies.registry"


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
    """`build_services` (the API process) on a test Core; any Questrade use fails and is recorded."""
    health.clear_soak_cache()
    calls: list[str] = []

    def no_client(core: Core) -> Any:
        calls.append("questrade_client")
        raise AssertionError("the dashboard opened a Questrade client")

    def forbidden(name: str) -> Callable[..., Any]:
        async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"the dashboard called Questrade {name}")

        return call

    monkeypatch.setattr(rt, "questrade_client", no_client)
    for name in ("quotes", "candles", "candles_many"):
        monkeypatch.setattr(QuestradeClient, name, forbidden(name))
    clock = FixedClock(NOW)
    core = test_core(db_factory, clock)
    loop = asyncio.new_event_loop()
    stack = AsyncExitStack()
    try:
        services = loop.run_until_complete(build_services(core, stack))
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


def _writes(statements: list[str]) -> list[str]:
    """Every statement that is not a plain SELECT, or that locks (FOR UPDATE/SHARE, advisory locks)."""
    out: list[str] = []
    for sql in statements:
        text = " ".join(sql.split()).upper()
        if not text.startswith("SELECT") or "FOR UPDATE" in text or "FOR SHARE" in text or "ADVISORY" in text:
            out.append(" ".join(sql.split())[:140])
    return out


def _app_ms(resp: Any) -> float:
    first = resp.headers["server-timing"].split(", ")[0]
    assert first.startswith("app;dur="), first
    return float(first.removeprefix("app;dur="))


# --- seeding ------------------------------------------------------------------------------------------------


def _live(s: Session, started_at: datetime = RUN_START) -> int:
    return seed_run(s, started_at=started_at, cash=STARTING)


def _mark(s: Session, run_id: int, sym: int, last: str | None, observed_at: datetime) -> None:
    price = Decimal(last) if last is not None else None
    s.add(
        m.QuoteMark(
            run_id=run_id,
            symbol_id=sym,
            bid=price - Decimal("0.01") if price is not None else None,
            ask=price + Decimal("0.01") if price is not None else None,
            last=price,
            observed_at=observed_at,
            written_at=observed_at,
            is_halted=False,
        )
    )


def _bars(s: Session, run_id: int, sym: int, start: datetime, n: int, price: Decimal) -> None:
    if n == 0:
        return
    s.execute(
        insert(m.MarkBar),
        [
            {
                "run_id": run_id,
                "symbol_id": sym,
                "minute_start": start + timedelta(minutes=k),
                "open": price,
                "high": price + Decimal("0.05"),
                "low": price - Decimal("0.05"),
                "close": price + Decimal("0.01") * (k % 5),
                "samples": 3,
                "updated_at": start + timedelta(minutes=k + 1),
            }
            for k in range(n)
        ],
    )


def _positions(
    s: Session,
    run_id: int,
    prefix: str,
    n: int,
    *,
    day: date = DAY,
    bars: int = 0,
    cfg: int | None = None,
) -> list[tuple[int, int]]:
    """`n` open positions (entry fee 0.35) with a live mark 0.40 above the entry; (position id, symbol id)."""
    out: list[tuple[int, int]] = []
    for i, sym in enumerate(_symbols(s, prefix, n)):
        price = Decimal("20.00") + i
        opened = at_et(day, 9, 40) + timedelta(seconds=i)
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
        _mark(s, run_id, sym, str(price + Decimal("0.40")), NOW - timedelta(seconds=3))
        _bars(s, run_id, sym, at_et(day, 9, 40), bars, price)
        out.append((pid, sym))
    s.flush()
    return out


def _orb_config(prod: Prod) -> int:
    return prod.services.registry.current("orb_sip").id


def _events(s: Session, run_id: int, n: int, *, day: date = DAY) -> None:
    for i in range(n):
        _event(s, run_id, "error", "engine", f"alert {i}", at_et(day, 11, 0) + timedelta(seconds=i))


def _ledger_row(s: Session, run_id: int, ts: datetime, amount: str, kind: str, ref: str) -> None:
    s.add(
        m.CashLedger(
            run_id=run_id,
            ts=ts,
            trade_date=ts.date(),
            settle_date=ts.date(),
            currency="USD",
            amount=Decimal(amount),
            kind=kind,
            ref=ref,
        )
    )


def _normal_day(prod: Prod, *, positions_n: int = 20, bars: int = 120) -> int:
    """Plan S14's normal day (smaller than DB-T12's only where it does not change the query shapes)."""
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        # 60 closed trades over the previous sessions, with one equity snapshot per session
        day = DAY
        sym = add_symbol(s, "HIST")
        for i in range(60):
            day = CAL.previous_session(day) if i % 2 == 0 else day
            round_trip(
                s,
                run_id,
                sym,
                entry="10.00",
                exit_="10.50" if i % 3 else "9.80",
                qty=10,
                opened=at_et(day, 9, 40) + timedelta(minutes=i % 2),
                closed=at_et(day, 10, 0) + timedelta(minutes=i % 2),
                entry_fees=fees("0.35"),
                exit_fees=fees("0.35"),
                pnl_r="0.5000" if i % 3 else "-0.4000",
            )
        snap_day = DAY
        for _ in range(60):
            snap_day = CAL.previous_session(snap_day)
            for hh in (9, 12, 16):
                s.add(
                    m.EquitySnapshot(
                        run_id=run_id,
                        ts=at_et(snap_day, hh, 1),
                        equity=STARTING,
                        cash=STARTING,
                        settled_cash=STARTING,
                        peak_equity=STARTING,
                        drawdown_pct=Decimal(0),
                    )
                )
        _positions(s, run_id, "N", positions_n, bars=bars, cfg=cfg)
        _events(s, run_id, 100)
        # 2,000 older event_log rows
        s.execute(
            insert(m.EventLog),
            [
                {
                    "ts": at_et(DAY, 8, 0) - timedelta(minutes=i),
                    "level": ("info", "warning", "error")[i % 3],
                    "source": "engine" if i % 2 else "log.worker",
                    "run_id": run_id if i % 4 else None,
                    "message": f"old {i}",
                    "data": None,
                }
                for i in range(2000)
            ],
        )
        # 543 candidates (12 passed) and 800 scan rejections over 3 rules plus 200 other decision-log rows
        cands = _symbols(s, "K", 800)
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
        rows = [
            {"stage": "scan", "outcome": "rejected", "rule": r, "symbol_id": sid, "ticker": f"K{i:03d}"}
            for i, (sid, r) in enumerate(zip(cands, rules, strict=True))
        ]
        rows += [
            {"stage": "scan", "outcome": "passed", "symbol_id": cands[i], "rule": None} for i in range(200)
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
                    "ticker": None,
                    **r,
                }
                for i, r in enumerate(rows)
            ],
        )
        for i in range(30):
            s.add(
                m.JobRun(
                    job=f"event:job{i}",
                    session_date=DAY,
                    started_at=at_et(DAY, 8, 0) + timedelta(minutes=i),
                    finished_at=at_et(DAY, 8, 1) + timedelta(minutes=i),
                    status="succeeded" if i % 5 else "failed",
                    error=None if i % 5 else "boom",
                    detail={"n": i},
                )
            )
        s.commit()
    return run_id


# --- B1-B2. read-only in production wiring ------------------------------------------------------------------


def _seed_small_day(prod: Prod) -> int:
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        _positions(s, run_id, "RO", 2, bars=10, cfg=cfg)
        _events(s, run_id, 3)
        s.commit()
    return run_id


@pytest.mark.parametrize("path", ["/api/live", "/api/live?range=run", "/api/control"])
def test_b1_b2_both_routes_only_select_in_production_wiring(prod: Prod, path: str) -> None:
    """Plan Global Constraints (`trader.api.livedata` never writes) and design D1: the Dashboard and the
    Control page are read-only. Measured on the second request (steady state) and reported for the first."""
    _seed_small_day(prod)
    client = prod.client()
    first, first_sql = _statements(prod.factory, lambda: client.get(path))
    second, second_sql = _statements(prod.factory, lambda: client.get(path))
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert second.json()["part_errors"] == []
    assert _writes(second_sql) == [], (
        f"{path} wrote on a steady-state request: {_writes(second_sql)}; first request: {_writes(first_sql)}"
    )


# --- B3. a broken plug-in config: no event written by opening a page ----------------------------------------


@pytest.mark.parametrize("path", ["/api/control", "/api/live"])
def test_b3_opening_a_page_with_a_broken_plugin_writes_no_alert_event(prod: Prod, path: str) -> None:
    """A strategy whose stored params no longer validate: the worker reports it. The API's `schedule` (DB-T6)
    and `timeline` (DB-T5) build the plan with `runtime.plan_builder`, whose fresh StrategyRegistry reports
    the broken plug-in as an `error` event (source `strategies.registry`), which the relay sends to Telegram.
    Opening the page twice must write nothing (soak.readonly_plan uses a quiet registry)."""
    _seed_small_day(prod)
    current = prod.services.registry.current("orb_sip")
    with prod.factory() as s:
        s.add(
            m.StrategyConfig(
                strategy_key="orb_sip",
                version=current.version,
                revision=current.revision + 1,
                params={**current.params, "top_n": 0},  # ge=1: fails validation
                enabled=True,
                created_at=NOW - timedelta(hours=1),
                created_by="test",
            )
        )
        s.commit()
    client = prod.client()
    for _ in range(2):
        resp = client.get(path)
        assert resp.status_code == 200, resp.text
    with prod.factory() as s:
        rows = s.execute(
            select(m.EventLog.level, m.EventLog.source, m.EventLog.message).where(
                m.EventLog.source == REGISTRY_SOURCE
            )
        ).all()
    relayable = [r for r in rows if r.level in ("error", "critical") and not r.source.startswith("log.")]
    assert rows == [], f"{path} x2 wrote {len(rows)} event(s), {len(relayable)} relayable: {rows}"


# --- B4. never Questrade ------------------------------------------------------------------------------------


def test_b4_no_questrade_call_with_live_stale_and_missing_marks(prod: Prod) -> None:
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        rows = _positions(s, run_id, "Q", 3, bars=30, cfg=cfg)
        ids, syms = [pid for pid, _ in rows], [sym for _, sym in rows]
        stale = s.get(m.QuoteMark, (run_id, syms[1]))
        assert stale is not None
        stale.observed_at = NOW - timedelta(seconds=45)
        missing = s.get(m.QuoteMark, (run_id, syms[2]))
        s.delete(missing)
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=NOW - timedelta(hours=5),
                beat_at=NOW - timedelta(seconds=95),  # a stale heartbeat as well
                session_date=DAY,
                phase="session",
                detail={"rate_limit": {"market_remaining": 1}},
            )
        )
        s.commit()
    client = prod.client()
    expand = ",".join(str(i) for i in ids)
    for path in ("/api/live", "/api/live?range=run", f"/api/live?expand={expand}", "/api/control"):
        resp = client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json()["part_errors"] == [], (path, resp.json()["part_errors"])
    body = client.get(f"/api/live?expand={expand}").json()
    assert [p["mark_state"] for p in body["positions"]] == ["live", "stale", "missing"]
    assert body["positions"][2]["unrealized"] is None and body["worker_stale"] is True
    assert prod.questrade == []


# --- B5. replay isolation -----------------------------------------------------------------------------------


def _replay_on_the_same_symbol(s: Session, sym: int, cfg: int) -> None:
    """A replay run with rows of every kind on the live run's symbol and day, including a pending proposal,
    an open position, a quote mark and mark bars, and an active manual pause."""
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
    _ledger_row(s, replay, at_et(DAY, 6, 0), "777", "deposit", "deposit")
    s.flush()
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
        pnl_r="9.0000",
    )
    _mark(s, replay, sym, "99.99", NOW - timedelta(seconds=1))
    _bars(s, replay, sym, at_et(DAY, 9, 40), 60, Decimal("99.00"))
    sig = _signal(s, replay, cfg, sym, DAY, at_et(DAY, 9, 35))
    _proposal(s, replay, sig, sym, kind="entry", created_at=NOW - timedelta(seconds=10), status="pending")
    _proposal(s, replay, sig, sym, kind="exit", created_at=at_et(DAY, 10, 0), position_id=pid)
    s.add(
        m.KillSwitchEvent(run_id=replay, switch="manual_pause", session_date=DAY, tripped_at=at_et(DAY, 9, 0))
    )
    s.add(
        m.EquitySnapshot(
            run_id=replay,
            ts=at_et(DAY, 9, 0),
            equity=Decimal("1"),
            cash=Decimal("1"),
            settled_cash=Decimal("1"),
            peak_equity=Decimal("100000"),
            drawdown_pct=Decimal("0.99"),
        )
    )
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
        m.DecisionLog(
            run_id=replay,
            session_date=DAY,
            seq=1,
            stage="scan",
            symbol_id=sym,
            ticker="LIVE000",
            outcome="rejected",
            rule="replay_rule",
            ts=at_et(DAY, 9, 36),
            recorded_at=at_et(DAY, 9, 40),
        )
    )
    _event(s, replay, "critical", "engine", "replay critical", at_et(DAY, 12, 0))
    _event(s, replay, "warning", "engine", "replay warning", at_et(DAY, 12, 1))
    s.flush()


def test_b5_replay_rows_on_the_same_symbol_change_nothing(prod: Prod) -> None:
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        (pid, sym), *_ = _positions(s, run_id, "LIVE", 1, bars=60, cfg=cfg)
        round_trip(
            s,
            run_id,
            add_symbol(s, "DONE"),
            entry="10.00",
            exit_="10.25",
            qty=10,
            opened=at_et(DAY, 9, 41),
            closed=at_et(DAY, 10, 1),
            pnl_r="0.5000",
        )
        s.commit()
    client = prod.client()
    paths = ("/api/live", "/api/live?range=run", f"/api/live?expand={pid}", "/api/control")
    before = [client.get(p).json() for p in paths]
    assert all(b["part_errors"] == [] for b in before)
    with prod.factory() as s:
        _replay_on_the_same_symbol(s, sym, cfg)
        s.commit()
    health.clear_soak_cache()
    after = [client.get(p).json() for p in paths]
    for body in (before[-1], after[-1]):  # the database round trip varies per request
        body["health"]["db_latency_ms"] = None
    for path, b, a in zip(paths, before, after, strict=True):
        changed = sorted(k for k in b if b[k] != a[k])
        assert changed == [], f"{path}: replay rows changed {changed}"
    assert before[0]["trading"] == "running" and before[0]["pending"] == []


# --- B6-B7. money at period boundaries ----------------------------------------------------------------------


def _period(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(p for p in body["periods"] if p["period"] == key)


def _money(block: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        block[k] for k in ("realized", "unrealized", "pnl_after_fees", "fees", "claude_usd", "net_after_ai")
    )


def test_b6_periods_across_the_dst_week_boundary_with_a_position_carried_over_the_weekend(prod: Prod) -> None:
    """Run from Wed 10-28. A: Fri 10-30 round trip (+9.30 after 0.70 fees). B: bought Fri 10-30 15:00 ET,
    10 @ 20.00 (entry fee 0.1234567 -> 0.1235 as the ledger stores it), still open, marked 21.50. E: bought
    Sun 23:59:59 EST (2026-11-02T04:59:59Z, week of 10-26) and sold Mon 00:00:00 EST (05:00:00Z, week of
    11-02), +4.60 after 0.40. F: Mon 11-02 -5.70 after 0.70. Claude: 0.123456 (10-30), 0.200001 (11-02) and a
    weekly report updated Sat 10-31 (0.5)."""
    b_fee = Fees(ecn=Decimal("0.1234567"))
    with prod.factory() as s:
        run_id = _live(s, started_at=et(2026, 10, 28, 9))
        a, b_sym, e_sym, f_sym = (add_symbol(s, t) for t in ("AAA", "BBB", "EEE", "FFF"))
        round_trip(
            s,
            run_id,
            a,
            entry="10.00",
            exit_="11.00",
            qty=10,
            opened=et(2026, 10, 30, 10),
            closed=et(2026, 10, 30, 11),
            entry_fees=fees("0.35"),
            exit_fees=fees("0.35"),
            pnl_r="1.0000",
        )
        open_position(
            s,
            run_id,
            b_sym,
            qty=10,
            price=Decimal("20.00"),
            ts=et(2026, 10, 30, 15),
            fees=b_fee,
            stop=Decimal("19"),
        )
        round_trip(
            s,
            run_id,
            e_sym,
            entry="10.00",
            exit_="10.50",
            qty=10,
            opened=datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC),
            closed=datetime(2026, 11, 2, 5, 0, 0, tzinfo=UTC),
            entry_fees=fees("0.20"),
            exit_fees=fees("0.20"),
            pnl_r="0.5000",
        )
        round_trip(
            s,
            run_id,
            f_sym,
            entry="30.00",
            exit_="29.00",
            qty=5,
            opened=et(2026, 11, 2, 10),
            closed=et(2026, 11, 2, 10, 30),
            entry_fees=fees("0.35"),
            exit_fees=fees("0.35"),
            pnl_r="-1.0000",
        )
        monday = et(2026, 11, 2, 11)
        _mark(s, run_id, b_sym, "21.50", monday - timedelta(seconds=2))
        _catalyst(s, a, date(2026, 10, 30), "0.123456")
        _catalyst(s, f_sym, date(2026, 11, 2), "0.200001")
        _weekly(s, run_id, date(2026, 10, 30), et(2026, 10, 31, 12), "0.500000")
        s.commit()
    client = prod.client()

    prod.clock.set(monday)
    body = client.get("/api/live").json()
    assert body["part_errors"] == []
    assert body["session_day"] == "2026-11-02"
    today, week, run = (_period(body, k) for k in ("today", "week", "run"))
    assert (today["date_from"], week["date_from"], run["date_from"]) == (
        "2026-11-02",
        "2026-11-02",
        "2026-10-28",
    )
    # the open position carried over the weekend counts fully in every period (S4, open question 5)
    assert _money(today) == ("-1.1000", "14.8765", "13.7765", "0.9000", "0.2000", "13.5765")
    assert _money(week) == _money(today)
    assert _money(run) == ("8.2000", "14.8765", "23.0765", "1.9235", "0.8235", "22.2530")
    assert (today["trades"], today["wins"], run["trades"], run["wins"]) == (2, 1, 3, 2)
    # the S4 invariant and the books, exact
    assert Decimal(run["pnl_after_fees"]) == Decimal(body["risk"]["equity"]) - STARTING
    assert body["books"]["ok"] is True and Decimal(body["books"]["difference"]) == 0

    # Sunday 11-01 23:59:59 EST (DST ended that morning): today = Fri 10-30, week = 10-26..11-01
    prod.clock.set(datetime(2026, 11, 2, 4, 59, 59, tzinfo=UTC))
    body = client.get("/api/live").json()
    assert body["part_errors"] == []
    assert body["session_day"] == "2026-10-30"
    today, week = _period(body, "today"), _period(body, "week")
    assert (today["date_from"], week["date_from"], week["date_to"]) == (
        "2026-10-30",
        "2026-10-26",
        "2026-11-01",
    )
    assert _money(today)[0::3] == ("9.3000", "0.8235")  # realized, fees (A 0.70 + B's entry 0.1235)
    assert _money(week)[0] == "9.3000"
    assert week["fees"] == "1.0235"  # + E's entry fee at 04:59:59Z; its exit fee (05:00:00Z) is next week's
    assert week["claude_usd"] == "0.6235" and today["claude_usd"] == "0.1235"
    assert week["net_after_ai"] == str(Decimal(week["pnl_after_fees"]) - Decimal("0.6235"))


def test_b7_periods_on_thanksgiving_and_the_weekend_after(prod: Prod) -> None:
    with prod.factory() as s:
        run_id = _live(s, started_at=et(2026, 11, 16, 9))
        sym = add_symbol(s, "TGV")
        for day, exit_ in (
            (date(2026, 11, 20), "10.10"),
            (date(2026, 11, 23), "10.20"),
            (date(2026, 11, 25), "9.60"),
        ):
            round_trip(
                s,
                run_id,
                sym,
                entry="10.00",
                exit_=exit_,
                qty=10,
                opened=et(day.year, day.month, day.day, 9, 40),
                closed=et(day.year, day.month, day.day, 10),
                entry_fees=fees("0.05"),
                exit_fees=fees("0.05"),
            )
        s.commit()
    client = prod.client()
    prod.clock.set(et(2026, 11, 26, 12))  # Thanksgiving: no session
    body = client.get("/api/live").json()
    assert body["part_errors"] == []
    assert body["session_day"] == "2026-11-25" and body["session"]["is_session"] is False
    today, week, run = (_period(body, k) for k in ("today", "week", "run"))
    # 11-20 +0.90, 11-23 +1.90, 11-25 -4.10 (each after 0.10 of fees)
    assert (today["realized"], week["realized"], run["realized"]) == ("-4.1000", "-2.2000", "-1.3000")
    assert (week["date_from"], week["date_to"]) == ("2026-11-23", "2026-11-26")
    assert body["closed_today"] == 1 and body["timeline"] == []
    prod.clock.set(et(2026, 11, 28, 12))  # Saturday: today = Fri 11-27 (a half day, nothing traded)
    body = client.get("/api/live").json()
    today, week = _period(body, "today"), _period(body, "week")
    assert body["session_day"] == "2026-11-27"
    assert (today["realized"], today["trades"], week["realized"]) == ("0.0000", 0, "-2.2000")


# --- B8. the books check to the cent ------------------------------------------------------------------------


def test_b8_books_flip_on_one_cent_and_pass_on_exact_books(prod: Prod) -> None:
    with prod.factory() as s:
        run_id = _live(s)
        sym = add_symbol(s, "BKS")
        for i in range(25):
            round_trip(
                s,
                run_id,
                sym,
                entry="12.3456",
                exit_="12.4567" if i % 2 else "12.1111",
                qty=7 + i,
                opened=at_et(DAY, 9, 40) + timedelta(minutes=i),
                closed=at_et(DAY, 10, 40) + timedelta(minutes=i),
                entry_fees=Fees(
                    commission=Decimal("1"), ecn=Decimal("0.0035") * (7 + i), sec=Decimal("0.0000278")
                ),
                exit_fees=Fees(
                    commission=Decimal("1"), ecn=Decimal("0.0035") * (7 + i), sec=Decimal("0.0000278")
                ),
            )
        open_position(
            s,
            run_id,
            add_symbol(s, "OPN"),
            qty=13,
            price=Decimal("7.7777"),
            ts=at_et(DAY, 11, 0),
            fees=fees("0.35"),
        )
        s.commit()
    client = prod.client()
    good = client.get("/api/live").json()["books"]
    assert good["ok"] is True and good["difference"] == "0.0000", good
    with prod.factory() as s:
        _ledger_row(s, run_id, at_et(DAY, 12, 0), "-0.0100", "fee", "breaker:cent")
        s.commit()
    bad = client.get("/api/live").json()["books"]
    assert bad["ok"] is False and bad["difference"] == "-0.0100", bad


# --- B9. unrealised from the marks, partial, the invariant --------------------------------------------------


def test_b9_unrealised_uses_marks_flags_partial_and_the_invariant_holds_when_all_marked(prod: Prod) -> None:
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        rows = _positions(s, run_id, "U", 3, bars=5, cfg=cfg)
        round_trip(
            s,
            run_id,
            add_symbol(s, "CLS"),
            entry="10.00",
            exit_="10.33",
            qty=9,
            opened=at_et(DAY, 9, 45),
            closed=at_et(DAY, 10, 15),
            entry_fees=fees("0.35"),
            exit_fees=fees("0.35"),
        )
        # position 1: a stale mark (kept, flagged stale); position 2: no mark
        stale = s.get(m.QuoteMark, (run_id, rows[1][1]))
        assert stale is not None
        stale.observed_at, stale.last = NOW - timedelta(minutes=5), Decimal("20.10")
        s.delete(s.get(m.QuoteMark, (run_id, rows[2][1])))
        s.commit()
    client = prod.client()
    body = client.get("/api/live").json()
    assert body["part_errors"] == []
    # (20.40 - 20.00) x 10 - 0.35 + (20.10 - 21.00) x 11 - 0.35 = 4.00 - 0.35 - 9.90 - 0.35
    for block in body["periods"]:
        assert block["unrealized"] == "-6.6000" and block["unrealized_partial"] is True, block
    by_id = {p["id"]: p for p in body["positions"]}
    assert [by_id[pid]["mark_state"] for pid, _ in rows] == ["live", "stale", "missing"]
    assert by_id[rows[0][0]]["unrealized"] == "4.0000"  # gross, the Telegram definition
    assert by_id[rows[2][0]]["unrealized"] is None
    # equity at marks values the unmarked position at cost
    with prod.factory() as s:
        cash = s.execute(
            select(func.sum(m.CashLedger.amount)).where(m.CashLedger.run_id == run_id)
        ).scalar_one()
    expected_equity = cash + Decimal("20.40") * 10 + Decimal("20.10") * 11 + Decimal("22.00") * 12
    assert Decimal(body["risk"]["equity"]) == expected_equity
    # mark the last one: not partial, and run P&L after fees == equity at marks - starting cash (S4)
    with prod.factory() as s:
        _mark(s, run_id, rows[2][1], "22.25", NOW - timedelta(seconds=1))
        s.commit()
    body = client.get("/api/live").json()
    run = _period(body, "run")
    assert run["unrealized_partial"] is False
    assert Decimal(run["pnl_after_fees"]) == Decimal(body["risk"]["equity"]) - STARTING


# --- B10. a fixed number of statements ----------------------------------------------------------------------


def test_b10_statement_count_is_constant_in_positions_and_activity(prod: Prod) -> None:
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = _live(s)
        _positions(s, run_id, "S", 1, bars=20, cfg=cfg)  # 2 items (order placed, fill)
        _events(s, run_id, 8)
        s.commit()
    client = prod.client()
    assert client.get("/api/live").status_code == 200  # warm-up (the plan builder ensures defaults once)
    small, small_sql = _statements(prod.factory, lambda: client.get("/api/live?expand=1"))
    with prod.factory() as s:
        _positions(s, run_id, "T", 19, bars=20, cfg=cfg)  # 38 more items
        _events(s, run_id, 52)
        s.commit()
    ids = ",".join(str(i) for i in range(1, 4))
    big, big_sql = _statements(prod.factory, lambda: client.get(f"/api/live?expand={ids}"))
    sb, bb = small.json(), big.json()
    assert (len(sb["positions"]), len(sb["activity"])) == (1, 10)
    assert (len(bb["positions"]), len(bb["activity"])) == (20, 100)
    print(f"GET /api/live statements: {len(small_sql)} (1 position, 10 items), {len(big_sql)} (20, 100)")
    assert len(big_sql) == len(small_sql)
    assert len(big_sql) <= LIVE_MAX_STATEMENTS


# --- B11. the 300 ms budget on a normal day -----------------------------------------------------------------


def test_b11_live_answers_under_300_ms_on_a_normal_day(prod: Prod) -> None:
    """The medians are printed (the number to report). Under the shared gate (2 lanes x 4 workers) wall time
    is inflated several-fold by other tests, so the assertion is on the fastest request of each series (the
    route's own cost without contention) plus the statement ceiling; DB-T12 test 1 asserts the median."""
    _normal_day(prod)
    client = prod.client()
    with prod.factory() as s:
        ids = list(
            s.execute(
                select(m.Position.id).where(m.Position.closed_at.is_(None)).order_by(m.Position.id).limit(3)
            ).scalars()
        )
    report: list[str] = []
    fastest: list[float] = []
    for path in ("/api/live", f"/api/live?range=run&expand={','.join(map(str, ids))}"):
        for _ in range(2):
            assert client.get(path).status_code == 200
        timings: list[float] = []
        parts = ""
        for _ in range(10):
            resp = client.get(path)
            assert resp.status_code == 200 and resp.json()["part_errors"] == []
            timings.append(_app_ms(resp))
            parts = resp.headers["server-timing"]
        body = resp.json()
        assert len(body["positions"]) == 20 and len(body["activity"]) == 100
        assert body["rejections"]["total"] == 800
        median = statistics.median(timings)
        fastest.append(min(timings))
        report.append(
            f"{path}: median {median:.1f} ms, min {min(timings):.1f}, max {max(timings):.1f}; last {parts}"
        )
    _, sql = _statements(prod.factory, lambda: client.get("/api/live"))
    report.append(f"statements per request: {len(sql)} (ceiling {LIVE_MAX_STATEMENTS})")
    print("\n".join(report))
    assert all(ms < BUDGET_MS for ms in fastest), report
    assert len(sql) <= LIVE_MAX_STATEMENTS, report


def test_b13_pending_proposals_do_not_add_statements_on_a_normal_day(prod: Prod) -> None:
    """Manual mode at 09:36 is the normal day with pending entries. The pending part builds each proposal's
    view with `notify.views.proposal_view` (signal, config and symbol `Session.get` per row): with the
    normal day already at the ceiling, two pending proposals must not push the request over it."""
    _normal_day(prod)
    client = prod.client()
    assert client.get("/api/live").status_code == 200
    _, without = _statements(prod.factory, lambda: client.get("/api/live"))
    cfg = _orb_config(prod)
    with prod.factory() as s:
        run_id = s.execute(select(m.Run.id).where(m.Run.mode == "live")).scalar_one()
        for sym in _symbols(s, "PP", 2):
            sig = _signal(s, run_id, cfg, sym, DAY, NOW - timedelta(seconds=30))
            _proposal(
                s, run_id, sig, sym, kind="entry", created_at=NOW - timedelta(seconds=20), status="pending"
            )
        s.commit()
    resp, with_pending = _statements(prod.factory, lambda: client.get("/api/live"))
    assert len(resp.json()["pending"]) == 2
    print(f"GET /api/live statements: {len(without)} without pending, {len(with_pending)} with 2 pending")
    assert len(with_pending) == len(without) and len(with_pending) <= LIVE_MAX_STATEMENTS, (
        len(without),
        len(with_pending),
    )


# --- B12. part isolation, masked ----------------------------------------------------------------------------

LIVE_PARTS: dict[str, tuple[Any, str]] = {
    "session_day": (periods, "session_day"),
    "worker": (api_views, "worker_out"),
    "trading": (risk, "trading_state"),
    "positions": (positions, "live_positions"),
    "periods": (periods, "period_blocks"),
    "claude_today": (periods, "claude_today"),
    "books": (books, "books_check"),
    "equity": (equity, "equity_series"),
    "risk": (risk, "risk_panel"),
    "activity": (activity, "activity_feed"),
    "rejections": (activity, "rejections"),
    "timeline": (live, "build_timeline"),
    "pending": (live, "_pending"),
    "closed_today": (live, "_closed_today"),
}
CONTROL_PARTS: dict[str, tuple[Any, str]] = {
    "engine": (control, "engine_card"),
    "killswitches": (risk, "killswitch_lights"),
    "strategies": (control, "strategy_cards"),
    "schedule": (control, "schedule"),
    "health": (health, "health_panel"),
    "soak": (health, "soak_summary"),
    "errors": (control, "error_log"),
}
LIVE_FIELDS = ("periods", "claude_today", "books", "equity", "risk", "positions", "activity", "rejections")
LIVE_FIELDS += ("timeline", "pending")
CONTROL_FIELDS = ("engine", "killswitches", "killswitch_history", "strategies", "schedule", "health", "soak")
CONTROL_FIELDS += ("errors",)
# a part whose failure also empties another (plan DB-T5: the risk panel needs the positions)
ALSO_NULL = {
    "positions": {"risk"},
    "killswitches": {"killswitch_history"},
    "session_day": {"periods"},  # period_windows calls the (patched) session_day too
}


@pytest.mark.parametrize(
    ("path", "part"),
    [("/api/live", p) for p in LIVE_PARTS] + [("/api/control", p) for p in CONTROL_PARTS],
)
def test_b12_each_part_fails_alone_with_a_masked_message(
    prod: Prod, monkeypatch: pytest.MonkeyPatch, path: str, part: str
) -> None:
    _seed_small_day(prod)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(BOOM)

    module, name = (LIVE_PARTS if path == "/api/live" else CONTROL_PARTS)[part]
    monkeypatch.setattr(module, name, boom)
    resp = prod.client().get(path)
    assert resp.status_code == 200, resp.text
    for secret in SECRETS:
        assert secret not in resp.text, secret
    body = resp.json()
    named = [e for e in body["part_errors"] if e["part"] == part]
    assert len(named) == 1 and named[0]["message"].startswith("RuntimeError: boom "), body["part_errors"]
    assert len(named[0]["message"]) <= len("RuntimeError: ") + 120
    nulls = {part} | ALSO_NULL.get(part, set())
    assert {e["part"] for e in body["part_errors"]} <= nulls, body["part_errors"]
    fields = LIVE_FIELDS if path == "/api/live" else CONTROL_FIELDS
    for field in fields:
        if field in nulls:
            assert body[field] is None, field
        else:
            assert body[field] is not None, (field, body["part_errors"])
