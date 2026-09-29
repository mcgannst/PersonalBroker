"""FIX-401 (Tue 2026-09-29): the 9:35 scan fetched 0 of 542 opening bars (all HTTP 401 or timeout) and
still "succeeded" with nothing said. Now:

- (a) the missing reason keeps Questrade's code and message: "questrade_error: HTTP 401 1017 ...";
- (c) the data service returns at once when the first results are all 401 (fail fast), every symbol
  marked with that 401;
- (e) the orb_open job detail records `universe`, `bars`, `missing` and the missing-reason counts, and when
  at least half of the universe is missing the engine writes one ERROR event (relayed to Telegram)
  "9:35 scan: N of M opening bars missing (top reason ...)" right after the scan.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.engine.test_orchestrator import DAY, T_ORB, build
from tests.factories import add_symbol
from trader.adapters.questrade.auth import AccessToken
from trader.adapters.questrade.client import FAIL_FAST_401, QuestradeClient
from trader.db import models as m
from trader.db.models import JobRun
from trader.engine.scheduler import FireDeps, day_plan, fire_event
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.market.data_service import MarketDataService, OpeningScan
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSip

pytestmark = pytest.mark.db
CAL = SessionCalendar()
QT_BASE = "https://api05.iq.questrade.com/v1/"
INVALID = {"code": 1017, "message": "Access token is invalid"}
REASON_401 = "questrade_error: HTTP 401 1017 Access token is invalid"


class _Tokens:
    def __init__(self) -> None:
        self.n = 0

    def access(self) -> AccessToken:
        return AccessToken(f"tok-{self.n}", QT_BASE, T_ORB + timedelta(minutes=30))

    def force_refresh(self, rejected: str | None = None) -> AccessToken:
        self.n += 1
        return self.access()


def _engine_events(factory: sessionmaker[Session], level: str) -> list[m.EventLog]:
    with factory() as s:
        return list(
            s.execute(select(m.EventLog).where(m.EventLog.level == level).order_by(m.EventLog.id)).scalars()
        )


# --- the data service ---------------------------------------------------------------------------------------


@respx.mock
async def test_all_401_opening_bars_fail_fast_with_questrades_reason(
    db_factory: sessionmaker[Session],
) -> None:
    n = FAIL_FAST_401 + 10
    with db_factory() as s:
        sids = [add_symbol(s, f"U{i:03d}", questrade_id=70_000 + i) for i in range(n)]
        s.commit()
    first = {70_000 + i for i in range(FAIL_FAST_401)}
    never = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if int(request.url.path.rsplit("/", 1)[1]) not in first:
            await never.wait()
        return httpx.Response(401, json=INVALID)

    respx.get(url__regex=rf"{QT_BASE}markets/candles/\d+").mock(side_effect=handler)
    async with QuestradeClient(_Tokens(), FixedClock(T_ORB), market_rps=1000.0) as client:
        svc = MarketDataService(db_factory, FixedClock(T_ORB), CAL, client, fetch_deadline_s=30.0)
        got = await asyncio.wait_for(svc.opening_bars(DAY, sids), timeout=5)  # never the 30 s deadline
    assert got.bars == {}
    assert got.missing == dict.fromkeys(sids, REASON_401)
    scan = svc.pop_opening_scan()
    assert scan == OpeningScan(DAY, universe=n, bars=0, missing=n, reasons={"questrade_error: HTTP 401": n})
    assert svc.pop_opening_scan() is None  # taken once


async def test_the_scan_counts_cached_and_fetched_bars(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    data = w.engine._data
    assert isinstance(data, MarketDataService)
    got = await data.opening_bars(DAY, [w.ids["AAA"], w.ids["BBB"], w.ids["SPY"]])
    assert set(got.bars) == {w.ids["AAA"], w.ids["BBB"]}
    scan = data.pop_opening_scan()
    assert scan is not None
    assert (scan.universe, scan.bars, scan.missing) == (3, 2, 1)
    assert scan.reasons == {"no_bar_at_open": 1}
    assert scan.detail() == {
        "universe": 3,
        "bars": 2,
        "missing": 1,
        "missing_reasons": {"no_bar_at_open": 1},
    }


# --- the engine: the scan detail and the ERROR event --------------------------------------------------------


async def test_a_scan_missing_half_the_universe_writes_one_error_event(
    db_factory: sessionmaker[Session],
) -> None:
    w = build(db_factory, auto=True)
    w.fq.errors[101] = 401
    w.fq.errors[102] = 401
    res = await w.engine.run_event("orb_open", DAY)
    assert res.scan == {
        "universe": 2,
        "bars": 0,
        "missing": 2,
        "missing_reasons": {"questrade_error: HTTP 401": 2},
    }
    (event,) = [e for e in _engine_events(db_factory, "error") if e.message.startswith("9:35 scan")]
    assert event.message == "9:35 scan: 2 of 2 opening bars missing (top reason questrade_error: HTTP 401)"
    assert event.data["missing"] == 2 and event.data["universe"] == 2


async def test_a_healthy_scan_records_its_counts_and_writes_no_error(
    db_factory: sessionmaker[Session],
) -> None:
    w = build(db_factory, auto=True)
    res = await w.engine.run_event("orb_open", DAY)
    assert res.scan == {"universe": 2, "bars": 2, "missing": 0, "missing_reasons": {}}
    assert [e for e in _engine_events(db_factory, "error") if e.message.startswith("9:35 scan")] == []


async def test_one_missing_bar_out_of_two_is_half_and_alerts(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    w.fq.errors[102] = 500
    res = await w.engine.run_event("orb_open", DAY)
    assert res.scan is not None and res.scan["missing"] == 1
    (event,) = [e for e in _engine_events(db_factory, "error") if e.message.startswith("9:35 scan")]
    assert event.message == "9:35 scan: 1 of 2 opening bars missing (top reason questrade_error: HTTP 500)"


async def test_an_event_without_a_scan_has_no_scan_detail(db_factory: sessionmaker[Session]) -> None:
    w = build(db_factory, auto=True)
    res = await w.engine.run_event("flatten", DAY)
    assert res.scan is None


# --- the scheduler writes the counts into the job detail ----------------------------------------------------


@pytest.mark.parametrize(
    "scan", [None, {"universe": 5, "bars": 1, "missing": 4, "missing_reasons": {"x": 4}}]
)
async def test_the_orb_open_job_detail_carries_the_scan_counts(
    db_factory: sessionmaker[Session], scan: dict[str, Any] | None
) -> None:
    class Runner:
        async def run_event(self, event_key: str, session_date: date) -> Any:
            return SimpleNamespace(strategies=["orb_sip"], outcomes=[], scan=scan)

    async def runner() -> Runner:
        return Runner()

    settings = RuntimeSettings()
    deps = FireDeps(
        factory=db_factory,
        clock=FixedClock(datetime(2026, 10, 6, 13, 35, 6, tzinfo=UTC)),
        calendar=CAL,
        settings=lambda: settings,
        plan=lambda d: day_plan([OrbSip()], CAL, d, settings),  # type: ignore[list-item]
        runner=runner,
    )
    result = await fire_event(deps, "orb_open", DAY)
    assert result.status == "fired"
    with db_factory() as s:
        detail = s.execute(select(JobRun.detail).where(JobRun.job == "event:orb_open")).scalar_one()
    expected: dict[str, Any] = {"strategies": ["orb_sip"], "outcomes": 0}
    if scan is not None:
        expected.update(scan)
    assert detail == expected
