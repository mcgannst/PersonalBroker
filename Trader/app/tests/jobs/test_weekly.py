"""P5-T9: the weekly report job body: commentary with its number check and one retry, the budget, the stored
row and one Telegram message per week (BR-61; SPEC §4.3, §9; Review Focus 4)."""

import json
import re
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.adapters.test_claude_reports import API_KEY, FakeClient, reply
from tests.factories import add_run
from tests.fakes_telegram import FakeRenderer, FakeTelegramApi, RecordingNotifier
from tests.reports.test_weekly import (
    CAL,
    FRI,
    MON,
    SAT,
    WED,
    NoSessionsIn,
    add_catalyst_cost,
    install_fake_metrics,
)
from trader.adapters.claude.catalyst import cost_usd
from trader.adapters.claude.reports import CommentaryWriter
from trader.db import models as m
from trader.jobs.weekly import WeeklyDeps, run_weekly
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.types import Notifier, WeeklyReportView
from trader.reports.weekly import WeekWindow, claude_spent, week_window
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
NOW = datetime.combine(SAT, time(9, 0), tzinfo=ET).astimezone(UTC)
CHAT = 42
FILLER = (
    "The account kept to its written plan on most days and the rules were followed with care. "
    "Risk stayed contained, no position was carried overnight, and the stops were in place quickly. "
    "Next week the same checklist applies before every entry, and each trade is reviewed after the close "
    "so that any slip in discipline is caught early rather than late."
)
CLEAN = f"The week ending November 27 had 4 trades and a 50% win rate, for +0.13R per trade. {FILLER}"
BAD = f"The week had 7 trades and made $12.34. {FILLER}"
CLEAN_COST = cost_usd("claude-sonnet-5", 3000, 400)  # the default reply: US$0.01


async def no_sleep(_: float) -> None:
    return None


class World:
    def __init__(self, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
        self.factory = factory
        with factory() as s:
            self.run_id = add_run(s)
            s.commit()
        self.metrics = install_fake_metrics(monkeypatch)
        self.notifier: Notifier = RecordingNotifier()
        self.render = FakeRenderer()
        self.client: FakeClient | None = None
        self.settings = RuntimeSettings()

    def deps(
        self,
        *replies: Any,
        calendar: SessionCalendar = CAL,
        no_writer: bool = False,
        **settings: Any,
    ) -> WeeklyDeps:
        self.settings = RuntimeSettings(**settings)
        s = self.settings
        writer = None
        if not no_writer:
            self.client = FakeClient(*replies)
            writer = CommentaryWriter(self.client, lambda: s)
        return WeeklyDeps(
            self.factory,
            FixedClock(NOW),
            calendar,
            lambda: s,
            writer,
            self.notifier,
            self.render,
            self.run_id,
        )

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.client.messages.calls if self.client else []

    def row(self) -> m.WeeklyReport:
        with self.factory() as s:
            rows = s.execute(select(m.WeeklyReport)).scalars().all()
        assert len(rows) == 1
        return rows[0]

    def views(self) -> list[WeeklyReportView]:
        return [args[0] for name, args in self.render.calls if name == "weekly_report"]

    def sent(self) -> list[Any]:
        assert isinstance(self.notifier, RecordingNotifier)
        return self.notifier.sent


@pytest.fixture
def world(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> World:
    return World(db_factory, monkeypatch)


def week() -> WeekWindow:
    return week_window(CAL, WED)


def user_text(call: dict[str, Any]) -> str:
    return str(call["messages"][0]["content"])


# --- the happy path -----------------------------------------------------------------------------------------
async def test_clean_commentary_is_stored_and_sent(world: World) -> None:
    detail = await run_weekly(world.deps(reply(CLEAN)), week())
    assert detail == {
        "week_ending": "2026-11-27",
        "commentary_status": "ok",
        "cost_usd": str(CLEAN_COST),
        "trades": 4,
        "sent": "handed_off",
    }
    assert len(world.calls) == 1
    row = world.row()
    assert row.commentary == CLEAN and row.commentary_status == "ok" and row.commentary_error is None
    assert row.model == "claude-sonnet-5" and (row.input_tokens, row.output_tokens) == (3000, 400)
    assert row.cost_usd == CLEAN_COST and row.run_id == world.run_id and row.week_start == MON
    assert row.facts["week"] == {"start": "2026-11-23", "end": "2026-11-27", "sessions": 4}
    (msg,) = world.sent()
    assert msg.kind == "weekly_report" and msg.dedupe_key == "weekly:2026-11-27"
    (view,) = world.views()
    assert view == WeeklyReportView(
        week_start=MON,
        week_ending=FRI,
        trades=4,
        wins=2,
        win_rate=Decimal("0.5000"),
        expectancy_r=Decimal("0.1250"),
        total_pnl=Decimal("5.0000"),
        max_drawdown_pct=Decimal("0.0310"),
        adherence_pct=Decimal("0.7500"),
        commentary=CLEAN,
        commentary_note=None,
    )


async def test_week_without_sessions_does_nothing(world: World) -> None:
    cal = NoSessionsIn(MON, FRI)
    w = week_window(cal, WED)
    assert await run_weekly(world.deps(reply(CLEAN), calendar=cal), w) == {"skipped": "no sessions"}
    assert world.calls == [] and world.sent() == []
    with world.factory() as s:
        assert s.execute(select(m.WeeklyReport)).first() is None


# --- 4. the number check and its one retry ------------------------------------------------------------------
async def test_made_up_number_then_clean_retry_is_ok(world: World) -> None:
    detail = await run_weekly(world.deps(reply(BAD), reply(CLEAN)), week())
    assert detail["commentary_status"] == "ok"
    assert len(world.calls) == 2
    assert "must not" not in user_text(world.calls[0])
    assert "7, 12.34" in user_text(world.calls[1])
    row = world.row()
    assert row.commentary == CLEAN and row.cost_usd == 2 * CLEAN_COST
    assert (row.input_tokens, row.output_tokens) == (6000, 800)
    assert world.views()[0].commentary == CLEAN


async def test_two_bad_commentaries_are_rejected_and_the_report_still_goes_out(world: World) -> None:
    worse = f"There were 9 trades. {FILLER}"
    detail = await run_weekly(world.deps(reply(BAD), reply(worse)), week())
    assert detail["commentary_status"] == "rejected" and len(world.calls) == 2
    row = world.row()
    assert row.commentary is None and row.commentary_status == "rejected"
    assert row.commentary_error is not None
    for token in ("7", "12.34", "9"):
        assert token in row.commentary_error
    assert row.cost_usd == 2 * CLEAN_COST
    (view,) = world.views()
    assert view.commentary is None
    assert view.commentary_note == "Commentary unavailable: it quoted numbers not in the report."
    assert len(world.sent()) == 1


async def test_wrong_length_is_rejected(world: World) -> None:
    short = "The week had 4 trades."
    detail = await run_weekly(world.deps(reply(short), reply(short)), week())
    assert detail["commentary_status"] == "rejected" and len(world.calls) == 2
    row = world.row()
    assert row.commentary is None and row.commentary_error is not None
    assert "length" in row.commentary_error
    assert world.views()[0].commentary_note == "Commentary unavailable: it was not the expected length."


# --- 5. the budget ------------------------------------------------------------------------------------------
async def test_budget_used_up_means_no_call(world: World) -> None:
    add_catalyst_cost(world.factory, SAT, "0.970000")
    detail = await run_weekly(world.deps(reply(CLEAN)), week())
    assert detail["commentary_status"] == "budget" and detail["cost_usd"] == "0"
    assert world.calls == []
    row = world.row()
    assert row.commentary is None and row.commentary_status == "budget" and row.cost_usd == 0
    (view,) = world.views()
    assert view.commentary_note == "Commentary unavailable: the daily Claude budget is used up."
    assert len(world.sent()) == 1


async def test_budget_left_allows_one_call_counted_the_same_day(world: World) -> None:
    add_catalyst_cost(world.factory, SAT, "0.900000")
    detail = await run_weekly(world.deps(reply(CLEAN)), week())
    assert detail["commentary_status"] == "ok" and len(world.calls) == 1
    assert world.row().cost_usd == CLEAN_COST
    assert claude_spent(world.factory, SAT) == Decimal("0.9") + CLEAN_COST


async def test_expensive_first_call_gets_no_retry(world: World) -> None:
    add_catalyst_cost(world.factory, SAT, "0.900000")
    pricey = reply(BAD, tin=10000, tout=1000)  # US$0.03: a retry would take the report to 0.06 > 0.05
    detail = await run_weekly(world.deps(pricey, reply(CLEAN)), week())
    assert detail["commentary_status"] == "rejected" and len(world.calls) == 1
    row = world.row()
    assert row.cost_usd == Decimal("0.03") and row.input_tokens == 10000
    assert row.commentary is None and "7" in (row.commentary_error or "")


async def test_retry_at_exactly_half_the_cap_keeps_the_report_within_it(world: World) -> None:
    half = reply(BAD, tin=7500, tout=1000)  # US$0.025: first + retry = 0.05, the cap exactly
    detail = await run_weekly(world.deps(half, reply(CLEAN, tin=7500, tout=1000)), week())
    assert detail["commentary_status"] == "ok" and len(world.calls) == 2
    assert world.row().cost_usd == Decimal("0.05") <= world.settings.reports_weekly_max_cost_usd


async def test_retry_needs_the_daily_budget_with_the_first_call_counted(world: World) -> None:
    add_catalyst_cost(world.factory, SAT, "0.945000")  # 0.055 left: the first call only
    detail = await run_weekly(world.deps(reply(BAD), reply(CLEAN)), week())
    assert detail["commentary_status"] == "rejected" and len(world.calls) == 1


async def test_budget_of_another_day_does_not_count(world: World) -> None:
    add_catalyst_cost(world.factory, FRI, "0.990000")
    detail = await run_weekly(world.deps(reply(CLEAN)), week())
    assert detail["commentary_status"] == "ok"


# --- 6. disabled --------------------------------------------------------------------------------------------
async def test_commentary_setting_off_means_no_call(world: World) -> None:
    detail = await run_weekly(world.deps(reply(CLEAN), reports_weekly_commentary=False), week())
    assert detail["commentary_status"] == "disabled" and world.calls == []
    row = world.row()
    assert row.commentary_status == "disabled" and row.commentary_error is None
    assert world.views()[0].commentary_note == "Commentary unavailable: it is turned off in Settings."
    assert len(world.sent()) == 1


async def test_no_client_is_disabled_with_the_reason(world: World) -> None:
    detail = await run_weekly(world.deps(no_writer=True), week())
    assert detail["commentary_status"] == "disabled"
    row = world.row()
    assert row.commentary_status == "disabled" and row.commentary_error == "ANTHROPIC_API_KEY not set"
    assert world.views()[0].commentary_note == "Commentary unavailable: Claude is not configured."
    assert len(world.sent()) == 1


# --- 7. errors ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "first",
    [RuntimeError(f"boom\nkey={API_KEY}"), reply(CLEAN, stop="max_tokens")],
    ids=["sdk-raises", "max-tokens"],
)
async def test_claude_failure_is_an_error_and_the_report_goes_out(world: World, first: Any) -> None:
    detail = await run_weekly(world.deps(first), week())
    assert detail["commentary_status"] == "error" and len(world.calls) == 1
    row = world.row()
    assert row.commentary is None and row.commentary_error is not None
    assert "\n" not in row.commentary_error and API_KEY not in row.commentary_error
    assert world.views()[0].commentary_note == "Commentary unavailable: Claude failed."
    assert len(world.sent()) == 1


async def test_failed_retry_is_an_error(world: World) -> None:
    detail = await run_weekly(world.deps(reply(BAD), RuntimeError("down")), week())
    assert detail["commentary_status"] == "error" and len(world.calls) == 2
    row = world.row()
    assert row.cost_usd == CLEAN_COST and "down" in (row.commentary_error or "")


# --- 8. once per week ---------------------------------------------------------------------------------------
async def test_twice_for_one_week_sends_once_keeps_one_row_and_accumulates_cost(world: World) -> None:
    api = FakeTelegramApi()
    world.notifier = TelegramNotifier(api, CHAT, world.factory, FixedClock(NOW), sleep=no_sleep)
    first = await run_weekly(world.deps(reply(CLEAN)), week())
    second = await run_weekly(world.deps(reply(CLEAN)), week())
    assert first["sent"] == "sent" and second["sent"] == "duplicate"
    assert len(api.calls_of("send_message")) == 1
    with world.factory() as s:
        keys = s.execute(select(m.Notification.dedupe_key)).scalars().all()
    assert keys == ["weekly:2026-11-27"]
    row = world.row()
    assert row.cost_usd == 2 * CLEAN_COST and row.commentary == CLEAN


async def test_recording_notifier_also_drops_the_second_message(world: World) -> None:
    await run_weekly(world.deps(reply(CLEAN)), week())
    await run_weekly(world.deps(reply(CLEAN)), week())
    assert len(world.sent()) == 1 and len(world.views()) == 2


async def test_facts_sent_to_claude_are_the_stored_facts(world: World) -> None:
    await run_weekly(world.deps(reply(CLEAN)), week())
    row = world.row()
    block = re.search(r"<facts>\n(.*)\n</facts>", user_text(world.calls[0]), re.DOTALL)
    assert block is not None and json.loads(block.group(1)) == row.facts
    # the job asked for the week's and the run-to-date metrics
    assert (world.run_id, MON, FRI) in world.metrics.calls and (
        world.run_id,
        None,
        FRI,
    ) in world.metrics.calls
