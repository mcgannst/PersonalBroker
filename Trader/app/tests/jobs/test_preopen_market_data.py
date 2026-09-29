"""FIX-401 (d): the pre-open's `market_data` check, one real market-data call. On 09-29 the token check
passed (a token was minted) while every market-data call was refused with HTTP 401."""

import dataclasses
from datetime import date
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run
from tests.jobs.test_preopen import DAY, NOW, Harness, _checks, _seed_healthy
from trader.adapters.questrade.client import QuestradeApiError
from trader.jobs.preopen import PreopenDeps, run_preopen
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db


class Probe:
    def __init__(self, result: str | None = "SPY", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[date] = []

    async def __call__(self, d: date) -> str | None:
        self.calls.append(d)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def harness(db_factory: sessionmaker[Session]) -> Harness:
    with db_factory() as s:
        run_id = add_run(s)
        _seed_healthy(s)
        s.commit()
    return Harness(db_factory, run_id, FixedClock(NOW))


def deps(h: Harness, probe: Probe) -> PreopenDeps:
    return dataclasses.replace(h.deps(), market_check=probe)


async def test_a_quote_that_answers_is_ok(harness: Harness) -> None:
    probe = Probe("SPY")
    detail = await run_preopen(deps(harness, probe), DAY)
    market = _checks(detail)["market_data"]
    assert market == {
        "name": "market_data",
        "ok": True,
        "level": "info",
        "detail": "Questrade market data OK (HTTP 200, quote SPY)",
    }
    assert probe.calls == [DAY]
    assert [c["name"] for c in detail["checks"]][:2] == ["token", "market_data"]
    assert detail["ok"] is True


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_quote_fails_and_alerts(harness: Harness, status: int) -> None:
    err = QuestradeApiError(status, "", code=1017, qt_message="Access token is invalid")
    detail = await run_preopen(deps(harness, Probe(error=err)), DAY)
    market = _checks(detail)["market_data"]
    assert market["ok"] is False and market["level"] == "error"
    assert market["detail"] == (f"Questrade refused market data: HTTP {status} 1017 Access token is invalid")
    assert detail["ok"] is False and len(harness.notifier.sent) == 1


async def test_another_failure_is_an_error_too(harness: Harness) -> None:
    detail = await run_preopen(deps(harness, Probe(error=QuestradeApiError(503, "busy"))), DAY)
    market = _checks(detail)["market_data"]
    assert market["ok"] is False and market["level"] == "error"
    assert market["detail"] == "Questrade market data call failed: HTTP 503"


async def test_nothing_to_quote_is_a_warning(harness: Harness) -> None:
    detail = await run_preopen(deps(harness, Probe(result=None)), DAY)
    market = _checks(detail)["market_data"]
    assert (market["ok"], market["level"]) == (False, "warning")


async def test_without_a_probe_the_checks_are_unchanged(harness: Harness) -> None:
    detail: dict[str, Any] = await run_preopen(harness.deps(), DAY)
    assert "market_data" not in _checks(detail)
