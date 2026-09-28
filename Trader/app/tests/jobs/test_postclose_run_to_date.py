"""P5-T10: the daily summary's run-to-date line (BR-60), built from trader.reports.metrics."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeRenderer, RecordingNotifier
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs import postclose
from trader.jobs.postclose import PostcloseDeps, daily_summary_view, run_postclose
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.market.types import Candle, Interval, OpeningBars, UniverseMember
from trader.notify.messages import run_to_date_lines
from trader.notify.types import DailySummaryView, RunToDateView
from trader.reports.metrics import Metrics
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
DAY = date(2026, 10, 6)
CAL = SessionCalendar()


def et(d: date, h: int, mi: int) -> datetime:
    return datetime.combine(d, time(h, mi), tzinfo=ET).astimezone(UTC)


NOW = et(DAY, 16, 15)


def metrics(run_id: int, **kw: Any) -> Metrics:
    base: dict[str, Any] = dict(
        run_id=run_id,
        date_from=None,
        date_to=DAY,
        trades=12,
        wins=5,
        losses=7,
        win_rate=Decimal("0.4167"),
        avg_win_r=Decimal("1.2000"),
        avg_loss_r=Decimal("-0.5500"),
        expectancy_r=Decimal("0.1800"),
        profit_factor=Decimal("1.5000"),
        avg_slippage=Decimal("0.0200"),
        avg_slippage_per_share=Decimal("0.0002"),
        max_drawdown_pct=Decimal("0.0300"),
        adherence_pct=None,
        total_pnl=Decimal("23.40"),
        total_fees=Decimal("12.00"),
        trades_without_r=0,
        r_histogram=(),
    )
    base.update(kw)
    return Metrics(**base)


@pytest.fixture
def run_id(db_factory: sessionmaker[Session]) -> int:
    with session_scope(db_factory) as s:
        return add_run(s)


def patch_metrics(monkeypatch: pytest.MonkeyPatch, result: Metrics, calls: list[tuple[Any, ...]]) -> None:
    def fake(
        factory: Any, run_id: int, date_from: date | None = None, date_to: date | None = None
    ) -> Metrics:
        calls.append((run_id, date_from, date_to))
        return result

    monkeypatch.setattr(postclose, "compute_metrics", fake)


def test_run_to_date_is_filled_from_the_metrics(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    patch_metrics(monkeypatch, metrics(run_id), calls)
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert calls == [(run_id, None, DAY)]
    assert view.run_to_date == RunToDateView(
        trades=12,
        win_rate=Decimal("0.4167"),
        expectancy_r=Decimal("0.1800"),
        total_pnl=Decimal("23.40"),
        expectancy_trades=12,
        expectancy_min_trades=50,
    )


def test_trades_without_r_do_not_count_for_the_expectancy_switch(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_metrics(monkeypatch, metrics(run_id, trades_without_r=2), [])
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert view.run_to_date is not None and view.run_to_date.expectancy_trades == 10


def add_trade(s: Session, run_id: int, symbol_id: int, closed_at: datetime, pnl_r: str | None) -> None:
    pos = m.Position(
        run_id=run_id,
        symbol_id=symbol_id,
        qty=10,
        avg_price=Decimal("20"),
        stop_loss=Decimal("19.5"),
        planned_risk=Decimal("5"),
        session_date=DAY,
        opened_at=closed_at - timedelta(minutes=30),
        closed_at=closed_at,
        entry_order_id=1,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    s.add(
        m.Trade(
            run_id=run_id,
            position_id=pos.id,
            symbol_id=symbol_id,
            session_date=DAY,
            entry_price=Decimal("20"),
            exit_price=Decimal("21"),
            qty=10,
            pnl=Decimal("10"),
            pnl_r=Decimal(pnl_r) if pnl_r is not None else None,
            planned_risk=Decimal("5"),
            exit_reason="flatten_close",
            slippage_total=Decimal(0),
            fees_total=Decimal(0),
            opened_at=closed_at - timedelta(minutes=30),
            closed_at=closed_at,
        )
    )


def test_after_an_expectancy_reset_only_later_trades_count(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch re-arms on the trades with an R multiple closed since its last reset (killswitch.py)."""
    reset_at = et(DAY, 11, 0)
    with session_scope(db_factory) as s:
        sid = add_symbol(s, "AAA")
        add_trade(s, run_id, sid, et(DAY, 10, 0), "1")  # before the reset
        add_trade(s, run_id, sid, et(DAY, 12, 0), "1")
        add_trade(s, run_id, sid, et(DAY, 13, 0), None)  # no R: not counted
        add_trade(s, run_id, sid, et(DAY, 14, 0), "-1")
        s.add(
            m.KillSwitchEvent(
                run_id=run_id,
                switch="expectancy",
                session_date=DAY,
                tripped_at=et(DAY, 10, 5),
                reset_at=reset_at,
                reset_reason="reviewed",
                reset_by="web:stephen",
            )
        )
    patch_metrics(monkeypatch, metrics(run_id, trades=4, trades_without_r=1), [])
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert view.run_to_date is not None and view.run_to_date.expectancy_trades == 2


def test_a_reset_after_the_session_day_does_not_count(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1: a summary rebuilt for DAY after a later reset counts as the switch did on DAY (the reset
    window is bounded by the end of the session's ET day, as the weekly facts count it)."""
    with session_scope(db_factory) as s:
        sid = add_symbol(s, "AAA")
        add_trade(s, run_id, sid, et(DAY, 10, 0), "1")
        add_trade(s, run_id, sid, et(DAY, 12, 0), "-1")
        s.add(
            m.KillSwitchEvent(
                run_id=run_id,
                switch="expectancy",
                session_date=DAY,
                tripped_at=et(DAY, 10, 5),
                reset_at=et(DAY + timedelta(days=1), 9, 0),
                reset_reason="reviewed",
                reset_by="web:stephen",
            )
        )
    patch_metrics(monkeypatch, metrics(run_id, trades=2, trades_without_r=0), [])
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert view.run_to_date is not None and view.run_to_date.expectancy_trades == 2


def test_run_to_date_line_says_one_trade(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_metrics(monkeypatch, metrics(run_id, trades=1, trades_without_r=0), [])
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert view.run_to_date is not None
    assert run_to_date_lines(view.run_to_date)[0].startswith("Run to date: 1 trade, ")


def test_a_failing_metrics_call_leaves_run_to_date_empty(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Metrics:
        raise NotImplementedError("P5-T2")

    monkeypatch.setattr(postclose, "compute_metrics", boom)
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {}, expectancy_min_trades=50)
    assert view.run_to_date is None
    assert view.trades == () and view.realized_pnl == Decimal(0)


def test_without_the_keyword_no_metrics_are_computed(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    patch_metrics(monkeypatch, metrics(run_id), calls)
    view = daily_summary_view(db_factory, run_id, DAY, NOW, {})
    assert view.run_to_date is None and calls == []


# --- run_postclose passes the setting ---
class _NoData:
    async def universe(self, session_date: date) -> list[UniverseMember]:
        return []

    async def candles(
        self, symbol_id: int, start: datetime, end: datetime, interval: Interval
    ) -> list[Candle]:
        return []

    async def opening_bars(self, session_date: date, symbol_ids: Any = None) -> OpeningBars:
        return OpeningBars({}, {})


class _Engine:
    async def run_event(self, event_key: str, session_date: date) -> Any:
        raise AssertionError("not used")

    async def poll_quotes(self) -> list[Any]:
        return []

    async def tick(self, now: datetime) -> None:
        return None

    async def end_of_session(self, session_date: date) -> list[Any]:
        return []


async def test_run_postclose_sends_the_line_with_the_configured_minimum(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_metrics(monkeypatch, metrics(run_id), [])
    render = FakeRenderer()
    settings = RuntimeSettings().model_copy(update={"killswitch_expectancy_min_trades": 30})
    deps = PostcloseDeps(
        factory=db_factory,
        clock=FixedClock(NOW),
        calendar=CAL,
        settings=lambda: settings,
        engine=_Engine(),
        data=_NoData(),
        notifier=RecordingNotifier(),
        render=render,
        issuer=FakeIssuer(),
        chat_id=42,
        run_id=run_id,
    )
    out = await run_postclose(deps, DAY)
    assert out["summary_sent"] is True
    [view] = [args[0] for name, args in render.calls if name == "daily_summary"]
    assert isinstance(view, DailySummaryView)
    assert view.run_to_date is not None
    assert (view.run_to_date.expectancy_trades, view.run_to_date.expectancy_min_trades) == (12, 30)


async def test_run_postclose_with_the_metrics_stub_still_sends_the_summary(
    db_factory: sessionmaker[Session], run_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Metrics:
        raise RuntimeError("database away")

    monkeypatch.setattr(postclose, "compute_metrics", boom)
    render = FakeRenderer()
    deps = PostcloseDeps(
        factory=db_factory,
        clock=FixedClock(NOW),
        calendar=CAL,
        settings=RuntimeSettings,
        engine=_Engine(),
        data=_NoData(),
        notifier=RecordingNotifier(),
        render=render,
        issuer=FakeIssuer(),
        chat_id=42,
        run_id=run_id,
    )
    out = await run_postclose(deps, DAY)
    assert out["summary_sent"] is True
    [view] = [args[0] for name, args in render.calls if name == "daily_summary"]
    assert view.run_to_date is None
