"""P6-T2: the soak report's pure parts (trader.jobs.soak): the clean-day rule D1 (acceptance tests 1-10), the
count and finish date (12), the Telegram line (13, rendering) and masking (14). No database: rows are
`JobRunRow` values, plans come from `day_plan` over the default strategies."""

import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.engine.scheduler import DayPlan, day_plan
from trader.jobs import soak
from trader.jobs.soak import (
    ExpectedJob,
    JobRunRow,
    SoakDay,
    SoakMark,
    build_report,
    effective_marks,
    evaluate_day,
    expected_jobs,
    line_view,
    session_window,
)
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.messages import MessageRenderer
from trader.notify.types import SoakLineView
from trader.settings_store import RuntimeSettings
from trader.strategies.orb_sip import OrbSip, OrbSipParams
from trader.strategies.spy_overlay import SpyOverlay, SpyOverlayParams

CAL = SessionCalendar()
S = RuntimeSettings()
MON, TUE, WED, THU, FRI = (date(2026, 9, 28) + timedelta(days=i) for i in range(5))
MT = ZoneInfo("America/Edmonton")


def et(d: date, hh: int, mm: int, ss: float = 0) -> datetime:
    whole = int(ss)
    micro = round((ss - whole) * 1_000_000)
    return datetime.combine(d, time(hh, mm, whole, micro), tzinfo=ET).astimezone(UTC)


def plan(d: date, *, orb: bool = True) -> DayPlan:
    strategies = [SpyOverlay(SpyOverlayParams())]
    if orb:
        strategies.insert(0, OrbSip(OrbSipParams()))  # type: ignore[arg-type]
    return day_plan(strategies, CAL, d, S)  # type: ignore[arg-type]


def row(
    job: str,
    start: datetime,
    end: datetime | None = None,
    status: str = "succeeded",
    error: str | None = None,
) -> JobRunRow:
    if end is None and status != "running":
        end = start + timedelta(seconds=30)
    return JobRunRow(job, status, start, end, error)


def clean_rows(d: date) -> list[JobRunRow]:
    """Every expected job of a normal session, succeeded once and on time."""
    prev = CAL.previous_session(d)
    rows = [
        row("nightly", et(prev, 20, 0), et(prev, 20, 4)),
        row("premarket", et(d, 8, 0), et(d, 8, 3)),
        row("preopen", et(d, 9, 20), et(d, 9, 20, 40)),
        row("event:orb_open", et(d, 9, 35, 5), et(d, 9, 35, 36.2)),
        row("checkin@11:30", et(d, 11, 30), et(d, 11, 30, 20)),
        row("event:entry_cancel", et(d, 11, 30), et(d, 11, 30, 2)),
        row("checkin@13:30", et(d, 13, 30), et(d, 13, 30, 20)),
        row("event:overlay_decision", et(d, 15, 30), et(d, 15, 30, 3)),
        row("event:flatten", et(d, 15, 50), et(d, 15, 50, 4)),
        row("session_end", et(d, 16, 0, 30), et(d, 16, 0, 40)),
        row("postclose", et(d, 16, 15), et(d, 16, 17)),
    ]
    week_last = max(x for x in (d + timedelta(days=i) for i in range(5 - d.weekday())) if CAL.is_session(x))
    if d == week_last:
        saturday = d + timedelta(days=5 - d.weekday())
        rows.append(row("weekly", et(saturday, 9, 0), et(saturday, 9, 1)))
    return rows


def evaluate(
    d: date,
    rows: list[JobRunRow],
    now: datetime,
    *,
    orb: bool = True,
    orb_required: bool = True,
    token_failures: tuple[datetime, ...] = (),
    outage: str | None = None,
) -> SoakDay:
    expected = expected_jobs(CAL, d, plan(d, orb=orb), S, {r.job for r in rows}, orb_required=orb_required)
    return evaluate_day(d, expected, rows, now, token_failures=token_failures, outage=outage)


def check(day: SoakDay, job: str) -> soak.JobCheck:
    [found] = [c for c in day.checks if c.job == job]
    return found


def replace_job(rows: list[JobRunRow], job: str, *new: JobRunRow) -> list[JobRunRow]:
    return [r for r in rows if r.job != job] + list(new)


LATER = et(WED, 12, 0)  # Tuesday's checks are all past their deadlines


# --- 1. a clean day; events by started_at -------------------------------------------------------------------


def test_every_job_on_time_is_a_clean_day_with_the_scan_duration() -> None:
    day = evaluate(TUE, clean_rows(TUE), LATER)
    assert day.verdict == "clean"
    assert day.failed == ()
    assert day.orb_open == "succeeded"
    assert day.orb_open_seconds == pytest.approx(31.2)
    assert {c.status for c in day.checks} == {"succeeded"}
    jobs = {c.job for c in day.checks}
    assert {"nightly", "premarket", "preopen", "event:orb_open", "event:flatten", "session_end"} <= jobs
    assert {"postclose", "token-refresh", "checkin@11:30", "checkin@13:30"} <= jobs
    assert "weekly" not in jobs  # Tuesday is not the week's last session


def test_a_slow_scan_started_inside_its_grace_is_on_time_and_a_late_start_is_late() -> None:
    slow = replace_job(
        clean_rows(TUE), "event:orb_open", row("event:orb_open", et(TUE, 9, 35, 6), et(TUE, 9, 37, 40))
    )
    day = evaluate(TUE, slow, LATER)
    assert check(day, "event:orb_open").status == "succeeded"
    assert day.verdict == "clean"
    assert day.orb_open_seconds == pytest.approx(154.0)

    late = replace_job(clean_rows(TUE), "event:orb_open", row("event:orb_open", et(TUE, 9, 37, 10)))
    day = evaluate(TUE, late, LATER)
    assert check(day, "event:orb_open").status == "late"
    assert day.orb_open == "late"
    assert day.verdict == "not_clean"
    assert day.failed == ("event:orb_open late",)


# --- 2. retries ---------------------------------------------------------------------------------------------


def test_a_failed_attempt_then_an_on_time_success_is_clean() -> None:
    retried = replace_job(
        clean_rows(TUE),
        "preopen",
        row("preopen", et(TUE, 9, 20), et(TUE, 9, 20, 30), "failed", "TokenExpired: try again"),
        row("preopen", et(TUE, 9, 21), et(TUE, 9, 21, 30)),
    )
    day = evaluate(TUE, retried, LATER)
    c = check(day, "preopen")
    assert (c.status, c.attempts) == ("succeeded", 2)
    assert day.verdict == "clean"


def test_a_retry_that_succeeds_after_the_deadline_is_late() -> None:
    retried = replace_job(
        clean_rows(TUE),
        "preopen",
        row("preopen", et(TUE, 9, 20), et(TUE, 9, 20, 30), "failed", "TokenExpired"),
        row("preopen", et(TUE, 9, 30, 30), et(TUE, 9, 31)),
    )
    day = evaluate(TUE, retried, LATER)
    assert check(day, "preopen").status == "late"
    assert day.verdict == "not_clean"


# --- 3. any on-time success counts --------------------------------------------------------------------------


def test_a_forced_rerun_that_fails_after_an_on_time_success_does_not_spoil_it() -> None:
    rows = clean_rows(TUE) + [row("preopen", et(TUE, 10, 0), et(TUE, 10, 1), "failed", "boom")]
    day = evaluate(TUE, rows, LATER)
    assert check(day, "preopen").status == "succeeded"
    assert check(day, "preopen").attempts == 2
    assert day.verdict == "clean"


# --- 4. the container was down ------------------------------------------------------------------------------


def test_no_rows_after_the_deadlines_is_missing_everywhere() -> None:
    day = evaluate(TUE, [], LATER)
    assert day.verdict == "not_clean"
    jobs = {c.job: c.status for c in day.checks if c.job != "token-refresh"}
    assert set(jobs.values()) == {"missing"}
    assert "event:orb_open" in jobs and "postclose" in jobs
    assert day.orb_open == "missing"
    assert "nightly missing" in day.failed


def test_no_rows_at_nine_is_nightly_missing_and_the_rest_pending() -> None:
    day = evaluate(TUE, [], et(TUE, 9, 0))
    statuses = {c.job: c.status for c in day.checks}
    assert statuses.pop("nightly") == "missing"
    assert set(statuses.values()) == {"pending"}
    assert day.verdict == "not_clean"


# --- 5. the 9:35 scan ---------------------------------------------------------------------------------------


def test_a_missed_scan_later_forced_is_missed() -> None:
    rows = replace_job(
        clean_rows(TUE),
        "event:orb_open",
        row("event:orb_open", et(TUE, 9, 40), et(TUE, 9, 40, 1), "failed", "missed: 295s late"),
        row("event:orb_open", et(TUE, 9, 50), et(TUE, 9, 50, 30)),
    )
    day = evaluate(TUE, rows, LATER)
    assert check(day, "event:orb_open").status == "missed"
    assert day.orb_open == "missed"
    assert day.verdict == "not_clean"


def test_the_scan_is_expected_while_orb_sip_is_enabled_even_if_the_plan_lacks_it() -> None:
    rows = [
        r for r in clean_rows(TUE) if r.job not in ("event:orb_open", "event:entry_cancel", "event:flatten")
    ]
    day = evaluate(TUE, rows, LATER, orb=False, orb_required=True)
    assert check(day, "event:orb_open").status == "missing"
    orb = [e for e in expected_jobs(CAL, TUE, plan(TUE, orb=False), S, set()) if e.job == "event:orb_open"]
    assert orb == [ExpectedJob("event:orb_open", et(TUE, 9, 37, 5))]
    assert day.verdict == "not_clean"


def test_without_orb_sip_the_scan_is_not_expected_and_the_line_says_off() -> None:
    rows = [
        r for r in clean_rows(TUE) if r.job not in ("event:orb_open", "event:entry_cancel", "event:flatten")
    ]
    day = evaluate(TUE, rows, LATER, orb=False, orb_required=False)
    assert "event:orb_open" not in {c.job for c in day.checks}
    assert day.verdict == "clean"
    assert day.orb_open == "off"
    report = build_report([day], cal=CAL, through=TUE, target=10, now=LATER, env="dev")
    msg = renderer().soak_line(line_view(report, final=False))
    assert "9:35 scan off" in msg.text


# --- 6. running and failed before the deadline --------------------------------------------------------------


def test_a_row_still_running_at_the_deadline_is_failed() -> None:
    rows = replace_job(clean_rows(TUE), "preopen", row("preopen", et(TUE, 9, 20), None, "running"))
    day = evaluate(TUE, rows, LATER)
    c = check(day, "preopen")
    assert c.status == "failed"
    assert c.error == "still running at the deadline"
    assert day.verdict == "not_clean"


def test_a_failure_before_the_deadline_is_pending() -> None:
    rows = [r for r in clean_rows(TUE) if r.started_at < et(TUE, 9, 20)]
    rows.append(row("preopen", et(TUE, 9, 20), et(TUE, 9, 20, 30), "failed", "TokenExpired"))
    day = evaluate(TUE, rows, et(TUE, 9, 25))
    assert check(day, "preopen").status == "pending"
    assert check(day, "nightly").status == "succeeded"
    assert day.verdict == "pending"


def test_retries_exhausted_before_the_deadline_is_provisionally_failed_until_a_catch_up() -> None:
    """Fix round 1 (D1): three failed preopen attempts (jobs.retry_attempts) by 09:26 ET: provisionally
    failed, the day not clean but not final and not counted; an on-time catch-up at 09:29 turns it clean.
    Fewer failed rows than the attempts stay pending; a job without in-process retries stays pending."""
    base = [r for r in clean_rows(TUE) if r.started_at < et(TUE, 9, 20)]
    tries = [
        row("preopen", et(TUE, 9, 20), et(TUE, 9, 20, 30), "failed", "TokenExpired"),
        row("preopen", et(TUE, 9, 22, 30), et(TUE, 9, 23), "failed", "TokenExpired"),
        row("preopen", et(TUE, 9, 25), et(TUE, 9, 25, 30), "failed", "TokenExpired"),
    ]
    now = et(TUE, 9, 26)
    day = evaluate(TUE, base + tries, now)
    c = check(day, "preopen")
    assert (c.status, c.provisional, c.attempts) == ("failed", True, 3)
    assert (day.verdict, day.provisional) == ("not_clean", True)
    report = build_report([day], cal=CAL, through=TUE, target=10, now=now, env="dev")
    assert report.last_final is None and report.consecutive_clean == 0
    msg = renderer(now).soak_line(line_view(report, final=False))
    assert "catch-up possible until 07:30 MT" in msg.text and msg.silent is False
    two = evaluate(TUE, base + tries[:2], now)
    assert check(two, "preopen").status == "pending" and two.verdict == "pending"
    caught = evaluate(TUE, [*base, *tries, row("preopen", et(TUE, 9, 29), et(TUE, 9, 29, 40))], LATER)
    assert check(caught, "preopen").status == "succeeded"
    # after the deadline with no catch-up it is final
    final = evaluate(TUE, [r for r in clean_rows(TUE) if r.job != "preopen"] + tries, LATER)
    assert (check(final, "preopen").provisional, final.provisional, final.verdict) == (
        False,
        False,
        "not_clean",
    )
    # a check-in (no in-process retries) that failed before its deadline stays pending
    failed_checkin = [row("checkin@11:30", et(TUE, 11, 30), et(TUE, 11, 30, 5), "failed", "boom")] * 3
    assert check(evaluate(TUE, failed_checkin, et(TUE, 11, 40)), "checkin@11:30").status == "pending"


# --- 7. the token check -------------------------------------------------------------------------------------


def test_token_failure_recovered_by_a_later_questrade_job_succeeds() -> None:
    fail = (et(TUE, 2, 0),)
    day = evaluate(TUE, clean_rows(TUE), LATER, token_failures=fail)
    assert check(day, "token-refresh").status == "succeeded"
    assert day.verdict == "clean"
    # a week later: the same verdict (nothing depends on `now` once final)
    again = evaluate(TUE, clean_rows(TUE), LATER + timedelta(days=7), token_failures=fail)
    assert again == day


def test_token_failure_without_recovery_fails() -> None:
    fail = (et(TUE, 2, 0),)
    rows = clean_rows(TUE)
    for job in ("premarket", "event:orb_open", "postclose"):
        [old] = [r for r in rows if r.job == job]
        rows = replace_job(rows, job, dataclasses.replace(old, status="failed", error="token"))
    day = evaluate(TUE, rows, LATER, token_failures=fail)
    assert check(day, "token-refresh").status == "failed"
    assert day.verdict == "not_clean"
    again = evaluate(TUE, rows, LATER + timedelta(days=7), token_failures=fail)
    assert again == day


def test_token_failure_before_the_questrade_jobs_started_is_not_recovered_by_them() -> None:
    # every token-needing success started before the failure event
    fail = (et(TUE, 16, 30),)
    rows = clean_rows(TUE)
    [old] = [r for r in rows if r.job == "postclose"]
    rows = replace_job(rows, "postclose", dataclasses.replace(old, started_at=et(TUE, 16, 15)))
    day = evaluate(TUE, rows, LATER, token_failures=fail)
    assert check(day, "token-refresh").status == "failed"


def test_token_failure_after_a_succeeded_postclose_fails() -> None:
    day = evaluate(TUE, clean_rows(TUE), LATER, token_failures=(et(TUE, 17, 0),))
    assert check(day, "token-refresh").status == "failed"
    assert "token-refresh failed" in day.failed


def test_a_token_user_started_after_the_deadline_never_recovers_the_day() -> None:
    """Fix round 1: a forced postclose keyed to D a week later started after D 18:00 ET; the final verdict
    must not flip to clean."""
    fail = (et(TUE, 17, 0),)
    rows = clean_rows(TUE) + [row("postclose", et(TUE, 18, 0, 1), et(TUE, 18, 2))]
    day = evaluate(TUE, rows, LATER, token_failures=fail)
    assert check(day, "token-refresh").status == "failed"
    on_edge = clean_rows(TUE) + [row("postclose", et(TUE, 18, 0), et(TUE, 18, 2))]
    assert check(evaluate(TUE, on_edge, LATER, token_failures=fail), "token-refresh").status == "succeeded"


def test_no_token_event_succeeds() -> None:
    day = evaluate(TUE, clean_rows(TUE), LATER)
    assert check(day, "token-refresh").status == "succeeded"
    assert check(day, "token-refresh").deadline == et(TUE, 18, 0)


# --- 8. early close and holidays ----------------------------------------------------------------------------


def test_early_close_deadlines_and_the_thanksgiving_gap() -> None:
    fri = date(2026, 11, 27)
    expected = {e.job: e.deadline for e in expected_jobs(CAL, fri, plan(fri), S, set())}
    close = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    for key in ("event:flatten", "event:entry_cancel", "event:overlay_decision"):
        assert expected[key] == close
    rows = [row("checkin@13:30", et(fri, 13, 30), et(fri, 13, 30, 5))]  # "after close" success
    day = evaluate(fri, rows, et(fri, 20, 0))
    assert check(day, "checkin@13:30").status == "succeeded"
    assert session_window(CAL, fri, 3) == (date(2026, 11, 24), date(2026, 11, 25), fri)
    assert date(2026, 11, 26) not in session_window(CAL, fri, 20)


# --- 9. DST and the nightly key -----------------------------------------------------------------------------


def test_deadlines_follow_the_dst_change() -> None:
    before = {
        e.job: e.deadline for e in expected_jobs(CAL, date(2026, 10, 30), plan(date(2026, 10, 30)), S, set())
    }
    after = {
        e.job: e.deadline for e in expected_jobs(CAL, date(2026, 11, 2), plan(date(2026, 11, 2)), S, set())
    }
    assert before["preopen"] == datetime(2026, 10, 30, 13, 30, tzinfo=UTC)
    assert after["preopen"] == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_the_monday_nightly_run_on_sunday_counts_for_monday() -> None:
    rows = clean_rows(MON)
    [nightly] = [r for r in rows if r.job == "nightly"]
    assert nightly.started_at == et(date(2026, 9, 25), 20, 0)  # built for Monday from the previous session
    rows = replace_job(
        rows, "nightly", row("nightly", et(date(2026, 9, 27), 20, 0), et(date(2026, 9, 27), 20, 5))
    )
    day = evaluate(MON, rows, et(TUE, 12, 0))
    assert check(day, "nightly").status == "succeeded"
    assert check(day, "nightly").deadline == et(MON, 8, 0)


# --- 10. weekly ---------------------------------------------------------------------------------------------


def test_weekly_is_expected_on_the_weeks_last_session_until_saturday_noon() -> None:
    expected = {e.job: e.deadline for e in expected_jobs(CAL, FRI, plan(FRI), S, set())}
    assert expected["weekly"] == et(date(2026, 10, 3), 12, 0)
    rows = [r for r in clean_rows(FRI) if r.job != "weekly"]
    friday_evening = evaluate(FRI, rows, et(FRI, 18, 5))
    assert check(friday_evening, "weekly").status == "pending"
    assert friday_evening.verdict == "pending"
    saturday = evaluate(FRI, clean_rows(FRI), et(date(2026, 10, 3), 10, 30))
    assert saturday.verdict == "clean"


def test_weekly_falls_on_thursday_when_friday_is_a_holiday() -> None:
    thu = date(2027, 3, 25)  # Good Friday 2027-03-26 is closed
    assert not CAL.is_session(date(2027, 3, 26))
    expected = {e.job: e.deadline for e in expected_jobs(CAL, thu, plan(thu), S, set())}
    assert expected["weekly"] == et(date(2027, 3, 27), 12, 0)
    assert "weekly" not in {
        e.job for e in expected_jobs(CAL, date(2027, 3, 24), plan(date(2027, 3, 24)), S, set())
    }


# --- 12. the count and the finish date ----------------------------------------------------------------------


def soak_day(d: date, verdict: str, *, reset: bool = False, outage: str | None = None) -> SoakDay:
    return SoakDay(d, verdict, (), (), "succeeded", 30.0, reset, outage)  # type: ignore[arg-type]


FIVE = session_window(CAL, date(2026, 10, 5), 6)  # Sep 28 - Oct 5


def test_count_stops_at_the_first_pending_day_and_the_finish_is_counted_in_sessions() -> None:
    verdicts = ["clean", "clean", "not_clean", "clean", "clean", "pending"]
    days = [soak_day(d, v) for d, v in zip(FIVE, verdicts, strict=True)]
    report = build_report(days, cal=CAL, through=FIVE[-1], target=10, now=et(FIVE[-1], 18, 5), env="dev")
    assert report.consecutive_clean == 2
    assert report.total_clean == 4
    assert report.last_final == FIVE[4]
    finish = FIVE[4]
    for _ in range(8):
        finish = CAL.next_session(finish)
    assert report.earliest_finish == finish == date(2026, 10, 14)


def test_a_reset_mark_restarts_the_count_on_its_day() -> None:
    verdicts = ["clean", "clean", "clean", "clean", "clean"]
    days = [soak_day(d, v, reset=(i == 3)) for i, (d, v) in enumerate(zip(FIVE, verdicts, strict=False))]
    report = build_report(days, cal=CAL, through=FIVE[4], target=10, now=et(FIVE[4], 18, 5), env="dev")
    assert report.consecutive_clean == 2


def test_target_reached_has_no_finish_date() -> None:
    window = session_window(CAL, date(2026, 10, 9), 10)
    days = [soak_day(d, "clean") for d in window]
    report = build_report(days, cal=CAL, through=window[-1], target=10, now=et(window[-1], 20, 0), env="dev")
    assert report.consecutive_clean == 10
    assert report.earliest_finish is None
    assert report.last_final == date(2026, 10, 9)


def test_an_outage_mark_makes_its_day_not_clean_with_the_reason() -> None:
    day = evaluate(TUE, clean_rows(TUE), LATER, outage="container down 10:02-10:15 ET")
    assert day.verdict == "not_clean"
    assert day.outage == "container down 10:02-10:15 ET"


def test_marks_latest_wins_and_clear_removes() -> None:
    at = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)
    marks = [
        SoakMark(TUE, "outage", "down", at),
        SoakMark(TUE, "clear", "mistake", at + timedelta(minutes=1)),
        SoakMark(WED, "reset", "abc123: rule 3", at),
        SoakMark(THU, "reset", "first", at),
        SoakMark(THU, "outage", "second", at + timedelta(minutes=5)),
    ]
    got = effective_marks(marks)
    assert TUE not in got
    assert got[WED].kind == "reset"
    assert (got[THU].kind, got[THU].reason) == ("outage", "second")


# --- 13. the line (rendering) -------------------------------------------------------------------------------


def renderer(at: datetime | None = None) -> MessageRenderer:
    return MessageRenderer("https://trader.test", MT, clock=FixedClock(at or et(TUE, 18, 5)))


def view(**kw: object) -> SoakLineView:
    base: dict[str, object] = {
        "session_date": MON,
        "verdict": "clean",
        "failed": (),
        "orb_open": "succeeded",
        "orb_open_seconds": 31.2,
        "consecutive_clean": 1,
        "target": 10,
        "earliest_finish": date(2026, 10, 9),
        "changed": (),
        "final": False,
        "env": "dev",
    }
    base.update(kw)
    return SoakLineView(**base)  # type: ignore[arg-type]


def test_a_clean_line_is_silent_with_the_count_and_finish() -> None:
    msg = renderer().soak_line(view())
    assert msg.kind == "soak"
    assert msg.silent is True
    assert msg.dedupe_key == "soak:2026-09-28"
    assert msg.text.startswith("<b>Soak Mon 28 Sep</b>")
    assert "clean ✅" in msg.text
    assert "9:35 scan 31 s" in msg.text
    assert "1 clean day in a row (target 10)" in msg.text
    assert "earliest finish Fri 9 Oct" in msg.text
    assert "16:05 MT" in msg.text  # 18:05 ET shown in Mountain Time


def test_a_not_clean_line_sounds_and_escapes() -> None:
    msg = renderer().soak_line(
        view(
            verdict="not_clean",
            failed=("preopen failed (<Token> & friends)",),
            orb_open_seconds=None,
            consecutive_clean=0,
            earliest_finish=date(2026, 10, 13),
            changed=((MON, "not clean: postclose failed"),),
        )
    )
    assert msg.silent is False
    assert "NOT clean ❌" in msg.text
    assert "preopen failed (&lt;Token&gt; &amp; friends)" in msg.text
    assert "count 0" in msg.text
    assert "Mon 28 Sep is now not clean: postclose failed" in msg.text
    assert "<Token>" not in msg.text


def test_the_final_line_has_its_own_key_and_prod_is_ops_without_target() -> None:
    final = renderer().soak_line(view(final=True))
    assert final.dedupe_key == "soak:2026-09-28:final"
    ops = renderer().soak_line(view(env="prod"))
    assert ops.text.startswith("<b>Ops Mon 28 Sep</b>")
    assert "target" not in ops.text
    assert "earliest finish" not in ops.text


def test_the_scan_is_off_only_when_orb_sip_is_disabled() -> None:
    """Fix round 1: with orb_sip enabled and the 9:35 scan still pending (a manual line at 09:00 ET on a day
    already not clean), the line says "9:35 scan pending", never "off"."""
    now = et(TUE, 9, 0)
    day = evaluate(TUE, [], now)
    report = build_report([day], cal=CAL, through=TUE, target=10, now=now, env="dev")
    lv = line_view(report, final=False)
    assert lv.orb_open == "pending"
    text = renderer(now).soak_line(lv).text
    assert "9:35 scan pending" in text and "9:35 scan off" not in text
    off = renderer().soak_line(view(orb_open="off", orb_open_seconds=None)).text
    assert "9:35 scan off" in off
    missed = (
        renderer()
        .soak_line(
            view(
                verdict="not_clean",
                orb_open="missed",
                orb_open_seconds=None,
                failed=("event:orb_open missed",),
            )
        )
        .text
    )
    assert "9:35 scan off" not in missed and "event:orb_open missed" in missed


def test_a_pending_friday_says_the_weekly_report_is_due() -> None:
    msg = renderer().soak_line(view(session_date=FRI, verdict="pending", failed=("weekly",)))
    assert "weekly report due Sat" in msg.text
    assert msg.silent is True


# --- 14. masking --------------------------------------------------------------------------------------------


def test_errors_are_masked_and_cut_to_200_characters() -> None:
    secret = "Bearer abcd1234efgh5678 at https://api.example/x?access_token=SECRETVALUE123"
    rows = replace_job(clean_rows(TUE), "preopen", row("preopen", et(TUE, 9, 20), None, "failed", secret))
    day = evaluate(TUE, rows, LATER)
    error = check(day, "preopen").error
    assert error is not None
    assert "abcd1234efgh5678" not in error and "SECRETVALUE123" not in error
    assert "[REDACTED]" in error
    long = replace_job(clean_rows(TUE), "preopen", row("preopen", et(TUE, 9, 20), None, "failed", "x" * 5000))
    assert len(check(evaluate(TUE, long, LATER), "preopen").error or "") == 200
