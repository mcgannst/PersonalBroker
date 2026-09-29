"""P4-T18 acceptance tests 6-8 (runtime and worker side), plus the notifier and logging changes of the Phase
4 fix rounds.

- 6: `runtime.finviz_cache_dir` follows `TRADER_FINVIZ_CACHE_DIR` (the CLI side is in test_cli_phase4.py).
- 7: `WorkerDeps.heartbeat_extra` is merged into the heartbeat `detail` (the Questrade rate-limit numbers);
  a failing callable is logged once per streak and the heartbeat is still written.
- 8: the live-run change. Trunk's worker already exits 4 (`runtime.EXIT_LIVE_RUN_CHANGED`, P3-T12 fix
  round), pinned by two tests in tests/test_runtime.py:
  `test_a_new_live_run_at_a_session_change_stops_the_worker_with_exit_4` and
  `test_a_new_live_run_mid_session_is_noticed_by_the_next_step_within_a_minute`. Here only the name.
- `TelegramNotifier.send` runs its database work (`_claim`, `_record_success`, `_record_failure`) in a
  worker thread, so the API's Telegram test route never runs sync DB work on the event loop.
"""

import asyncio
import threading
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from structlog.testing import capture_logs

import trader.runtime as rt
from tests.fakes_questrade import FakeQuestrade
from tests.fakes_telegram import FakeTelegramApi
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from tests.test_worker import SAT, Harness, _heartbeat, et
from trader import logging_setup
from trader.adapters.telegram.types import TelegramApiError
from trader.market.clock import FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import OutboundMessage
from trader.worker import Worker, WorkerDeps

# --- 6 ------------------------------------------------------------------------------------------------------


def test_finviz_cache_dir_follows_the_env_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADER_FINVIZ_CACHE_DIR", str(tmp_path / "fv"))
    assert rt.finviz_cache_dir() == tmp_path / "fv"
    monkeypatch.setenv("TRADER_FINVIZ_CACHE_DIR", "  ")
    assert rt.finviz_cache_dir() == Path.home() / ".cache" / "trader" / "finviz"
    monkeypatch.delenv("TRADER_FINVIZ_CACHE_DIR")
    assert rt.finviz_cache_dir() == Path.home() / ".cache" / "trader" / "finviz"


# --- 7. the heartbeat's extra detail ------------------------------------------------------------------------


def _worker(factory: sessionmaker[Session], extra: Any) -> Worker:
    h = Harness(factory, FixedClock(et(SAT, 11, 0)))
    return Worker(WorkerDeps(**{**vars(h.deps()), "heartbeat_extra": extra}))


@pytest.mark.db
def test_heartbeat_detail_carries_the_rate_limit_and_the_p3_keys(db_factory: sessionmaker[Session]) -> None:
    w = _worker(db_factory, lambda: {"rate_limit": {"market_data": 18, "account": 29}})
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None
    assert hb.detail["rate_limit"] == {"market_data": 18, "account": 29}
    assert hb.detail["fills_today"] == 0 and "last_event" in hb.detail


@pytest.mark.db
def test_the_p3_keys_win_over_extra_keys(db_factory: sessionmaker[Session]) -> None:
    w = _worker(db_factory, lambda: {"fills_today": 99, "rate_limit": {}})
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.detail["fills_today"] == 0 and hb.detail["rate_limit"] == {}


@pytest.mark.db
def test_a_failing_heartbeat_extra_is_logged_once_and_the_beat_is_still_written(
    db_factory: sessionmaker[Session],
) -> None:
    state = {"fail": True}

    def extra() -> dict[str, Any]:
        if state["fail"]:
            raise RuntimeError("rate limit unreadable")
        return {"rate_limit": {"market_data": 20}}

    w = _worker(db_factory, extra)
    with capture_logs() as logs:
        for _ in range(3):
            w._beat("idle")
        hb = _heartbeat(db_factory)
        assert hb is not None and hb.phase == "idle" and "rate_limit" not in hb.detail
        state["fail"] = False
        w._beat("idle")
    names = [e["event"] for e in logs]
    assert names.count("worker.heartbeat_extra_failed") == 1
    assert names.count("worker.heartbeat_extra_recovered") == 1
    hb = _heartbeat(db_factory)
    assert hb is not None and hb.detail["rate_limit"] == {"market_data": 20}


@pytest.mark.db
def test_an_unserialisable_heartbeat_extra_is_skipped(db_factory: sessionmaker[Session]) -> None:
    w = _worker(db_factory, lambda: {"rate_limit": object()})
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None and "rate_limit" not in hb.detail


@pytest.mark.db
def test_without_heartbeat_extra_the_detail_is_the_p3_one(db_factory: sessionmaker[Session]) -> None:
    w = _worker(db_factory, None)
    w._beat("idle")
    hb = _heartbeat(db_factory)
    assert hb is not None and set(hb.detail) == {"fills_today", "last_event"}


class _Qt(FakeQuestrade):
    def __init__(self) -> None:
        super().__init__()
        self.rate_limit_remaining = {"market_data": 17}


class _Ctx:
    def __init__(self, qt: FakeQuestrade) -> None:
        self.qt = qt

    async def __aenter__(self) -> FakeQuestrade:
        return self.qt

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.mark.db
async def test_lazy_questrade_reports_the_rate_limit_only_once_open(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qt = _Qt()
    monkeypatch.setattr(rt, "questrade_client", lambda core: _Ctx(qt))
    async with AsyncExitStack() as stack:
        lazy = rt.LazyQuestrade(world.core, stack)
        assert lazy.rate_limit_remaining() is None
        await lazy.client()
        assert lazy.rate_limit_remaining() == {"market_data": 17}
        qt.rate_limit_remaining["account"] = 29
        assert lazy.rate_limit_remaining() == {"market_data": 17, "account": 29}


@pytest.mark.db
async def test_run_worker_passes_the_rate_limit_as_heartbeat_extra(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[WorkerDeps] = []

    async def capture(self: Worker, stop: Any, *, once: bool = False) -> None:
        captured.append(self.deps)
        assert self.deps.heartbeat_extra is not None
        # the client has not been opened yet (DB-T10: the tap's and the publisher's health keys are there)
        assert "rate_limit" not in dict(self.deps.heartbeat_extra())

    monkeypatch.setattr(Worker, "run", capture)
    assert await rt.run_worker(once=True) == 0
    assert len(captured) == 1


def test_the_live_run_exit_code_is_4() -> None:
    assert rt.EXIT_LIVE_RUN_CHANGED == 4


# --- the notifier's database work runs off the event loop ---------------------------------------------------


async def test_notifier_database_work_runs_in_a_worker_thread() -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    api = FakeTelegramApi()
    notifier = TelegramNotifier(api, 4242, None, FixedClock(datetime(2026, 10, 6, 14, tzinfo=UTC)))  # type: ignore[arg-type]

    def claim(msg: OutboundMessage) -> tuple[int, int] | None:
        seen["claim"] = threading.get_ident()
        return (1, 1)

    def success(row_id: int | None, message_ids: list[int], calls: int) -> None:
        seen["success"] = threading.get_ident()

    def failure(msg: OutboundMessage, row_id: int | None, attempt: int, failed: Any) -> None:
        seen["failure"] = threading.get_ident()

    notifier._claim = claim  # type: ignore[method-assign]
    notifier._record_success = success  # type: ignore[method-assign]
    notifier._record_failure = failure  # type: ignore[method-assign]
    await notifier.send(OutboundMessage(kind="reply", text="hello", dedupe_key="k1"))
    original = api.send_message

    async def refuse(*args: Any, **kwargs: Any) -> int:
        raise TelegramApiError(400, "Bad Request: chat not found")

    api.send_message = refuse  # type: ignore[method-assign]
    await notifier.send(OutboundMessage(kind="reply", text="again", dedupe_key="k2"))
    api.send_message = original  # type: ignore[method-assign]
    assert set(seen) == {"claim", "success", "failure"}
    assert all(ident != loop_thread for ident in seen.values()), seen


async def test_a_cancelled_send_still_propagates() -> None:
    api = FakeTelegramApi()
    notifier = TelegramNotifier(api, 4242, None, FixedClock(datetime(2026, 10, 6, 14, tzinfo=UTC)))  # type: ignore[arg-type]
    started = threading.Event()

    def slow_claim(msg: OutboundMessage) -> tuple[int, int] | None:
        started.set()
        return None

    notifier._claim = slow_claim  # type: ignore[method-assign]
    task = asyncio.create_task(notifier.send(OutboundMessage(kind="reply", text="x", dedupe_key="k")))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- logging ------------------------------------------------------------------------------------------------


def test_httpx2_is_a_quiet_http_logger() -> None:
    assert "httpx2" in logging_setup.HTTP_LOGGERS
    import logging

    logger = logging.getLogger("httpx2")
    before = logger.level
    try:
        logger.setLevel(logging.NOTSET)
        logging_setup.quiet_http_loggers()
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(before)
