"""AFTEREXIT (Mon 2026-10-05, run 302): post-close archives 1-minute candles for every traded symbol, whatever
its rank, and the daily summary shows where each traded stock ended the session against the entry.

Monday's trades at ranks 23 to 59 (and Friday's at 22 to 25) were past postclose.archive_top_n, so the
archive had no minute bars to show what the price did after each stop-out.
"""

import dataclasses
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.jobs import test_postclose as base
from tests.jobs.test_postclose import (
    DAY,
    World,
    add_candidates,
    add_position,
    archive_rows,
    et,
    post_close,
    seed_day,
)
from trader.db import models as m
from trader.jobs.postclose import daily_summary_view, run_postclose
from trader.notify.messages import MessageRenderer
from trader.notify.types import DailySummaryView, TradeLine

pytestmark = pytest.mark.db
world = base.world  # the fixture: AAA, BBB, CCC in the universe, SPY as a symbol, an active live run


# --- the archive always covers traded symbols -------------------------------------------------------------
async def test_a_traded_symbol_past_the_top_n_gets_minute_candles(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        ranked = [add_symbol(s, f"T{i:02d}") for i in range(1, 26)]
        add_candidates(s, world.run_id, ranked)
        add_position(s, world.run_id, ranked[24], closed=True)  # rank 25: past archive_top_n (20)
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    archived = {r.symbol_id for r in archive_rows(world.factory, "1m")}
    assert archived == {*ranked[:20], ranked[24], world.ids["SPY"]}  # the top 20 are still all there


async def test_a_traded_symbol_with_no_candidate_row_gets_minute_candles(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        add_position(s, world.run_id, world.ids["BBB"], closed=True)
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    assert {r.symbol_id for r in archive_rows(world.factory, "1m")} == {world.ids["BBB"], world.ids["SPY"]}


async def test_another_days_or_another_runs_position_is_not_a_target(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        add_position(s, world.run_id, world.ids["AAA"], closed=True, session_date=date(2026, 10, 5))
        other = add_run(s, mode="replay", status="completed")
        add_position(s, other, world.ids["CCC"], closed=True)
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    assert {r.symbol_id for r in archive_rows(world.factory, "1m")} == {world.ids["SPY"]}


async def test_with_archive_top_n_zero_traded_symbols_are_still_archived(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    world.settings = world.settings.model_copy(update={"postclose_archive_top_n": 0})
    with db_factory() as s:
        add_candidates(s, world.run_id, [world.ids["AAA"]])
        add_position(s, world.run_id, world.ids["BBB"], closed=True)
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    assert {r.symbol_id for r in archive_rows(world.factory, "1m")} == {world.ids["BBB"], world.ids["SPY"]}


# --- the summary's closing price ----------------------------------------------------------------------------
async def test_the_summary_trade_carries_the_session_close(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        seed_day(s, world.run_id, world.ids["AAA"])
        s.commit()
    await run_postclose(world.deps(post_close(DAY)), DAY)
    (view,) = world.summaries()
    (trade,) = view.trades
    assert trade.ticker == "AAA"
    assert trade.session_close == Decimal("21.4000")  # the 15:59 ET bar's close (FakeData)


def test_the_session_close_is_the_last_minute_bar_of_that_et_day(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    sid = world.ids["AAA"]

    def row(d: date, h: int, mi: int, close: str) -> m.CandleArchive:
        px = Decimal(close)
        return m.CandleArchive(
            symbol_id=sid, interval="1m", start_ts=et(d, h, mi), open=px, high=px, low=px, close=px, volume=1
        )

    with db_factory() as s:
        seed_day(s, world.run_id, sid)
        s.add_all(
            [
                row(DAY, 9, 30, "20.00"),
                row(DAY, 15, 58, "22.00"),
                row(DAY, 15, 59, "22.50"),  # the day's last bar
                row(date(2026, 10, 5), 15, 59, "30.00"),  # the day before
                row(date(2026, 10, 7), 9, 30, "40.00"),  # the day after
                m.CandleArchive(  # a 5-minute bar is not a closing price
                    symbol_id=sid,
                    interval="5m",
                    start_ts=et(DAY, 15, 55),
                    open=Decimal(50),
                    high=Decimal(50),
                    low=Decimal(50),
                    close=Decimal(50),
                    volume=1,
                ),
            ]
        )
        s.commit()
    view = daily_summary_view(db_factory, world.run_id, DAY, post_close(DAY), {})
    assert [t.session_close for t in view.trades] == [Decimal("22.5000")]


def test_no_archived_minute_bars_leaves_the_close_empty(
    world: World, db_factory: sessionmaker[Session]
) -> None:
    with db_factory() as s:
        seed_day(s, world.run_id, world.ids["AAA"])
        s.commit()
    view = daily_summary_view(db_factory, world.run_id, DAY, post_close(DAY), {})
    assert [t.session_close for t in view.trades] == [None]


# --- the rendered line --------------------------------------------------------------------------------------
def _view(trade: TradeLine) -> DailySummaryView:
    return DailySummaryView(
        session_date=DAY,
        trades=(trade,),
        realized_pnl=trade.pnl,
        fees=Decimal(0),
        equity=Decimal("992.95"),
        drawdown_pct=Decimal("0.0070"),
        open_positions=(),
        decisions=0,
        avg_decision_seconds=None,
        unprotected_seconds=0,
        blocking_switches=(),
        archive={},
    )


BBWI = TradeLine(
    ticker="BBWI",
    qty=5,
    entry=Decimal("17.0300"),
    exit=Decimal("16.9000"),
    pnl=Decimal("-0.6517"),
    pnl_r=Decimal("-1.1246"),
    exit_reason="protective_stop",
)
RENDER = MessageRenderer("http://trader.home:8080", ZoneInfo("America/Edmonton"))


def test_the_trade_line_shows_where_the_stock_closed() -> None:
    up = dataclasses.replace(BBWI, session_close=Decimal("17.4100"))
    text = RENDER.daily_summary(_view(up), ()).text
    assert "protective_stop · closed 17.41 (+2.23% vs entry)" in text
    down = dataclasses.replace(BBWI, session_close=Decimal("16.5000"))
    assert "closed 16.50 (-3.11% vs entry)" in RENDER.daily_summary(_view(down), ()).text


def test_the_trade_line_is_unchanged_without_a_close() -> None:
    text = RENDER.daily_summary(_view(BBWI), ()).text
    assert "closed" not in text
    assert "BBWI 5 @ 17.03 → 16.90: " in text and text.count("protective_stop") == 1
