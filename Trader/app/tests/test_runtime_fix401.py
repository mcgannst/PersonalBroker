"""FIX-401 runtime wiring: (d) the pre-open check makes ONE real market-data call (a quote for SPY, else the
first universe symbol) through the app's client and a 401/403 fails the check and alerts; (g) the worker's
idle phase notices a new live run within a minute and exits 4. Fake Questrade and Telegram."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

import trader.runtime as rt
from tests.factories import add_symbol
from tests.test_runtime import DAY, World, et, replace_live_run, use_fake_engine, world  # noqa: F401
from trader.adapters.questrade.client import QuestradeApiError
from trader.db import models as m
from trader.worker import Worker

pytestmark = pytest.mark.db


def seed_universe(w: World, *, spy: bool) -> None:
    with w.factory() as s:
        for i, t in enumerate(("BBB", "AAA")):
            sid = add_symbol(s, t, questrade_id=301 + i)
            s.add(
                m.UniverseSnapshot(
                    session_date=DAY,
                    symbol_id=sid,
                    price=Decimal("20"),
                    avg_volume=2_000_000,
                    atr14=Decimal("1.0000"),
                    source="finviz",
                )
            )
        if spy:
            add_symbol(s, "SPY", questrade_id=399, exchange="ARCA")
        s.commit()


def checks(detail: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in detail["checks"]}


async def test_the_preopen_quotes_spy_once(world: World) -> None:  # noqa: F811
    seed_universe(world, spy=True)
    world.clock.set(et(9, 20))
    out = await rt.preopen_job(world.core, DAY, force=False)
    assert [c for c in world.qt.qt.calls if c[0] == "quotes"] == [("quotes", 1)]
    market = checks(out.detail)["market_data"]
    assert market["ok"] is True and "SPY" in market["detail"] and "HTTP 200" in market["detail"]
    names = [c["name"] for c in out.detail["checks"]]
    assert names[:2] == ["token", "market_data"]


async def test_without_spy_the_first_universe_symbol_is_quoted(world: World) -> None:  # noqa: F811
    seed_universe(world, spy=False)
    world.clock.set(et(9, 20))
    out = await rt.preopen_job(world.core, DAY, force=False)
    market = checks(out.detail)["market_data"]
    assert market["ok"] is True and "AAA" in market["detail"]


async def test_a_401_from_questrade_fails_the_check_and_alerts(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_universe(world, spy=True)

    async def refused(ids: Any) -> Any:
        raise QuestradeApiError(401, "", code=1017, qt_message="Access token is invalid")

    monkeypatch.setattr(world.qt.qt, "quotes", refused)
    world.clock.set(et(9, 20))
    out = await rt.preopen_job(world.core, DAY, force=False)
    market = checks(out.detail)["market_data"]
    assert market["ok"] is False and market["level"] == "error"
    assert "HTTP 401 1017 Access token is invalid" in market["detail"]
    assert out.detail["ok"] is False
    [sent] = world.api.calls_of("send_message")
    assert "market_data" in sent["text"] and "HTTP 401" in sent["text"]


async def test_no_symbol_to_probe_is_a_warning_without_a_call(world: World) -> None:  # noqa: F811
    world.clock.set(et(9, 20))
    out = await rt.preopen_job(world.core, DAY, force=False)
    market = checks(out.detail)["market_data"]
    assert market["ok"] is False and market["level"] == "warning"
    assert world.qt.entered == 0


async def test_an_idle_worker_notices_a_new_live_run_within_a_minute(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02:12 ET on a session day, as on 09-29: the worker is idle and only the idle check can see it."""
    use_fake_engine(world, monkeypatch)
    world.clock.set(et(2, 11))
    seen: dict[str, Any] = {}

    async def new_run_at_night(self: Worker, stop: Any, *, once: bool = False) -> None:
        await self.step()
        replace_live_run(world)
        world.clock.set(et(2, 11, 30))
        await self.step()
        seen["early"] = stop.is_set()  # checked 30 s ago: not yet
        world.clock.set(world.clock.now() + timedelta(seconds=30))
        await self.step()
        seen["later"] = stop.is_set()

    monkeypatch.setattr(Worker, "run", new_run_at_night)
    assert await rt.run_worker(once=True) == rt.EXIT_LIVE_RUN_CHANGED
    assert seen == {"early": False, "later": True}
    assert world.runner.events == [] and world.engines_opened == []
