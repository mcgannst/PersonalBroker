"""P5-T7 acceptance test 8: the live views never show a replay's rows: the Dashboard events, `GET /api/events`
(every filter), the System errors list and the SSE `events` messages (contract refinement 8)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import make_client
from tests.api.test_feed import Running, StepSleep, drain, no_msg, settings_with
from tests.api.test_replays import add_replay
from tests.fakes_api import make_services, test_core
from trader.api.deps import FeedMessage
from trader.api.feed import PollingChangeFeed
from trader.api.routers import dashboard, system
from trader.db import models as m
from trader.engine.runs import get_live_run
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET


def _event(s: Session, run_id: int | None, message: str, level: str, minutes: int) -> int:
    ev = m.EventLog(
        ts=NOW - timedelta(minutes=minutes),
        level=level,
        source="engine",
        run_id=run_id,
        message=message,
        data=None,
    )
    s.add(ev)
    s.flush()
    return ev.id


def _seed(factory: sessionmaker[Session]) -> tuple[int, int]:
    """Events of the live run, of a replay and without a run, at info and error, interleaved by id and time.
    Returns (live run id, replay run id)."""
    live = get_live_run(factory, FixedClock(NOW), RuntimeSettings()).id
    with factory() as s:
        replay = add_replay(s, status="running")
        n = 0
        for level in ("info", "error", "critical"):
            for owner, run_id in (("live", live), ("replay", replay), ("none", None)):
                n += 1
                _event(s, run_id, f"{owner} {level}", level, 30 - n)
        s.commit()
    return live, replay


def _client(factory: sessionmaker[Session]) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)))
    return make_client(services, dashboard.router, system.router)


def _messages(items: list[dict[str, object]]) -> list[str]:
    return [str(i["message"]) for i in items]


def test_8_dashboard_events_leave_out_the_replay(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    events = _client(db_factory).get("/api/dashboard").json()["events"]
    assert _messages(events) == [
        "none critical",
        "live critical",
        "none error",
        "live error",
        "none info",
        "live info",
    ]


def test_8_the_events_list_leaves_out_the_replay_under_every_filter(
    db_factory: sessionmaker[Session],
) -> None:
    _seed(db_factory)
    client = _client(db_factory)
    everything = _messages(client.get("/api/events").json()["items"])
    assert everything == [
        "none critical",
        "live critical",
        "none error",
        "live error",
        "none info",
        "live info",
    ]
    since = _messages(client.get("/api/events", params={"since": 0}).json()["items"])
    assert since == list(reversed(everything))
    errors = _messages(client.get("/api/events", params={"level": "error"}).json()["items"])
    assert errors == ["none critical", "live critical", "none error", "live error"]
    by_source = client.get("/api/events", params={"source": "engine"}).json()["items"]
    assert all("replay" not in msg for msg in _messages(by_source)) and len(by_source) == 6
    first_id = client.get("/api/events", params={"since": 0}).json()["items"][0]["id"]
    before = client.get("/api/events", params={"before": first_id + 100}).json()["items"]
    assert all("replay" not in msg for msg in _messages(before))


def test_8_system_errors_leave_out_the_replay(db_factory: sessionmaker[Session]) -> None:
    _seed(db_factory)
    errors = _client(db_factory).get("/api/system").json()["errors"]
    assert _messages(errors) == ["none critical", "live critical", "none error", "live error"]


async def test_8_sse_events_carry_live_rows_only(db_factory: sessionmaker[Session]) -> None:
    live, replay = _seed(db_factory)
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(0.5), sleep=sleep)
    async with feed.subscribe() as it, Running(feed):
        await sleep.started()
        await no_msg(it)

        with db_factory() as s:  # a replay event alone: nothing at all
            _event(s, replay, "replay step", "error", 0)
            s.commit()
        await sleep.step()
        await no_msg(it)

        with db_factory() as s:  # a replay event, then a live one: only the live one is carried
            _event(s, replay, "replay again", "info", 0)
            _event(s, live, "live now", "warning", 0)
            _event(s, None, "no run now", "info", 0)
            s.commit()
        await sleep.step()
        msgs = await drain(it)
        assert msgs[0] == FeedMessage("invalidate", {"topics": ["events"]})
        [events] = [msg for msg in msgs if msg.kind == "events"]
        assert _messages(events.data["items"]) == ["live now", "no run now"]
