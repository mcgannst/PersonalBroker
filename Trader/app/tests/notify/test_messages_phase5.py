"""P5-T10: the weekly report message, the run-to-date line and the kill-switch reset wording (SPEC §4.4)."""

import dataclasses
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trader.market.clock import FixedClock
from trader.notify.messages import (
    TELEGRAM_LIMIT,
    WEB_POINTER,
    MessageRenderer,
    fmt_rate,
    fmt_signed_money,
    fmt_signed_r,
    run_to_date_lines,
)
from trader.notify.types import AlertView, DailySummaryView, RunToDateView, WeeklyReportView

MT = ZoneInfo("America/Edmonton")
BASE = "http://trader.home:8080"
T0 = datetime(2026, 10, 6, 20, 20, tzinfo=UTC)  # 16:20 ET, 14:20 MT


@pytest.fixture
def r() -> MessageRenderer:
    return MessageRenderer(BASE, MT, clock=FixedClock(T0))


def weekly(**kw: Any) -> WeeklyReportView:
    base: dict[str, Any] = dict(
        week_start=date(2026, 11, 23),
        week_ending=date(2026, 11, 27),
        trades=7,
        wins=3,
        win_rate=Decimal("0.4286"),
        expectancy_r=Decimal("0.1250"),
        total_pnl=Decimal("5.00"),
        max_drawdown_pct=Decimal("0.0314"),
        adherence_pct=Decimal("0.7500"),
        commentary="A quiet week with <b>steady</b> execution & careful stops.",
        commentary_note=None,
    )
    base.update(kw)
    return WeeklyReportView(**base)


# --- 1. the full weekly message ---
def test_weekly_report_full_view(r: MessageRenderer) -> None:
    msg = r.weekly_report(weekly())
    text = msg.text
    assert msg.kind == "weekly_report"
    assert text.startswith("<b>Weekly report</b> 2026-11-23 to 2026-11-27")
    # every headline number appears exactly once
    for token in ("Trades: 7 (3 wins, win rate 42.9%)", "+0.13R", "+$5.00", "3.14%", "75.0%"):
        assert text.count(token) == 1, token
    assert "Expectancy: +0.13R" in text
    assert "P&amp;L: +$5.00" in text
    assert "Max drawdown: 3.14%" in text
    assert "Rules followed: 75.0% of answered days" in text
    # the commentary is untrusted: escaped, so a <b> in it shows literally
    assert "with &lt;b&gt;steady&lt;/b&gt; execution &amp; careful stops." in text
    assert "<b>steady</b>" not in text
    assert f'href="{BASE}/reports?week=2026-11-27"' in text
    assert "\n\nA quiet week" in text  # a blank line before the commentary
    assert text.rstrip().endswith("</a>")


def test_weekly_report_leaves_out_lines_with_a_none_value(r: MessageRenderer) -> None:
    text = r.weekly_report(
        weekly(
            trades=0,
            wins=0,
            win_rate=None,
            expectancy_r=None,
            max_drawdown_pct=None,
            adherence_pct=None,
            total_pnl=Decimal(0),
        )
    ).text
    assert "Trades: 0" in text and "win rate" not in text
    assert "Expectancy" not in text and "Max drawdown" not in text and "Rules followed" not in text
    assert "P&amp;L: +$0.00" in text


# --- 2. note, and a long commentary ---
def test_weekly_report_without_commentary_shows_the_note_in_italics(r: MessageRenderer) -> None:
    note = "Commentary unavailable: the daily Claude budget is used up."
    text = r.weekly_report(weekly(commentary=None, commentary_note=note)).text
    assert f"<i>{note}</i>" in text
    assert "quiet week" not in text


@pytest.mark.parametrize("filler", ["word ", "a&b ", "x<y> ", "loooooooooooong "])
def test_a_long_commentary_is_cut_at_a_word_with_the_web_pointer(r: MessageRenderer, filler: str) -> None:
    commentary = (filler * 5000)[:5000]
    msg = r.weekly_report(weekly(commentary=commentary))
    text = msg.text
    assert len(text) <= TELEGRAM_LIMIT
    assert WEB_POINTER in text
    assert "(truncated, see the web app)" not in text  # the commentary was cut, not the message
    assert f'href="{BASE}/reports?week=2026-11-27"' in text  # the link survives
    body = text.split("\n\n", 1)[1].split(WEB_POINTER)[0]
    # never a partial entity: every & starts a complete escape
    assert re.search(r"&(?!(amp|lt|gt);)", body) is None
    # cut at a word boundary: the kept text is whole words of the escaped filler
    word = filler.strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert set(body.split()) == {word}
    assert "Expectancy: +0.13R" in text  # the headline is kept whole


def test_a_commentary_that_just_fits_is_not_cut(r: MessageRenderer) -> None:
    text = r.weekly_report(weekly(commentary="short and sweet")).text
    assert WEB_POINTER not in text and "short and sweet" in text


# --- 3. one formatter for the daily line and the weekly message ---
def test_formatters() -> None:
    assert fmt_rate(Decimal("0.4167")) == "41.7%"
    assert fmt_rate(Decimal("0.5")) == "50.0%"
    assert fmt_signed_r(Decimal("0.1800")) == "+0.18R"
    assert fmt_signed_r(Decimal("-0.1250")) == "-0.13R"
    assert fmt_signed_money(Decimal("23.40")) == "+$23.40"
    assert fmt_signed_money(Decimal("-2.5")) == "-$2.50"


def test_run_to_date_line_and_weekly_use_the_same_numbers(r: MessageRenderer) -> None:
    rtd = RunToDateView(
        trades=12,
        win_rate=Decimal("0.4167"),
        expectancy_r=Decimal("0.1800"),
        total_pnl=Decimal("23.40"),
        expectancy_trades=12,
        expectancy_min_trades=50,
    )
    assert run_to_date_lines(rtd) == [
        "Run to date: 12 trades, win rate 41.7%, expectancy +0.18R, P&amp;L +$23.40",
        "Expectancy switch: 12 of 50 trades",
    ]
    text = r.weekly_report(
        weekly(win_rate=Decimal("0.4167"), expectancy_r=Decimal("0.1800"), total_pnl=Decimal("23.40"))
    ).text
    assert "41.7%" in text and "+0.18R" in text and "+$23.40" in text


def test_run_to_date_line_leaves_out_none_values_and_the_armed_switch() -> None:
    rtd = RunToDateView(
        trades=0,
        win_rate=None,
        expectancy_r=None,
        total_pnl=Decimal(0),
        expectancy_trades=50,
        expectancy_min_trades=50,
    )
    assert run_to_date_lines(rtd) == ["Run to date: 0 trades, P&amp;L +$0.00"]


def summary(**kw: Any) -> DailySummaryView:
    base: dict[str, Any] = dict(
        session_date=date(2026, 10, 6),
        trades=(),
        realized_pnl=Decimal(0),
        fees=Decimal(0),
        equity=Decimal("10000"),
        drawdown_pct=Decimal("0.0100"),
        open_positions=(),
        decisions=0,
        avg_decision_seconds=None,
        unprotected_seconds=0,
        blocking_switches=(),
        archive={},
    )
    base.update(kw)
    return DailySummaryView(**base)


def test_daily_summary_shows_the_run_to_date_line_after_the_drawdown(r: MessageRenderer) -> None:
    rtd = RunToDateView(12, Decimal("0.4167"), Decimal("0.1800"), Decimal("23.40"), 12, 50)
    lines = r.daily_summary(summary(run_to_date=rtd), ()).text.split("\n")
    i = lines.index("Drawdown: 1.00%")
    assert lines[i + 1] == "Run to date: 12 trades, win rate 41.7%, expectancy +0.18R, P&amp;L +$23.40"
    assert lines[i + 2] == "Expectancy switch: 12 of 50 trades"


def test_daily_summary_without_run_to_date_is_unchanged(r: MessageRenderer) -> None:
    plain = r.daily_summary(summary(), ()).text
    assert "Run to date" not in plain
    assert plain == r.daily_summary(dataclasses.replace(summary(), run_to_date=None), ()).text


# --- the reset confirmation ---
def test_kill_switch_reset_wording(r: MessageRenderer) -> None:
    msg = r.alert(
        AlertView(
            kind="kill_switch",
            level="warning",
            source="killswitch",
            message="kill switch max_drawdown_pct reset",
            ts=T0,
            data={"switch": "max_drawdown_pct", "reason": "reviewed <ok>", "equity_at_reset": "9500.00"},
        )
    )
    lines = msg.text.split("\n")
    assert msg.kind == "kill_switch"
    assert lines[0] == "<b>KILL SWITCH RESET: max_drawdown_pct</b> at 14:20 MT"
    assert lines[1] == "Reason: reviewed &lt;ok&gt;"
    assert lines[2] == "Entries allowed again unless another switch is tripped."
    assert "blocked" not in msg.text


def test_kill_switch_reset_without_switch_in_data_reads_the_message(r: MessageRenderer) -> None:
    msg = r.alert(AlertView("kill_switch", "warning", "killswitch", "kill switch expectancy reset", T0, {}))
    assert msg.text.startswith("<b>KILL SWITCH RESET: expectancy</b>")
    assert "Reason:" not in msg.text


def test_a_trip_keeps_its_p3_wording(r: MessageRenderer) -> None:
    msg = r.alert(
        AlertView(
            "kill_switch",
            "error",
            "killswitch",
            "kill switch max_drawdown_pct tripped: entries blocked",
            T0,
            {"switch": "max_drawdown_pct", "value": "0.1", "threshold": "0.08"},
        )
    )
    assert msg.text.startswith("<b>KILL SWITCH: max_drawdown_pct</b>")
    assert "RESET" not in msg.text
