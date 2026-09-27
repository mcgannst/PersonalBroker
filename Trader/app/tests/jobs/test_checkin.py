"""P3-T10: the 11:30 and 13:30 ET check-ins: a status push, then a backup firing of every due event."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_telegram import FakeRenderer, RecordingNotifier
from trader.adapters.questrade.models import QtQuote
from trader.db import models as m
from trader.engine.scheduler import DayPlan, FireResult, PlannedEvent
from trader.jobs.checkin import CheckinDeps, run_checkin
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.types import StatusView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday, EDT
AT_1130 = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30:00 ET
EARLY = date(2026, 11, 27)  # 13:00 ET close
THANKSGIVING = date(2026, 11, 26)


def _plan(d: date) -> DayPlan:
    if d == EARLY:
        return DayPlan(
            d,
            True,
            datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
            datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
            (
                PlannedEvent("orb_open", datetime(2026, 11, 27, 14, 35, 5, tzinfo=UTC), ("orb_sip",), False),
                PlannedEvent("flatten", datetime(2026, 11, 27, 17, 50, tzinfo=UTC), ("orb_sip",), True),
            ),
        )
    if not CAL.is_session(d):
        return DayPlan(d, False, None, None, ())
    return DayPlan(
        d,
        True,
        datetime(2026, 10, 6, 13, 30, tzinfo=UTC),
        datetime(2026, 10, 6, 20, 0, tzinfo=UTC),
        (
            PlannedEvent("orb_open", datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC), ("orb_sip",), False),
            PlannedEvent("entry_cancel", AT_1130, ("orb_sip",), True),
            PlannedEvent(
                "overlay_decision", datetime(2026, 10, 6, 19, 30, tzinfo=UTC), ("spy_overlay",), True
            ),
            PlannedEvent("flatten", datetime(2026, 10, 6, 19, 50, tzinfo=UTC), ("orb_sip",), True),
        ),
    )


def _quote(symbol_id: int, last: str) -> QtQuote:
    return QtQuote(symbol_id, "AAA", None, None, Decimal(last), None, 0, None, 0, False, None)


class Harness:
    def __init__(self, factory: sessionmaker[Session], run_id: int, clock: FixedClock) -> None:
        self.factory = factory
        self.run_id = run_id
        self.clock = clock
        self.notifier = RecordingNotifier()
        self.render = FakeRenderer()
        self.fired: set[str] = {"orb_open"}
        self.fire_calls: list[tuple[str, date]] = []
        self.quote_prices: dict[int, str] = {}
        self.quotes_raise = False

    async def fire(self, key: str, session_date: date) -> FireResult:
        self.fire_calls.append((key, session_date))
        return FireResult(key, session_date, "fired", {})

    async def quotes(self, ids: Sequence[int]) -> Mapping[int, QtQuote]:
        if self.quotes_raise:
            raise RuntimeError("quotes down")
        return {i: _quote(i, self.quote_prices[i]) for i in ids if i in self.quote_prices}

    def deps(self) -> CheckinDeps:
        return CheckinDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=RuntimeSettings,
            run_id=self.run_id,
            notifier=self.notifier,
            render=self.render,
            plan=_plan,
            fired=lambda d: set(self.fired),
            fire=self.fire,
            quotes=self.quotes,
        )

    def views(self) -> list[tuple[StatusView, str]]:
        out = []
        for name, args in self.render.calls:
            if name == "checkin":
                assert isinstance(args[0], StatusView)
                out.append((args[0], args[1]))
        return out


@pytest.fixture
def harness(db_factory: sessionmaker[Session]) -> Harness:
    with db_factory() as s:
        run_id = add_run(s)
        s.commit()
    return Harness(db_factory, run_id, FixedClock(AT_1130))


async def test_holiday_is_skipped(harness: Harness) -> None:
    harness.clock.set(datetime(2026, 11, 26, 16, 30, tzinfo=UTC))
    detail = await run_checkin(harness.deps(), THANKSGIVING, "11:30")
    assert detail == {"skipped": "not a session"}
    assert harness.notifier.sent == [] and harness.fire_calls == []


async def test_1330_on_an_early_close_day_is_skipped(harness: Harness) -> None:
    harness.clock.set(datetime(2026, 11, 27, 18, 30, tzinfo=UTC))  # 13:30 ET, after the 13:00 close
    harness.fired = set()
    detail = await run_checkin(harness.deps(), EARLY, "13:30")
    assert detail == {"skipped": "after close"}
    assert harness.notifier.sent == [] and harness.fire_calls == []


async def test_1130_sends_one_message_with_the_label(harness: Harness) -> None:
    detail = await run_checkin(harness.deps(), DAY, "11:30")
    assert len(harness.notifier.sent) == 1
    msg = harness.notifier.sent[0]
    assert msg.kind == "checkin" and msg.dedupe_key == "checkin:2026-10-06:11:30"
    [(view, label)] = harness.views()
    assert label == "11:30"
    assert view.now == AT_1130 and view.phase == "open" and view.session_date == DAY
    assert detail["sent"] is True


async def test_running_twice_sends_one_message(harness: Harness) -> None:
    await run_checkin(harness.deps(), DAY, "11:30")
    harness.fired.add("entry_cancel")
    await run_checkin(harness.deps(), DAY, "11:30")
    assert len(harness.notifier.sent) == 1


async def test_due_entry_cancel_is_backup_fired(harness: Harness) -> None:
    detail = await run_checkin(harness.deps(), DAY, "11:30")
    assert harness.fire_calls == [("entry_cancel", DAY)]
    assert detail["fired"] == [{"key": "entry_cancel", "status": "fired"}]


async def test_already_fired_entry_cancel_is_not_fired_again(harness: Harness) -> None:
    harness.fired = {"orb_open", "entry_cancel"}
    detail = await run_checkin(harness.deps(), DAY, "11:30")
    assert harness.fire_calls == []
    assert detail["fired"] == []


async def test_status_view_from_the_database(harness: Harness) -> None:
    now = AT_1130
    with harness.factory() as s:
        aaa = add_symbol(s, "AAA")
        bbb = add_symbol(s, "BBB")
        cfg = add_strategy_config(s)
        s.add(
            m.ApiCredential(
                provider="questrade",
                refresh_token_enc="x",
                last_refresh_at=now - timedelta(hours=2),
                updated_at=now,
            )
        )
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=now - timedelta(hours=2),
                beat_at=now - timedelta(seconds=5),
                session_date=DAY,
                phase="session",
            )
        )
        protected = m.Position(
            run_id=harness.run_id,
            symbol_id=aaa,
            qty=10,
            avg_price=Decimal("20.00"),
            stop_loss=Decimal("19.50"),
            session_date=DAY,
            opened_at=now - timedelta(hours=1),
            entry_order_id=1,
            unprotected_seconds=30,
        )
        naked = m.Position(
            run_id=harness.run_id,
            symbol_id=bbb,
            qty=5,
            avg_price=Decimal("10.00"),
            stop_loss=Decimal("9.80"),
            session_date=DAY,
            opened_at=now - timedelta(minutes=10),
            entry_order_id=2,
            unprotected_since=now - timedelta(seconds=100),
            unprotected_seconds=20,
        )
        s.add_all([protected, naked])
        s.flush()
        s.add(
            m.Order(
                run_id=harness.run_id,
                position_id=protected.id,
                symbol_id=aaa,
                side="sell",
                order_type="stop",
                purpose="stop",
                qty=10,
                stop_price=Decimal("19.55"),
                tif="day",
                status="working",
                reason="protective_stop",
                session_date=DAY,
                submitted_at=now - timedelta(minutes=50),
                stale_alerted=False,
            )
        )
        sig = m.Signal(
            run_id=harness.run_id,
            strategy_config_id=cfg,
            symbol_id=aaa,
            session_date=DAY,
            event_key="orb_open",
            ts=now,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.flush()
        for status in ("pending", "submitted"):
            s.add(
                m.Proposal(
                    run_id=harness.run_id,
                    signal_id=sig.id,
                    kind="entry",
                    order_spec={},
                    qty=1,
                    status=status,
                    created_at=now,
                    expires_at=now + timedelta(minutes=5),
                    escalations=0,
                )
            )
        s.add(
            m.KillSwitchEvent(
                run_id=harness.run_id,
                switch="max_drawdown_pct",
                session_date=DAY,
                tripped_at=now - timedelta(hours=1),
            )
        )
        s.commit()
    harness.quote_prices = {aaa: "21.00"}
    await run_checkin(harness.deps(), DAY, "11:30")
    [(view, _)] = harness.views()
    assert view.next_event_key == "entry_cancel" and view.next_event_at == AT_1130
    assert view.approval_mode == "manual"
    assert view.blocking_switches == ("max_drawdown_pct",)
    assert view.token_ok is True and view.token_error is None
    assert view.token_age_hours == pytest.approx(2.0)
    assert view.heartbeat_age_seconds == pytest.approx(5.0)
    assert view.pending_count == 1
    by_ticker = {p.ticker: p for p in view.positions}
    a, b = by_ticker["AAA"], by_ticker["BBB"]
    assert a.stop == Decimal("19.55") and a.stop_working is True
    assert a.last == Decimal("21.00") and a.unrealized_pnl == Decimal("10.00")
    assert a.unprotected_seconds == 30
    assert b.stop == Decimal("9.80") and b.stop_working is False
    assert b.last is None and b.unrealized_pnl is None
    assert b.unprotected_seconds == 120


async def test_token_error_and_quote_failure_do_not_fail_the_checkin(harness: Harness) -> None:
    with harness.factory() as s:
        aaa = add_symbol(s, "AAA")
        s.add(
            m.ApiCredential(
                provider="questrade",
                refresh_token_enc="x",
                last_refresh_at=AT_1130 - timedelta(hours=30),
                last_error=None,
                updated_at=AT_1130,
            )
        )
        s.add(
            m.Position(
                run_id=harness.run_id,
                symbol_id=aaa,
                qty=10,
                avg_price=Decimal("20.00"),
                stop_loss=Decimal("19.50"),
                session_date=DAY,
                opened_at=AT_1130,
                entry_order_id=1,
                unprotected_seconds=0,
            )
        )
        s.commit()
    harness.quotes_raise = True
    harness.fired = {"orb_open", "entry_cancel"}
    await run_checkin(harness.deps(), DAY, "11:30")
    [(view, _)] = harness.views()
    assert view.token_ok is False and view.token_error is not None and "30" in view.token_error
    assert view.heartbeat_age_seconds is None
    assert view.positions[0].last is None
    assert view.next_event_key == "overlay_decision"


async def test_an_unseeded_token_is_not_ok(harness: Harness) -> None:
    await run_checkin(harness.deps(), DAY, "11:30")
    [(view, _)] = harness.views()
    assert view.token_ok is False and view.token_error


async def test_a_status_failure_still_backup_fires(harness: Harness) -> None:
    def broken_render(*args: object) -> None:
        raise RuntimeError("render bug")

    harness.render.checkin = broken_render  # type: ignore[method-assign,assignment]
    detail = await run_checkin(harness.deps(), DAY, "11:30")
    assert harness.fire_calls == [("entry_cancel", DAY)]
    assert detail["sent"] is False and "RuntimeError" in detail["error"]
    with harness.factory() as s:
        events = s.query(m.EventLog).filter(m.EventLog.source == "job.checkin").all()
    assert len(events) == 1 and events[0].level == "error"
