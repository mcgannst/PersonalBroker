"""DB-T10 acceptance test 4 (wiring): the worker's composition root puts the `QuoteTap` in front of its shared
Questrade client and runs a `MarkPublisher` beside the decisions loop (live dashboard plan S1, S1a, S10).

- `run_worker` passes a `MarkPublisher` in `WorkerDeps.marks`, reading the tap, for the live run.
- The session engines and the bot's market data get the tap (which wraps the `LazyQuestrade`).
- The heartbeat `detail` carries `rate_limit` (still from the `LazyQuestrade`), `questrade`, `candle_batches`
  (after a `candles_many`) and `marks`; a raising `health_detail` leaves its keys out, the others in, and the
  beat is still written.
- The publisher's `close` runs when the worker exits, after the worker's own shutdown.
- `marks_event_writer` writes one `event_log` row, source `marks`, and never raises.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import trader.runtime as rt
from tests.fakes_questrade import FakeQuestrade
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from tests.test_worker import _heartbeat
from trader.adapters.questrade.client import CallStats
from trader.adapters.questrade.models import CandleRequest
from trader.db import models as m
from trader.market.data_service import MarketDataService
from trader.marks.publisher import MarkPublisher
from trader.marks.tap import QuoteTap
from trader.marks.types import MARKS_SOURCE
from trader.worker import Worker, WorkerDeps

pytestmark = pytest.mark.db


class StatsQuestrade(FakeQuestrade):
    """A fake client with the real client's `stats` and `rate_limit_remaining` attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.rate_limit_remaining = {"market_data": 17, "account": 29}
        self.market = CallStats()
        self.account = CallStats()

    @property
    def stats(self) -> dict[str, CallStats]:
        return {"market": self.market, "account": self.account}

    async def candles_many(self, reqs: Sequence[CandleRequest], *, deadline_s: float | None = None) -> Any:
        self.market.requests += len(reqs)
        self.market.http_429 += 1
        self.market.pause_s += 1.5
        return await super().candles_many(reqs, deadline_s=deadline_s)


class Ctx:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt

    async def __aenter__(self) -> FakeQuestrade:
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


def live_run_id(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return int(
            s.execute(select(m.Run.id).where(m.Run.mode == "live", m.Run.status == "active")).scalar_one()
        )


async def test_run_worker_wires_the_tap_the_publisher_and_the_heartbeat(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt = StatsQuestrade()
    monkeypatch.setattr(rt, "questrade_client", lambda core: Ctx(qt))
    services: list[Any] = []

    class RecordingData(MarketDataService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            services.append(args[3] if len(args) > 3 else kwargs["client"])

    monkeypatch.setattr(rt, "MarketDataService", RecordingData)
    order: list[str] = []
    real_close = MarkPublisher.close

    def close(self: MarkPublisher) -> None:
        order.append("close")
        real_close(self)

    monkeypatch.setattr(MarkPublisher, "close", close)
    captured: list[WorkerDeps] = []
    details: dict[str, dict[str, Any]] = {}

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        deps = self.deps
        captured.append(deps)
        publisher = deps.marks
        assert isinstance(publisher, MarkPublisher)
        tap = publisher.deps.tap
        assert isinstance(tap, QuoteTap)
        # the engines' client and the bot's market data are the tap around the one LazyQuestrade
        engines = deps.engine_for.__self__  # type: ignore[attr-defined]
        assert engines._client is tap
        assert isinstance(tap._inner, rt.LazyQuestrade)
        assert services and all(c is tap for c in services)
        assert not hasattr(tap, "rate_limit_remaining")  # the heartbeat reads it from the LazyQuestrade
        # before the client opened: marks only (no rate limit, no questrade block, no batch yet)
        assert deps.heartbeat_extra is not None
        before = dict(deps.heartbeat_extra())
        assert before == {"candle_batches": [], "marks": {"written_at": None, "symbols": 0, "failing": False}}
        # a candles_many through the tap opens the client (as the 9:35 batch does)
        now = world.clock.now()
        reqs = [CandleRequest(101, now - timedelta(minutes=5), now, "OneMinute")]
        await tap.candles_many(reqs, deadline_s=45.0)
        self._beat("idle")
        hb = _heartbeat(world.factory)
        assert hb is not None
        details["full"] = dict(hb.detail)
        # a raising tap health leaves the others in, and the beat is still written (then the publisher's too)
        monkeypatch.setattr(tap, "health_detail", _boom)
        world.clock.advance(timedelta(seconds=5))
        self._beat("idle")
        hb = _heartbeat(world.factory)
        assert hb is not None and hb.beat_at == world.clock.now()
        details["no_tap"] = dict(hb.detail)
        monkeypatch.setattr(publisher, "health_detail", _boom)
        world.clock.advance(timedelta(seconds=5))
        self._beat("idle")
        hb = _heartbeat(world.factory)
        assert hb is not None and hb.beat_at == world.clock.now()
        details["neither"] = dict(hb.detail)
        order.append("run_end")

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    (deps,) = captured
    publisher = deps.marks
    assert isinstance(publisher, MarkPublisher)
    assert publisher.deps.factory is world.core.factory and publisher.deps.clock is world.core.clock
    assert publisher.deps.run_id() == live_run_id(world.factory)
    assert publisher.deps.event is not None

    full = details["full"]
    assert full["rate_limit"] == {"market_data": 17, "account": 29}
    assert full["marks"] == {"written_at": None, "symbols": 0, "failing": False}
    assert full["questrade"]["market"]["requests"] == 1 and full["questrade"]["market"]["http_429"] == 1
    (batch,) = full["candle_batches"]
    assert (batch["symbols"], batch["completed"], batch["deadline_s"], batch["http_429"]) == (1, 1, 45.0, 1)
    assert batch["pause_s"] == 1.5 and batch["raised"] is None
    assert {"fills_today", "last_event"} <= set(full)

    no_tap = details["no_tap"]
    assert "questrade" not in no_tap and "candle_batches" not in no_tap
    assert no_tap["rate_limit"] == full["rate_limit"] and no_tap["marks"] == full["marks"]
    neither = details["neither"]
    assert set(neither) == {"rate_limit", "fills_today", "last_event"}

    # the publisher is closed once the worker has finished (its tasks cancelled), with the worker's stack
    assert order == ["run_end", "close"]
    assert publisher._executor._shutdown


def _boom() -> dict[str, Any]:
    raise RuntimeError("health unreadable")


async def test_a_failing_health_call_is_logged_once_per_streak(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structlog.testing import capture_logs

    captured: list[dict[str, Any]] = []

    async def run(self: Worker, stop: Any, *, once: bool = False) -> None:
        publisher = self.deps.marks
        assert publisher is not None and self.deps.heartbeat_extra is not None
        real = publisher.health_detail
        monkeypatch.setattr(publisher, "health_detail", _boom)
        with capture_logs() as logs:
            for _ in range(3):
                captured.append(dict(self.deps.heartbeat_extra()))
            monkeypatch.setattr(publisher, "health_detail", real)
            captured.append(dict(self.deps.heartbeat_extra()))
        names = [e["event"] for e in logs]
        assert names.count("runtime.heartbeat_part_failed") == 1
        assert names.count("runtime.heartbeat_part_recovered") == 1

    monkeypatch.setattr(Worker, "run", run)
    assert await rt.run_worker(once=True) == 0
    assert [("marks" in d) for d in captured] == [False, False, False, True]


async def test_the_marks_event_writer_writes_one_row_and_never_raises(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = rt.marks_event_writer(world.core)
    run_id = live_run_id(world.factory) if _has_live_run(world.factory) else None
    write("warning", "mark publisher pass failed: boom", {"error_type": "RuntimeError"}, run_id)
    with world.factory() as s:
        rows = s.execute(select(m.EventLog).where(m.EventLog.source == MARKS_SOURCE)).scalars().all()
    assert [(r.level, r.message, r.run_id) for r in rows] == [
        ("warning", "mark publisher pass failed: boom", run_id)
    ]

    def broken(*a: Any, **k: Any) -> None:
        raise RuntimeError("database down")

    monkeypatch.setattr(rt, "log_event", broken)
    write("info", "marks recovered after 3 failures", {"failures": 3}, None)  # no exception


def _has_live_run(factory: sessionmaker[Session]) -> bool:
    with factory() as s:
        return s.execute(select(m.Run.id).where(m.Run.mode == "live")).first() is not None


def test_live_marks_builds_a_tap_and_a_publisher(world: World) -> None:  # noqa: F811
    inner = FakeQuestrade()
    tapped, publisher = rt.live_marks(world.core, inner, lambda: 7)
    try:
        assert isinstance(tapped, QuoteTap) and tapped._inner is inner
        assert isinstance(publisher, MarkPublisher) and publisher.deps.tap is tapped
        assert publisher.deps.run_id() == 7
        assert publisher.deps.factory is world.core.factory and publisher.deps.clock is world.core.clock
    finally:
        assert publisher is not None
        publisher.close()
