"""QUOTEBAR wiring: the live engine builds its 9:35 opening bars from quotes; `trader openbar-check` (cron
09:47 ET) and `trader volume-scale` (the deploy-time backfill; the post-close measures it every day) run as
jobs."""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from typer.testing import CliRunner

import trader.runtime as rt
from tests.factories import add_symbol
from tests.strategies.fakes import FakeCatalysts
from tests.test_runtime import CAL, DAY, World, et, job_runs, world  # noqa: F401 (fixture)
from trader.cli import app
from trader.db import models as m
from trader.engine.orchestrator import build_engine
from trader.jobs.runner import JobOutcome
from trader.market.types import Candle

pytestmark = pytest.mark.db
OPEN = CAL.session_open(DAY)


def test_the_live_engine_takes_opening_bars_from_quotes(world: World) -> None:  # noqa: F811
    engine = build_engine(world.core, world.qt.qt, FakeCatalysts())
    assert engine._data.opening_bar_source == "quotes"  # type: ignore[attr-defined]


async def test_openbar_check_job_compares_and_records_a_job_run(world: World) -> None:  # noqa: F811
    with world.factory() as s:
        sid = add_symbol(s, "AAA", questrade_id=101)
        s.add(
            m.OpeningBarQuote(
                session_date=DAY,
                symbol_id=sid,
                captured_at=OPEN + timedelta(minutes=5, seconds=5),
                quote_time=None,
                open=Decimal("20"),
                high=Decimal("21"),
                low=Decimal("19"),
                close=Decimal("20.5"),
                quote_volume=1_400,
                volume=1_000,
                vol_factor=Decimal("0.714300"),
                factor_source="default",
            )
        )
        s.commit()
    world.qt.qt.add_bars(
        101,
        "FiveMinutes",
        [
            Candle(
                OPEN,
                OPEN + timedelta(minutes=5),
                *(Decimal(x) for x in ("20", "21", "19", "20.4")),
                1_020,
                None,
            )
        ],
    )
    world.clock.set(et(9, 47))
    out = await rt.openbar_check_job(world.core, DAY, force=False)
    assert out.status == "succeeded", out
    assert out.detail["compared"] == 1 and out.detail["prices_exact"] == 1
    assert job_runs(world, "openbar") == [("openbar_check", "succeeded", None)]


async def test_volume_scale_job_measures_the_day(world: World) -> None:  # noqa: F811
    world.clock.set(et(18, 30))
    out = await rt.volume_scale_job(world.core, DAY, force=False)
    assert out.status == "succeeded" and out.detail["symbols"] == 0
    assert job_runs(world, "volume") == [("volume_scale", "succeeded", None)]


@pytest.mark.parametrize(
    ("args", "name"),
    [
        (["openbar-check"], "openbar_check_job"),
        (["volume-scale", "--date", "2026-10-06", "--force"], "volume_scale_job"),
    ],
)
def test_cli_commands_call_the_runtime(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    name: str,
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def job(core: Any, day: Any, *, force: bool) -> JobOutcome:
        calls.append((day, force))
        return JobOutcome("succeeded", {"compared": 3})

    monkeypatch.setattr(rt, name, job)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls == [(DAY, "--force" in args)]
    assert "succeeded" in result.output and "2026-10-06" in result.output
