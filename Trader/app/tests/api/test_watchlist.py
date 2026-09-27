"""P4-T10 acceptance tests 3 and 4: `GET/POST /api/watchlist` and `DELETE /api/watchlist/{date}`."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import BASE_URL, make_client
from tests.fakes_api import FakeJobLauncher, make_services, test_core
from trader.api.launcher import already_running
from trader.api.routers import watchlist
from trader.api.schemas import JobLaunchOut, ManualJob
from trader.db import models as m
from trader.market.clock import FixedClock
from trader.market.watchlist import MAX_BYTES, get_watchlist

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET
NEXT = date(2026, 10, 7)  # the session tonight's nightly prepares
CSV = b"Symbol,Name\naapl,Apple\nBF-B,Brown-Forman\naapl,dup\n$$$,bad\n"


def _client(factory: sessionmaker[Session], jobs: FakeJobLauncher | None = None) -> TestClient:
    services = make_services(test_core(factory, FixedClock(NOW)), jobs=jobs or FakeJobLauncher())
    return make_client(services, watchlist.router)


def _upload(client: TestClient, data: bytes = CSV, filename: str = "list.csv", **fields: str) -> Any:
    return client.post("/api/watchlist", files={"file": (filename, data, "text/csv")}, data=fields)


def _audits(factory: sessionmaker[Session], action: str) -> list[m.AuditLog]:
    with factory() as s:
        return list(s.scalars(select(m.AuditLog).where(m.AuditLog.action == action).order_by(m.AuditLog.id)))


# --- acceptance test 3 ------


def test_upload_get_replace_and_delete(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = _upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["watchlist"] == {
        "session_date": "2026-10-07",
        "tickers": ["AAPL", "BF.B"],
        "filename": "list.csv",
        "uploaded_at": "2026-10-06T21:00:00Z",
        "uploaded_by": "web:stephen",
    }
    assert body["rejected"] == [
        {"row": 4, "value": "aapl", "reason": "duplicate"},
        {"row": 5, "value": "$$$", "reason": "invalid ticker"},
    ]
    assert body["launched"] is None
    assert len(_audits(db_factory, "watchlist.upload")) == 1

    got = client.get("/api/watchlist")  # no date: the next session
    assert got.status_code == 200 and got.json() == body["watchlist"]
    assert client.get("/api/watchlist", params={"date": "2026-10-07"}).json() == body["watchlist"]

    second = _upload(client, b"MSFT\n", "second.csv", date="2026-10-07")
    assert second.status_code == 200, second.text
    assert client.get("/api/watchlist").json()["tickers"] == ["MSFT"]
    assert client.get("/api/watchlist").json()["filename"] == "second.csv"

    deleted = client.delete("/api/watchlist/2026-10-07")
    assert deleted.status_code == 200 and deleted.json()["ok"] is True
    assert get_watchlist(db_factory, NEXT) is None
    assert len(_audits(db_factory, "watchlist.delete")) == 1
    missing = client.get("/api/watchlist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.delete("/api/watchlist/2026-10-07").status_code == 404


def test_today_is_allowed_a_past_date_or_holiday_is_not(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    assert _upload(client, date="2026-10-06").status_code == 200  # today's session
    for day in ("2026-10-05", "2026-11-26", "2026-10-10", "not-a-date"):  # past, Thanksgiving, Saturday
        r = _upload(client, date=day)
        assert r.status_code == 422, (day, r.text)
        assert r.json()["error"]["code"] == "validation"
    assert get_watchlist(db_factory, date(2026, 10, 5)) is None


def test_bad_files_store_nothing(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    for data in (b"$$$\n", b"", b"A\n" * (MAX_BYTES // 2 + 1), b"\xff\xfe\x00"):
        r = _upload(client, data)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "validation"
    assert get_watchlist(db_factory, NEXT) is None
    assert _audits(db_factory, "watchlist.upload") == []


def test_multipart_only(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = client.post("/api/watchlist", json={"file": "AAPL"})
    assert r.status_code == 415, r.text
    r = client.post("/api/watchlist", data={"date": "2026-10-07"})  # urlencoded, no file
    assert r.status_code == 415, r.text
    r = client.post("/api/watchlist", files={"other": ("x.csv", b"AAPL\n", "text/csv")})
    assert r.status_code == 422, r.text
    r = client.post("/api/watchlist", files={"file": (None, b"AAPL\n")})  # a plain field, not a file
    assert r.status_code == 422, r.text
    assert get_watchlist(db_factory, NEXT) is None


def test_oversized_request_is_refused_before_parsing(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = _upload(client, b"AAPL\n" * (2 * MAX_BYTES // 5))
    assert r.status_code == 422 and "larger than" in r.json()["error"]["message"]


def test_filename_is_reduced_to_its_base_name(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory)
    r = _upload(client, filename="C:\\fakepath\\" + "w" * 300 + ".csv")
    assert r.status_code == 200, r.text
    name = r.json()["watchlist"]["filename"]
    assert "\\" not in name and len(name) <= 200 and name.startswith("www")


def test_reads_need_a_session_and_writes_the_csrf_user(db_factory: sessionmaker[Session]) -> None:
    services = make_services(test_core(db_factory, FixedClock(NOW)))
    anon = make_client(services, watchlist.router, user=None)
    assert anon.get("/api/watchlist").status_code == 401
    assert _upload(anon).status_code == 401
    assert anon.delete("/api/watchlist/2026-10-07").status_code == 401


# --- acceptance test 4 ------


def test_run_nightly_launches_the_job_with_force(db_factory: sessionmaker[Session]) -> None:
    jobs = FakeJobLauncher()
    client = _client(db_factory, jobs)
    r = _upload(client, date="2026-10-07", run_nightly="true")
    assert r.status_code == 200, r.text
    assert jobs.launches == [("nightly", NEXT, True, "web:stephen")]
    assert r.json()["launched"] == {
        "job": "nightly",
        "session_date": "2026-10-07",
        "launched": True,
        "message": "nightly started",
    }


def test_run_nightly_false_launches_nothing(db_factory: sessionmaker[Session]) -> None:
    jobs = FakeJobLauncher()
    client = _client(db_factory, jobs)
    assert _upload(client, run_nightly="false").status_code == 200
    assert jobs.launches == []
    assert _upload(client, run_nightly="maybe").status_code == 422


def test_run_nightly_while_nightly_runs_stores_but_does_not_launch(db_factory: sessionmaker[Session]) -> None:
    jobs = FakeJobLauncher()
    jobs.running_jobs.add("nightly")
    client = _client(db_factory, jobs)
    r = _upload(client, run_nightly="true")
    assert r.status_code == 200, r.text
    assert jobs.launches == []
    launched = r.json()["launched"]
    assert launched["launched"] is False and "already running" in launched["message"]
    assert get_watchlist(db_factory, NEXT) is not None


# --- fix round 1 (P4-BB) ------


def test_a_chunked_upload_over_the_limit_is_refused_while_it_streams(
    db_factory: sessionmaker[Session],
) -> None:
    """No Content-Length (a chunked body): the running byte count stops the parse once it passes the file
    limit plus the form allowance, long before the whole body has been read."""
    client = _client(db_factory)
    boundary = "fixroundboundary"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode()
    chunk = b"AAPL\n" * 13_107  # about 64 KB
    total_chunks = 64  # about 4 MB in all: 16 times the limit
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        yield head
        for _ in range(total_chunks):
            sent += 1
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    async def post() -> httpx.Response:
        # httpx's ASGI transport hands the body to the app chunk by chunk, as a server does (the TestClient
        # reads it all first), so `sent` shows how much of it the app pulled.
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
            return await http.post(
                "/api/watchlist",
                content=body(),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )

    r = asyncio.run(post())
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 422 and "larger than" in r.json()["error"]["message"], r.text
    assert sent < total_chunks // 2, sent  # stopped early, not after reading everything
    assert get_watchlist(db_factory, NEXT) is None
    assert _audits(db_factory, "watchlist.upload") == []


class RacingLauncher(FakeJobLauncher):
    """`running` says no, then `launch` finds a nightly already started (another tab won the race)."""

    async def launch(
        self, job: ManualJob, session_date: date | None, force: bool, actor: str
    ) -> JobLaunchOut:
        raise already_running(job)


def test_run_nightly_losing_the_launch_race_is_still_a_200(db_factory: sessionmaker[Session]) -> None:
    client = _client(db_factory, RacingLauncher())
    r = _upload(client, run_nightly="true")
    assert r.status_code == 200, r.text
    launched = r.json()["launched"]
    assert launched["launched"] is False and "already running" in launched["message"]
    assert get_watchlist(db_factory, NEXT) is not None
