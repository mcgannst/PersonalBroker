"""DB-T5 acceptance tests 3-8: `GET /api/live` (live dashboard plan S14, S15).

The route composes DB-T3's and DB-T4's parts, built in parallel with this task. Tests 3, 4 and 6 replace
every part function with a recording fake (`FakeParts`), so they pin the route alone. Tests 5 and 7 run the
real composition on a seeded day and substitute a fake only for a part function that is still a DB-T1 stub
(`FakeParts.install(only_stubs=True)`), so they exercise the real DB-T3/DB-T4 code as soon as it lands; the
end-to-end budget and composition are DB-T12's.
"""

import asyncio
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.api.test_trading import _config, _proposal, _signal, at_et, live_run
from tests.factories import add_run, add_symbol
from tests.fakes_api import make_services, test_core
from tests.live.test_activity_db import (
    DAY,
    NOW,
    _event,
    _fill,
    s5_day_bounds,
    seed_replay_noise,
    seed_session_day,
)
from tests.live.test_contracts import _is_stub
from trader.api import views as api_views
from trader.api.deps import ApiServices
from trader.api.livedata import activity, books, equity, periods, positions, risk
from trader.api.livedata.types import LivePositions, OpenValue, PeriodWindow
from trader.api.routers import live, meta
from trader.api.schemas import (
    ActivityItemOut,
    BooksCheckOut,
    ClaudeTodayOut,
    EquityPointLiveOut,
    EquitySeriesOut,
    LivePositionOut,
    PeriodPnlOut,
    ProposalOut,
    RejectionRuleOut,
    RejectionsOut,
    RiskOut,
)
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.notify.views import proposal_view

pytestmark = pytest.mark.db

SECRET = "token=abcdef123456"
BOOM = f"boom {SECRET}"
MASKED = "RuntimeError: boom token=[REDACTED]"

# --- fixtures of each part ----------------------------------------------------------------------------------

PERIODS = [
    PeriodPnlOut(
        period=k,
        date_from=DAY,
        date_to=DAY,
        realized=Decimal("-12.5"),
        unrealized=Decimal("3"),
        unrealized_partial=False,
        pnl_after_fees=Decimal("-9.5"),
        fees=Decimal("0.7"),
        claude_usd=Decimal("0.12"),
        net_after_ai=Decimal("-9.62"),
        trades=1,
        wins=0,
        losses=1,
        trades_without_r=0,
    )
    for k in ("today", "week", "run")
]
CLAUDE = ClaudeTodayOut(
    date=DAY, spent_usd=Decimal("0.12"), cap_usd=Decimal("2"), used_fraction=Decimal("0.06")
)
BOOKS = BooksCheckOut(
    ok=True,
    cash=Decimal("990"),
    positions_at_cost=Decimal("0"),
    actual=Decimal("990"),
    starting_cash=Decimal("1000"),
    realized_gross=Decimal("-11.8"),
    fees_paid=Decimal("0.7"),
    expected=Decimal("987.5"),
    difference=Decimal("0"),
    realized_recorded=Decimal("-12.5"),
    open_positions=0,
)
POSITION = LivePositionOut(
    id=12,
    symbol_id=3,
    ticker="AAPL",
    strategy_key="orb_sip",
    side="long",
    qty=25,
    entry=Decimal("182.46"),
    mark=Decimal("182.6"),
    mark_at=NOW,
    mark_state="live",
    stop=Decimal("181.96"),
    stop_working=True,
    near_stop=False,
    opened_at=NOW - timedelta(minutes=5),
    held_seconds=300,
    unprotected_seconds=0,
    spark=[],
    fills=[],
    link="/trades?position=12",
)
LIVE_POSITIONS = LivePositions(
    positions=[POSITION],
    open_values=[OpenValue(12, 3, 25, Decimal("182.46"), Decimal("182.6"), Decimal("0.35"))],
    equity_at_marks=Decimal("1003.5"),
    all_marked=True,
)
RISK = RiskOut(
    killswitches=[],
    equity=Decimal("1003.5"),
    open_risk=Decimal("12.5"),
    open_risk_cap=Decimal("10.04"),
    slots_used=1,
    slots_max=1,
    open_positions=1,
)
ACTIVITY = [
    ActivityItemOut(
        id="fill:1", ts=NOW, kind="fill", chip="trades", ticker="AAPL", text="Filled", tone="neutral"
    )
]
REJECTIONS = RejectionsOut(
    session_date=DAY,
    source="decision_log",
    total=2,
    final=False,
    recorded_at=NOW,
    rules=[
        RejectionRuleOut(
            stage="scan", rule="rvol_below_min", count=2, tickers=["A", "B"], truncated=False, link="/x"
        )
    ],
)


def _equity(range_: str) -> EquitySeriesOut:
    return EquitySeriesOut(
        range=range_,
        start_equity=Decimal("1000"),
        points=[EquityPointLiveOut(ts=NOW, equity=Decimal("1003.5"), source="now")],
        fills=[],
        downsampled=False,
    )


def _windows() -> tuple[PeriodWindow, PeriodWindow, PeriodWindow]:
    start, end = s5_day_bounds(DAY)
    return tuple(PeriodWindow(k, DAY, DAY, start, end) for k in ("today", "week", "run"))  # type: ignore[return-value]


class FakeParts:
    """Recording fakes for every DB-T3/DB-T4/DB-T5 part function the route calls."""

    def __init__(self) -> None:
        self.calls: dict[str, list[tuple[Any, ...]]] = {}
        self.raising: dict[str, BaseException] = {}
        self.sleep_s: dict[str, float] = {}

    def _fake(self, name: str, result: Callable[..., Any]) -> Callable[..., Any]:
        def fn(*args: Any, **kwargs: Any) -> Any:
            self.calls.setdefault(name, []).append((*args, *kwargs.values()))
            if name in self.sleep_s:
                time.sleep(self.sleep_s[name])
            if name in self.raising:
                raise self.raising[name]
            return result(*args, **kwargs)

        return fn

    def fakes(self) -> list[tuple[Any, str, Callable[..., Any]]]:
        return [
            (periods, "et_day_bounds", self._fake("et_day_bounds", s5_day_bounds)),
            (periods, "session_day", self._fake("session_day", lambda cal, now: DAY)),
            (periods, "period_windows", self._fake("period_windows", lambda *a: _windows())),
            (periods, "period_blocks", self._fake("period_blocks", lambda *a: list(PERIODS))),
            (periods, "claude_today", self._fake("claude_today", lambda *a: CLAUDE)),
            (books, "books_check", self._fake("books_check", lambda *a: BOOKS)),
            (
                equity,
                "equity_series",
                self._fake("equity_series", lambda f, c, r, n, range_, e: _equity(range_)),
            ),
            (positions, "live_positions", self._fake("live_positions", lambda *a: LIVE_POSITIONS)),
            (risk, "trading_state", self._fake("trading_state", lambda *a: "running")),
            (risk, "risk_panel", self._fake("risk_panel", lambda *a: RISK)),
            (activity, "activity_feed", self._fake("activity_feed", lambda *a, **k: list(ACTIVITY))),
            (activity, "rejections", self._fake("rejections", lambda *a: REJECTIONS)),
        ]

    def install(self, monkeypatch: pytest.MonkeyPatch, *, only_stubs: bool = False) -> "FakeParts":
        for module, name, fake in self.fakes():
            if not only_stubs or _is_stub(getattr(module, name)):
                monkeypatch.setattr(module, name, fake)
        return self


@pytest.fixture
def parts(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeParts]:
    yield FakeParts().install(monkeypatch)


def _services(factory: sessionmaker[Session], **overrides: Any) -> ApiServices:
    return make_services(test_core(factory, FixedClock(NOW)), **overrides)


def _client(factory: sessionmaker[Session], **overrides: Any) -> TestClient:
    return make_client(_services(factory, **overrides), live.router, raise_server_exceptions=False)


def _json(model: Any) -> Any:
    if isinstance(model, list):
        return [_json(x) for x in model]
    return model.model_dump(mode="json")


# --- 3. shape -----------------------------------------------------------------------------------------------


def test_the_route_returns_every_part(db_factory: sessionmaker[Session], parts: FakeParts) -> None:
    run_id = live_run(db_factory, FixedClock(NOW))
    r = _client(db_factory).get("/api/live")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part_errors"] == []
    assert (body["run_id"], body["server_time"], body["session_day"]) == (
        run_id,
        "2026-10-06T18:00:00Z",
        "2026-10-06",
    )
    assert body["session"]["phase"] == "open" and body["session"]["is_session"] is True
    assert (body["approval_mode"], body["trading"], body["telegram_configured"]) == (
        "manual",
        "running",
        True,
    )
    assert body["worker"]["ok"] is False and body["worker_stale"] is True  # no heartbeat row
    assert (body["marks_stale_seconds"], body["closed_today"]) == (30, 0)
    assert body["periods"] == _json(PERIODS)
    assert body["claude_today"] == _json(CLAUDE)
    assert body["books"] == _json(BOOKS)
    assert body["equity"] == _json(_equity("today"))
    assert body["risk"] == _json(RISK)
    assert body["positions"] == _json([POSITION])
    assert body["activity"] == _json(ACTIVITY)
    assert body["rejections"] == _json(REJECTIONS)
    assert [t["key"] for t in body["timeline"]][:2] == ["nightly", "premarket"]
    assert body["pending"] == []
    # the arguments the parts got: the run, the session day, the positions' open values and equity
    assert parts.calls["period_blocks"][0][1:] == (run_id, _windows(), LIVE_POSITIONS.open_values)
    assert parts.calls["equity_series"][0][2:] == (run_id, NOW, "today", Decimal("1003.5"))
    assert parts.calls["activity_feed"][0][1:] == (run_id, DAY, ZoneInfo("America/Edmonton"))  # tz_display
    assert parts.calls["rejections"][0][1:] == (run_id, DAY)
    assert parts.calls["risk_panel"][0][-1] is LIVE_POSITIONS
    assert parts.calls["live_positions"][0][1:] == (run_id, NOW, frozenset())


def test_range_and_expand_reach_their_parts(db_factory: sessionmaker[Session], parts: FakeParts) -> None:
    client = _client(db_factory)
    r = client.get("/api/live", params={"range": "run", "expand": "12,15"})
    assert r.status_code == 200, r.text
    assert r.json()["equity"]["range"] == "run"
    assert parts.calls["equity_series"][-1][4] == "run"
    assert parts.calls["live_positions"][-1][3] == {12, 15}
    for bad in ({"expand": "1,2,3,4"}, {"expand": "abc"}, {"expand": "0"}, {"range": "week"}):
        resp = client.get("/api/live", params=bad)
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["code"] == "validation"


def test_closed_today_and_pending_come_from_the_live_run(
    db_factory: sessionmaker[Session], parts: FakeParts
) -> None:
    run_id = live_run(db_factory, FixedClock(NOW))
    with db_factory() as s:
        seed_session_day(s, run_id)
        seed_replay_noise(s, run_id)
        sym = add_symbol(s, "PEND")
        sig = _signal(s, run_id, _config(s, "orb_sip"), sym, DAY, NOW)
        pending = _proposal(
            s, run_id, sig, sym, kind="entry", created_at=NOW - timedelta(seconds=5), status="pending"
        )
        s.commit()
    body = _client(db_factory).get("/api/live").json()
    assert body["closed_today"] == 1
    assert [p["id"] for p in body["pending"]] == [pending.id]


def test_pending_views_take_the_same_statements_for_0_2_and_10_pending(
    db_factory: sessionmaker[Session],
) -> None:
    """Fix round 1 (DB-GDATA F4): the views are built from one batch (no Session.get per proposal), and each
    is the same view `notify.views.proposal_view` builds row by row."""
    run_id = live_run(db_factory, FixedClock(NOW))
    services = _services(db_factory)
    engine = db_factory.kw["bind"]
    counts: list[int] = []
    with db_factory() as s:
        cfg = _config(s, "orb_sip")
        s.commit()
    for n in (0, 2, 8):  # 0, then 2, then 10 pending
        with db_factory() as s:
            for sym in (add_symbol(s, f"PN{len(counts)}{i}") for i in range(n)):
                sig = _signal(s, run_id, cfg, sym, DAY, NOW)
                _proposal(s, run_id, sig, sym, kind="entry", created_at=NOW, status="pending")
            s.commit()
        seen: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any, seen: list[str] = seen) -> None:
            seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            out = live._pending(services, run_id)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        counts.append(len(seen))
    assert len(out) == 10
    assert counts == [counts[0]] * 3, counts
    with db_factory() as s:
        rows = s.scalars(
            select(m.Proposal)
            .where(m.Proposal.status == "pending")
            .order_by(m.Proposal.created_at, m.Proposal.id)
        ).all()
        expected = [ProposalOut.from_view(proposal_view(s, p), p) for p in rows]
    assert out == expected
    assert {p.ticker for p in out} == {f"PN{k}{i}" for k, n in ((1, 2), (2, 8)) for i in range(n)}
    assert {p.strategy_key for p in out} == {"orb_sip"}


# --- 4. part isolation --------------------------------------------------------------------------------------

# part name in the response / part_errors -> the fake that raises
ISOLATED = {
    "periods": "period_blocks",
    "claude_today": "claude_today",
    "books": "books_check",
    "equity": "equity_series",
    "risk": "risk_panel",
    "activity": "activity_feed",
    "rejections": "rejections",
}
OPTIONAL = (
    "periods",
    "claude_today",
    "books",
    "equity",
    "risk",
    "positions",
    "activity",
    "rejections",
    "timeline",
    "pending",
)


@pytest.mark.parametrize("part", sorted(ISOLATED))
def test_a_failing_part_is_null_with_its_error(
    db_factory: sessionmaker[Session], parts: FakeParts, part: str
) -> None:
    parts.raising[ISOLATED[part]] = RuntimeError(BOOM)
    r = _client(db_factory).get("/api/live")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part_errors"] == [{"part": part, "message": MASKED}]
    assert SECRET not in r.text
    assert body[part] is None
    assert all(body[p] is not None for p in OPTIONAL if p != part)


def test_failing_positions_leave_the_periods_partial_and_the_risk_unknown(
    db_factory: sessionmaker[Session], parts: FakeParts
) -> None:
    parts.raising["live_positions"] = RuntimeError(BOOM)
    body = _client(db_factory).get("/api/live").json()
    assert [e["part"] for e in body["part_errors"]] == ["positions", "risk"]
    assert body["part_errors"][0]["message"] == MASKED
    assert body["positions"] is None and body["risk"] is None
    assert all(b["unrealized_partial"] for b in body["periods"])
    assert parts.calls["period_blocks"][0][3] == []  # no open values
    assert parts.calls["equity_series"][0][5] is None  # no equity at marks
    assert all(body[p] is not None for p in OPTIONAL if p not in ("positions", "risk"))


@pytest.mark.parametrize(
    ("target", "part"),
    [
        (lambda: (live, "build_timeline"), "timeline"),
        (lambda: (live, "_pending"), "pending"),
    ],
)
def test_the_timeline_and_pending_parts_are_isolated(
    db_factory: sessionmaker[Session],
    parts: FakeParts,
    monkeypatch: pytest.MonkeyPatch,
    target: Callable[[], tuple[Any, str]],
    part: str,
) -> None:
    module, name = target()

    def boom(*args: Any) -> Any:
        raise RuntimeError(BOOM)

    monkeypatch.setattr(module, name, boom)
    body = _client(db_factory).get("/api/live").json()
    assert body["part_errors"] == [{"part": part, "message": MASKED}]
    assert body[part] is None
    assert all(body[p] is not None for p in OPTIONAL if p != part)


def test_the_header_fields_fall_back_when_their_lookup_fails(
    db_factory: sessionmaker[Session], parts: FakeParts, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any) -> Any:
        raise RuntimeError(BOOM)

    parts.raising["session_day"] = RuntimeError(BOOM)
    parts.raising["trading_state"] = RuntimeError(BOOM)
    monkeypatch.setattr(api_views, "worker_out", boom)
    monkeypatch.setattr(live, "_closed_today", boom)
    r = _client(db_factory).get("/api/live")
    assert r.status_code == 200
    body = r.json()
    assert [e["part"] for e in body["part_errors"]] == ["session_day", "worker", "trading", "closed_today"]
    assert body["session_day"] == body["session"]["date"]
    assert body["trading"] == "blocked"  # never claims "running" when it can't tell
    assert body["worker"]["ok"] is False and body["worker_stale"] is True
    assert body["closed_today"] == 0


@pytest.mark.parametrize("target", ["live_run", "current_session"])
def test_the_live_run_or_session_failing_is_a_500(
    db_factory: sessionmaker[Session], parts: FakeParts, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    def boom(*args: Any) -> Any:
        raise RuntimeError(BOOM)

    monkeypatch.setattr(live, target, boom)
    r = _client(db_factory).get("/api/live")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal"
    assert SECRET not in r.text


# --- 5. no Questrade ----------------------------------------------------------------------------------------


def test_the_route_never_calls_questrade(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeParts().install(monkeypatch, only_stubs=True)
    run_id = live_run(db_factory, FixedClock(NOW))
    with db_factory() as s:
        seed_session_day(s, run_id)
        s.commit()
    called: list[str] = []

    async def quotes(ids: Sequence[int]) -> Any:
        called.append("quotes")
        raise AssertionError("GET /api/live called Questrade quotes")

    async def candles(symbol_id: int, start: datetime, end: datetime) -> Any:
        called.append("candles")
        raise AssertionError("GET /api/live called Questrade candles")

    client = _client(db_factory, quotes=quotes, candles=candles)
    for params in ({}, {"range": "run"}, {"range": "today", "expand": "1,2,3"}):
        r = client.get("/api/live", params=params)
        assert r.status_code == 200, r.text
        assert r.json()["part_errors"] == [], params
    assert called == []


# --- 6. Server-Timing and off the event loop ----------------------------------------------------------------

PART_NAMES = [
    "session_day",
    "worker",
    "trading",
    "positions",
    "periods",
    "claude_today",
    "books",
    "equity",
    "risk",
    "activity",
    "rejections",
    "timeline",
    "pending",
    "closed_today",
]


def test_server_timing_lists_app_then_every_part(db_factory: sessionmaker[Session], parts: FakeParts) -> None:
    r = _client(db_factory).get("/api/live")
    header = r.headers["server-timing"]
    entries = header.split(", ")
    assert re.fullmatch(r"app;dur=\d+\.\d", entries[0]), header
    assert [e.split(";")[0] for e in entries[1:]] == PART_NAMES
    assert all(re.fullmatch(r"[a-z_]+;dur=\d+\.\d", e) for e in entries[1:]), header


def test_the_parts_run_off_the_event_loop(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    on_loop: list[bool] = []
    fakes = FakeParts()

    def spy(*args: Any, **kwargs: Any) -> list[ActivityItemOut]:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return []

    fakes.install(monkeypatch)
    monkeypatch.setattr(activity, "activity_feed", spy)
    assert _client(db_factory).get("/api/live").status_code == 200
    assert on_loop == [False]


def test_a_slow_part_does_not_block_other_requests(
    db_factory: sessionmaker[Session], parts: FakeParts
) -> None:
    parts.sleep_s["activity_feed"] = 1.0
    app = make_client(_services(db_factory), live.router, meta.router).app
    done: list[str] = []

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:

            async def get(path: str, delay: float) -> None:
                await asyncio.sleep(delay)
                r = await client.get(path)
                assert r.status_code in (200, 503), (path, r.text)
                done.append(path)

            await asyncio.gather(get("/api/live", 0), get("/api/health", 0.2))

    started = time.perf_counter()
    asyncio.run(scenario())
    assert done == ["/api/health", "/api/live"]
    assert time.perf_counter() - started >= 1.0


# --- 7. replay exclusion ------------------------------------------------------------------------------------


def _replay_rows(s: Session, live_run_id: int) -> None:
    seed_replay_noise(s, live_run_id)
    replay = add_run(s, mode="replay", status="completed", started_at=at_et(DAY, 5, 0))
    sym = add_symbol(s, "RPL2")
    s.add(
        m.QuoteMark(
            run_id=replay,
            symbol_id=sym,
            bid=Decimal("1"),
            ask=Decimal("1.1"),
            last=Decimal("1.05"),
            observed_at=NOW,
            written_at=NOW,
            is_halted=False,
        )
    )
    s.add(
        m.MarkBar(
            run_id=replay,
            symbol_id=sym,
            minute_start=NOW - timedelta(minutes=1),
            open=Decimal("1"),
            high=Decimal("1.1"),
            low=Decimal("1"),
            close=Decimal("1.05"),
            samples=3,
            updated_at=NOW,
        )
    )
    o = m.Order(
        run_id=replay,
        symbol_id=sym,
        side="buy",
        order_type="market",
        purpose="entry",
        qty=5,
        tif="day",
        status="filled",
        reason="entry",
        session_date=DAY,
        submitted_at=at_et(DAY, 10, 0),
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    _fill(s, replay, o, "1.05", "0.00", at_et(DAY, 10, 0))
    pos = m.Position(
        run_id=replay,
        symbol_id=sym,
        qty=5,
        avg_price=Decimal("1.05"),
        session_date=DAY,
        opened_at=at_et(DAY, 10, 0),
        closed_at=at_et(DAY, 11, 0),
        entry_order_id=o.id,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    s.add(
        m.Trade(
            run_id=replay,
            position_id=pos.id,
            symbol_id=sym,
            session_date=DAY,
            entry_price=Decimal("1.05"),
            exit_price=Decimal("2"),
            qty=5,
            pnl=Decimal("4.75"),
            exit_reason="flatten",
            slippage_total=Decimal(0),
            fees_total=Decimal(0),
            opened_at=pos.opened_at,
            closed_at=at_et(DAY, 11, 0),
        )
    )
    s.add(
        m.EquitySnapshot(
            run_id=replay,
            ts=NOW,
            equity=Decimal("5000"),
            cash=Decimal("5000"),
            settled_cash=Decimal("5000"),
            peak_equity=Decimal("5000"),
            drawdown_pct=Decimal(0),
        )
    )
    _event(s, replay, "critical", "engine", "replay critical", at_et(DAY, 12, 0))


def test_replay_rows_change_nothing(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeParts().install(monkeypatch, only_stubs=True)
    run_id = live_run(db_factory, FixedClock(NOW))
    with db_factory() as s:
        seed_session_day(s, run_id)
        s.commit()
    client = _client(db_factory)
    queries: list[dict[str, str]] = [{}, {"range": "run"}]
    before = [client.get("/api/live", params=q).json() for q in queries]
    with db_factory() as s:
        _replay_rows(s, run_id)
        s.commit()
    after = [client.get("/api/live", params=q).json() for q in queries]
    assert before == after
    assert all(b["part_errors"] == [] for b in before)
    assert len(before[0]["activity"]) > 10 and before[0]["rejections"]["source"] == "candidates"


# --- 8. auth ------------------------------------------------------------------------------------------------


def test_live_needs_a_session_but_no_csrf(db_factory: sessionmaker[Session], parts: FakeParts) -> None:
    services = _services(db_factory)
    r = make_client(services, live.router, user=None).get("/api/live")
    assert r.status_code == 401
    assert _client(db_factory).get("/api/live").status_code == 200  # a GET: no X-CSRF-Token header needed
