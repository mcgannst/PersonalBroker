"""P5-T7 acceptance test 9: the change feed's `replays` and `reports` topics, and trading-topic watermarks
that ignore a replay's rows (so a running replay never makes every open live page refetch)."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tests.api.test_feed import (
    DAY,
    NOW,
    Running,
    StepSleep,
    add_order,
    add_proposal,
    drain,
    no_msg,
    settings_with,
)
from tests.api.test_replays import add_replay
from tests.factories import add_run, add_strategy_config, add_symbol
from tests.reports import add_trade
from trader.api.deps import FeedMessage
from trader.api.feed import WATERMARK_TOPICS, PollingChangeFeed, watermarks
from trader.api.schemas import Topic
from trader.db import models as m
from trader.market.clock import FixedClock

pytestmark = pytest.mark.db

TRADING: tuple[Topic, ...] = (
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "candidates",
    "killswitch",
    "events",
)


def test_the_new_topics_are_watermarked() -> None:
    assert WATERMARK_TOPICS[-2:] == ("replays", "reports")
    assert set(TRADING) <= set(WATERMARK_TOPICS)


def _marks(factory: sessionmaker[Session]) -> dict[Topic, tuple[Any, ...]]:
    with factory() as s:
        return watermarks(s)


def _trading_rows(s: Session, run_id: int, sym: int, cfg: int) -> None:
    """One row of every trading topic for `run_id` (and a strategy config is added by the caller)."""
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=DAY,
        event_key="orb_935",
        ts=NOW,
        intent={},
        evidence={},
    )
    s.add(sig)
    s.flush()
    add_proposal(s, run_id, sig.id)
    order_id = add_order(s, run_id, sym)
    s.add(
        m.Fill(
            run_id=run_id,
            order_id=order_id,
            ts=NOW,
            qty=10,
            price=Decimal("20"),
            fees={},
            quote_snapshot={},
            slippage=Decimal("0.01"),
        )
    )
    add_trade(s, run_id, sym, DAY, "5.0000", "0.5000", config_id=cfg)  # a closed position and its trade
    s.add(
        m.Candidate(
            run_id=run_id, session_date=DAY, strategy_key="orb_sip", symbol_id=sym, rvol=None, rank=1,
            candle=None, passed=True, reject_reason=None, data=None, created_at=NOW,
        )
    )  # fmt: skip
    s.add(m.KillSwitchEvent(run_id=run_id, switch="manual_pause", session_date=DAY, tripped_at=NOW))
    s.add(m.EventLog(ts=NOW, level="error", source="engine", run_id=run_id, message="boom", data=None))
    s.flush()


def test_a_replays_rows_move_no_trading_or_strategies_watermark(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        live = add_run(s)
        sym = add_symbol(s)
        cfg = add_strategy_config(s)
        replay = add_replay(s, status="running")
        s.commit()
    before = _marks(db_factory)
    with db_factory() as s:
        _trading_rows(s, replay, sym, cfg)
        s.add(
            m.StrategyConfig(
                strategy_key="orb_sip", version="1.0.0", revision=1, params={"top_n": 3}, enabled=True,
                created_at=NOW, created_by=f"replay:{replay}", scope="replay",
            )
        )  # fmt: skip
        s.commit()
    after_replay = _marks(db_factory)
    for topic in (*TRADING, "strategies"):
        assert after_replay[topic] == before[topic], topic

    with db_factory() as s:
        _trading_rows(s, live, sym, cfg)
        s.add(
            m.StrategyConfig(
                strategy_key="orb_sip", version="1.0.0", revision=2, params={}, enabled=True,
                created_at=NOW, created_by="test", scope="live",
            )
        )  # fmt: skip
        s.commit()
    after_live = _marks(db_factory)
    for topic in (*TRADING, "strategies"):
        assert after_live[topic] != after_replay[topic], topic
    assert after_live["proposals"][3] == 1  # the live pending proposal only


def test_rows_without_a_run_still_move_the_events_watermark(db_factory: sessionmaker[Session]) -> None:
    before = _marks(db_factory)
    with db_factory() as s:
        s.add(m.EventLog(ts=NOW, level="info", source="api", run_id=None, message="hi", data=None))
        s.commit()
    assert _marks(db_factory)["events"] != before["events"]


async def test_the_feed_invalidates_replays_and_reports(db_factory: sessionmaker[Session]) -> None:
    with db_factory() as s:
        live = add_run(s)
        replay = add_replay(s, status="queued")
        s.commit()
    sleep = StepSleep()
    feed = PollingChangeFeed(db_factory, FixedClock(NOW), settings_with(0.5), sleep=sleep)
    async with feed.subscribe() as it, Running(feed):
        await sleep.started()
        await no_msg(it)

        # the replay makes progress: only `replays`
        with db_factory() as s:
            s.execute(
                update(m.Run)
                .where(m.Run.id == replay)
                .values(
                    status="running", updated_at=NOW + timedelta(seconds=30), progress={"sessions_done": 1}
                )
            )
            s.commit()
        await sleep.step()
        assert await drain(it) == [FeedMessage("invalidate", {"topics": ["replays"]})]

        # a new replay run: `replays` again
        with db_factory() as s:
            add_replay(s, status="queued", started_at=NOW)
            s.commit()
        await sleep.step()
        assert await drain(it) == [FeedMessage("invalidate", {"topics": ["replays"]})]

        # a weekly report is written, then updated: `reports`
        with db_factory() as s:
            s.add(
                m.WeeklyReport(
                    week_ending=date(2026, 10, 2), week_start=date(2026, 9, 28), run_id=live, facts={},
                    commentary=None, commentary_status="disabled", created_at=NOW, updated_at=NOW,
                )
            )  # fmt: skip
            s.commit()
        await sleep.step()
        assert await drain(it) == [FeedMessage("invalidate", {"topics": ["reports"]})]
        with db_factory() as s:
            s.execute(update(m.WeeklyReport).values(commentary="ok", updated_at=NOW + timedelta(minutes=1)))
            s.commit()
        await sleep.step()
        assert await drain(it) == [FeedMessage("invalidate", {"topics": ["reports"]})]

        # the live run's own row changing (it is not a replay) moves nothing
        with db_factory() as s:
            s.execute(update(m.Run).where(m.Run.id == live).values(label="renamed"))
            s.commit()
        await sleep.step()
        await no_msg(it)
