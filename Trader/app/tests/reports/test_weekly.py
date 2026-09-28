"""P5-T9: the weekly report's week, facts, number check, Claude spend and stored row (BR-61; SPEC §4.3)."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_symbol
from tests.reports import add_trade
from trader.db import models as m
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.reports import weekly
from trader.reports.metrics import Metrics
from trader.reports.weekly import (
    CommentaryOutcome,
    WeekWindow,
    build_facts,
    check_numbers,
    claude_spent,
    last_completed_week,
    upsert_report,
    week_window,
)
from trader.settings_store import RuntimeSettings

CAL = SessionCalendar()
MON = date(2026, 11, 23)
WED = date(2026, 11, 25)
THANKSGIVING = date(2026, 11, 26)
FRI = date(2026, 11, 27)
SAT = date(2026, 11, 28)
NEXT_MON = date(2026, 11, 30)
WEEK_SESSIONS = (MON, date(2026, 11, 24), WED, FRI)
SAT_0900 = datetime.combine(SAT, time(9, 0), tzinfo=ET).astimezone(UTC)


# --- shared helpers (also used by tests/jobs/test_weekly.py) -----------------------------------------------
def make_metrics(run_id: int, date_from: date | None, date_to: date | None, **kw: Any) -> Metrics:
    """A Metrics value with every field set (defaults: the "4 trades, 50% win rate" week)."""
    base: dict[str, Any] = {
        "run_id": run_id,
        "date_from": date_from,
        "date_to": date_to,
        "trades": 4,
        "wins": 2,
        "losses": 2,
        "win_rate": Decimal("0.5000"),
        "avg_win_r": Decimal("1.2500"),
        "avg_loss_r": Decimal("-1.0000"),
        "expectancy_r": Decimal("0.1250"),
        "profit_factor": Decimal("1.2500"),
        "avg_slippage": Decimal("0.0200"),
        "avg_slippage_per_share": Decimal("0.0020"),
        "max_drawdown_pct": Decimal("0.0310"),
        "adherence_pct": Decimal("0.7500"),
        "total_pnl": Decimal("5.0000"),
        "total_fees": Decimal("0.0000"),
        "trades_without_r": 0,
        "r_histogram": (),
    }
    base.update(kw)
    return Metrics(**base)


class FakeMetrics:
    """A table-driven stand-in for `compute_metrics` (P5-T2): {(date_from, date_to): overrides}; any other
    range gets the defaults. Records every call."""

    def __init__(
        self, table: Mapping[tuple[date | None, date | None], Mapping[str, Any]] | None = None
    ) -> None:
        self.table = dict(table or {})
        self.calls: list[tuple[int, date | None, date | None]] = []

    def __call__(
        self,
        factory: sessionmaker[Session],
        run_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> Metrics:
        self.calls.append((run_id, date_from, date_to))
        return make_metrics(run_id, date_from, date_to, **self.table.get((date_from, date_to), {}))


def install_fake_metrics(
    monkeypatch: pytest.MonkeyPatch,
    table: Mapping[tuple[date | None, date | None], Mapping[str, Any]] | None = None,
) -> FakeMetrics:
    fake = FakeMetrics(table)
    monkeypatch.setattr(weekly, "compute_metrics", fake)
    return fake


def the_week() -> WeekWindow:
    return week_window(CAL, WED)


# --- 1. the week --------------------------------------------------------------------------------------------
def test_week_window_thanksgiving_week() -> None:
    for day in (WED, SAT, date(2026, 11, 29), MON):
        w = week_window(CAL, day)
        assert w == WeekWindow(MON, FRI, WEEK_SESSIONS, FRI), day
        assert len(w.sessions) == 4 and THANKSGIVING not in w.sessions


def test_last_completed_week() -> None:
    assert last_completed_week(CAL, SAT) == week_window(CAL, WED)
    assert last_completed_week(CAL, date(2026, 11, 29)) == week_window(CAL, WED)  # Sunday
    assert last_completed_week(CAL, NEXT_MON) == week_window(CAL, WED)
    assert last_completed_week(CAL, FRI) == week_window(CAL, date(2026, 11, 18))  # the week before


class NoSessionsIn(SessionCalendar):
    def __init__(self, first: date, last: date) -> None:
        super().__init__()
        self.first, self.last = first, last

    def is_session(self, d: date) -> bool:
        return not (self.first <= d <= self.last) and super().is_session(d)


def test_week_without_sessions_has_no_week_ending() -> None:
    w = week_window(NoSessionsIn(MON, FRI), WED)
    assert w == WeekWindow(MON, FRI, (), None)


# --- 2. facts -----------------------------------------------------------------------------------------------
def seed_week(factory: sessionmaker[Session]) -> int:
    """The live run: trades in the week and the week before, journal answers, kill-switch trips."""
    with factory() as s:
        run = add_run(s)
        aaa, bbb = add_symbol(s, "AAA"), add_symbol(s, "BBB")
        add_trade(s, run, aaa, date(2026, 11, 16), "100.0000", "3.0000")  # the week before: not best
        add_trade(s, run, aaa, MON, "10.0000", "1.0000")
        add_trade(s, run, bbb, MON, "-2.0000", None)  # no R: never best or worst
        add_trade(s, run, bbb, date(2026, 11, 24), "-5.0000", "-0.5000")
        add_trade(s, run, aaa, WED, "2.0000", "0.2000")
        s.add_all(
            [
                m.Journal(run_id=run, session_date=MON, rules_followed=True),
                m.Journal(run_id=run, session_date=date(2026, 11, 24), rules_followed=False),
                m.Journal(run_id=run, session_date=WED, rules_followed=None),
            ]
        )
        for switch, day, value, threshold in (
            ("daily_loss_pct", date(2026, 11, 17), "0.060000", "0.050000"),  # the week before
            ("daily_loss_pct", date(2026, 11, 24), "0.051000", "0.050000"),
            ("manual_pause", FRI, None, None),
        ):
            s.add(
                m.KillSwitchEvent(
                    run_id=run,
                    switch=switch,
                    session_date=day,
                    tripped_at=datetime.combine(day, time(15, 0), tzinfo=UTC),
                    value=Decimal(value) if value else None,
                    threshold=Decimal(threshold) if threshold else None,
                )
            )
        s.commit()
    return run


@pytest.mark.db
def test_build_facts_on_a_seeded_week(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = seed_week(db_factory)
    fake = install_fake_metrics(
        monkeypatch,
        {(MON, FRI): {"trades": 4, "total_pnl": Decimal("5.0000")}, (None, FRI): {"trades": 5, "wins": 3}},
    )
    facts = build_facts(
        db_factory, CAL, run, the_week(), settings=RuntimeSettings(killswitch_expectancy_min_trades=50)
    )

    assert (run, MON, FRI) in fake.calls and (run, None, FRI) in fake.calls
    assert facts["week"] == {"start": "2026-11-23", "end": "2026-11-27", "sessions": 4}
    week_m, rtd = facts["week_metrics"], facts["run_to_date"]
    assert week_m == {
        "trades": 4,
        "wins": 2,
        "losses": 2,
        "win_rate": "0.5000",
        "expectancy_r": "0.1250",
        "avg_win_r": "1.2500",
        "avg_loss_r": "-1.0000",
        "profit_factor": "1.2500",
        "total_pnl": "5.0000",
        "total_fees": "0.0000",
        "avg_slippage": "0.0200",
        "max_drawdown_pct": "0.0310",
        "adherence_pct": "0.7500",
    }
    assert rtd["trades"] == 5 and rtd["wins"] == 3 and set(rtd) == set(week_m)
    assert facts["days"] == [
        {"date": "2026-11-23", "trades": 2, "pnl": "8.0000", "rules_followed": True},
        {"date": "2026-11-24", "trades": 1, "pnl": "-5.0000", "rules_followed": False},
        {"date": "2026-11-25", "trades": 1, "pnl": "2.0000", "rules_followed": None},
        {"date": "2026-11-27", "trades": 0, "pnl": "0.0000", "rules_followed": None},
    ]
    assert facts["best_trade"] == {"ticker": "AAA", "date": "2026-11-23", "pnl": "10.0000", "pnl_r": "1.0000"}
    assert facts["worst_trade"] == {
        "ticker": "BBB",
        "date": "2026-11-24",
        "pnl": "-5.0000",
        "pnl_r": "-0.5000",
    }
    assert facts["kill_switch_trips"] == [
        {"switch": "daily_loss_pct", "date": "2026-11-24", "value": "0.051000", "threshold": "0.050000"},
        {"switch": "manual_pause", "date": "2026-11-27", "value": None, "threshold": None},
    ]
    # closed trades with an R multiple up to the week's end (the switch was never reset)
    assert facts["expectancy_switch"] == {"closed_trades": 4, "min_trades": 50}


@pytest.mark.db
def test_build_facts_without_trades(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with db_factory() as s:
        run = add_run(s)
        s.commit()
    install_fake_metrics(monkeypatch)
    facts = build_facts(db_factory, CAL, run, the_week(), settings=RuntimeSettings())
    assert facts["best_trade"] is None and facts["worst_trade"] is None
    assert facts["kill_switch_trips"] == []
    assert [d["trades"] for d in facts["days"]] == [0, 0, 0, 0]


# --- 3. the number check ------------------------------------------------------------------------------------
FACTS: dict[str, Any] = {
    "week": {"start": "2026-11-23", "end": "2026-11-27", "sessions": 4},
    "week_metrics": {
        "trades": 4,
        "wins": 2,
        "win_rate": "0.5000",
        "expectancy_r": "0.1250",
        "total_pnl": "5.0000",
        "max_drawdown_pct": "0.0310",
    },
    "run_to_date": {"total_pnl": "-1234.5000", "adherence_pct": "0.8000"},
    "best_trade": None,
}


def test_check_numbers_passes_quoted_facts() -> None:
    text = (
        "The week ending November 27 had 4 trades with a 50% win rate and an expectancy of +0.13R. "
        "It made $5.00 over 4 sessions. The run is down -$1,234.50 overall, with 80% adherence and "
        "a 3.1% drawdown, in 2026."
    )
    assert check_numbers(text, FACTS) == []


def test_check_numbers_flags_numbers_not_in_the_facts() -> None:
    text = "There were 7 trades, a 51% win rate and a profit of $12.34, or 0.125R."
    assert check_numbers(text, FACTS) == ["7", "51", "12.34"]


def test_check_numbers_rounding_is_half_up_and_limited_to_four_decimals() -> None:
    facts = {"x": "0.1250", "y": "2.5"}
    assert check_numbers("0.13 and 3 and 0.1250 and 0.125", facts) == []
    assert check_numbers("0.12 and 2", facts) == ["0.12", "2"]
    assert check_numbers("0.12500", facts) == []  # more than 4 decimals: equal values only
    assert check_numbers("0.12501", facts) == ["0.12501"]


def test_check_numbers_takes_pct_kill_switch_trips_as_ratios() -> None:
    """Fix round 1 (P5-GN breaker test_05): a `*_pct` switch's trip value and limit are ratios; the
    expectancy switch's are R multiples and are not multiplied by 100."""
    facts = {
        "kill_switch_trips": [
            {"switch": "daily_loss_pct", "date": "2026-11-09", "value": "0.061200", "threshold": "0.050000"},
            {"switch": "max_drawdown_pct", "date": "2026-11-10", "value": "0.1520", "threshold": "0.1500"},
            {"switch": "expectancy", "date": "2026-11-10", "value": "-0.1200", "threshold": "0.0000"},
            {"switch": "manual_pause", "date": "2026-11-10", "value": None, "threshold": None},
        ]
    }
    ok = "A 6.12% daily loss passed its 5% limit, and a 15.2% drawdown its 15% limit, at -0.12R."
    assert check_numbers(ok, facts) == []
    assert check_numbers("the expectancy switch tripped at 12%", facts) == ["12"]
    assert check_numbers("a 6.13% loss", facts) == ["6.13"]


# --- claude_spent and upsert_report -------------------------------------------------------------------------
def outcome(**kw: Any) -> CommentaryOutcome:
    base: dict[str, Any] = {
        "status": "ok",
        "text": "fine",
        "error": None,
        "model": "claude-sonnet-5",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": Decimal("0.004000"),
    }
    base.update(kw)
    return CommentaryOutcome(**base)


def add_catalyst_cost(factory: sessionmaker[Session], day: date, cost: str, ticker: str = "CAT") -> None:
    with factory() as s:
        sym = add_symbol(s, ticker)
        s.add(
            m.Catalyst(
                symbol_id=sym,
                session_date=day,
                headlines=[],
                catalyst_type="unknown",
                direction="neutral",
                cost_usd=Decimal(cost),
                input_tokens=0,
                output_tokens=0,
                created_at=SAT_0900,
            )
        )
        s.commit()


@pytest.mark.db
def test_claude_spent_counts_catalysts_and_weekly_reports_of_the_et_date(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        run = add_run(s)
        s.commit()
    add_catalyst_cost(db_factory, SAT, "0.100000")
    add_catalyst_cost(db_factory, FRI, "0.500000", "OLD")
    assert claude_spent(db_factory, SAT) == Decimal("0.1")
    # 23:30 ET Saturday is Sunday in UTC: still Saturday's spend
    late = FixedClock(datetime.combine(SAT, time(23, 30), tzinfo=ET).astimezone(UTC))
    upsert_report(db_factory, late, the_week(), run, FACTS, outcome(cost_usd=Decimal("0.020000")))
    assert claude_spent(db_factory, SAT) == Decimal("0.12")
    assert claude_spent(db_factory, date(2026, 11, 29)) == Decimal(0)


@pytest.mark.db
def test_upsert_report_replaces_keeps_created_at_and_accumulates_cost(
    db_factory: sessionmaker[Session],
) -> None:
    with db_factory() as s:
        run = add_run(s)
        s.commit()
    first, second = FixedClock(SAT_0900), FixedClock(SAT_0900 + timedelta(hours=2))
    upsert_report(db_factory, first, the_week(), run, FACTS, outcome())
    upsert_report(
        db_factory,
        second,
        the_week(),
        run,
        {"week": {"sessions": 4}},
        outcome(
            status="rejected", text=None, error="numbers not in the facts: 7", cost_usd=Decimal("0.006000")
        ),
    )
    with db_factory() as s:
        rows = s.execute(select(m.WeeklyReport)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.week_ending == FRI and row.week_start == MON and row.run_id == run
    assert row.facts == {"week": {"sessions": 4}}
    assert row.commentary is None and row.commentary_status == "rejected"
    assert row.commentary_error == "numbers not in the facts: 7"
    assert row.cost_usd == Decimal("0.010000")
    assert row.created_at == SAT_0900 and row.updated_at == SAT_0900 + timedelta(hours=2)


def test_upsert_report_refuses_a_week_without_sessions() -> None:
    week = WeekWindow(MON, FRI, (), None)
    none: Callable[[], Any] = lambda: None  # noqa: E731 - never reached
    with pytest.raises(ValueError):
        upsert_report(none, FixedClock(SAT_0900), week, 1, {}, outcome())  # type: ignore[arg-type]
