"""P6-T2 gauntlet (Breaker, attempt 1): the soak report's read-only guarantee under hostile conditions, the
D1 clean-day verdict at its edges (retry and deadline boundaries, a forced 9:35, early closes, holidays, DST,
verdicts that must never flip once final), the consecutive count with marks and the Saturday line, the
Telegram line (escaping, one line, dedupe, silent vs sound, MT) and the two crontab lines with the replay
quiet windows they added."""

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.engine.runs as runs_mod
import trader.runtime as rt
import trader.strategies.registry as registry_mod
from tests.integration.test_soak_report import counts, deps, make_world, seed_event, seed_rows
from tests.jobs.test_soak import (
    CAL,
    LATER,
    S,
    check,
    clean_rows,
    et,
    evaluate,
    plan,
    renderer,
    replace_job,
    row,
)
from trader.cli import app
from trader.db import models as m
from trader.db.session import session_scope
from trader.jobs import soak
from trader.jobs.soak import build_report, default_through, expected_jobs, line_view, session_window
from trader.market.clock import ET, FixedClock
from trader.replay.data import QUIET_TIMES, quiet_until
from trader.strategies.base import ScheduledEvent, SessionOffset
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

runner = CliRunner()
MON, TUE, WED = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)
ROOT = Path(__file__).resolve().parents[4]


class BrokenSip(OrbSip):
    """A plug-in with a config row that fails to start (its constructor raises)."""

    key = "broken_sip"

    def __init__(self, params: Any) -> None:
        raise RuntimeError("boom access_token=SECRETSECRET")


class NoConfigSip(OrbSip):
    """A plug-in installed after the defaults were ensured: no strategy_configs row yet."""

    key = "noconfig_sip"


class OddKeySip(OrbSip):
    """A plug-in whose plan has an event key with HTML-special characters."""

    key = "odd_key_sip"

    def schedule(self, cal: Any) -> list[ScheduledEvent]:
        return [*super().schedule(cal), ScheduledEvent("x<y&z>", SessionOffset.parse("open+10m"))]


def all_counts(factory: sessionmaker[Session]) -> dict[str, Any]:
    out: dict[str, Any] = counts(factory)
    with factory() as s:
        out["api_credentials"] = s.execute(select(func.count()).select_from(m.ApiCredential)).scalar_one()
        out["event_log_max_id"] = s.execute(select(func.max(m.EventLog.id))).scalar_one()
        out["settings_rows"] = sorted(
            (k, json.dumps(v, sort_keys=True)) for k, v in s.execute(select(m.Setting.key, m.Setting.value))
        )
    return out


def json_line(output: str) -> Any:
    [line] = [x for x in output.splitlines() if x.startswith("{")]
    return json.loads(line)


# --- 1. strictly read-only, whatever the database looks like ------------------------------------------------


@pytest.mark.db
def test_soak_report_is_read_only_with_a_broken_plugin_no_config_no_live_run_and_a_corrupt_settings_row(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    StrategyRegistry(
        w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay, "broken_sip": BrokenSip}
    ).ensure_defaults()
    plugins = {
        "orb_sip": OrbSip,
        "spy_overlay": SpyOverlay,
        "broken_sip": BrokenSip,
        "noconfig_sip": NoConfigSip,
    }
    monkeypatch.setattr(registry_mod, "load_all", lambda: plugins)
    with session_scope(w.factory) as s:
        s.add(m.Setting(key="scheduler.late_grace_seconds", value="not a number", updated_by="test"))

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("the soak report must not build plans or live runs through the runtime")

    monkeypatch.setattr(rt, "plan_builder", forbidden)
    monkeypatch.setattr(rt, "get_live_run", forbidden)
    monkeypatch.setattr(runs_mod, "get_live_run", forbidden)
    monkeypatch.setattr(StrategyRegistry, "ensure_defaults", forbidden)
    seed_rows(w.factory, MON, clean_rows(MON))
    seed_rows(w.factory, TUE, clean_rows(TUE))
    w.clock.set(et(TUE, 18, 5))
    before = all_counts(w.factory)
    assert before["runs"] == 0
    outputs = []
    for args in (
        ["soak-report"],
        ["soak-report", "--json"],
        ["soak-report", "--final", "--json"],
        ["soak-report", "--notify"],
        ["soak-report", "--notify"],
        ["soak-report", "--notify", "--final", "--json"],
        ["soak-report", "--notify", "--final", "--json", "--through", "2026-09-29"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, (args, result.output)
        outputs.append(result.output)
    after = all_counts(w.factory)
    notifications = after.pop("notifications") - before.pop("notifications")
    assert (
        after == before
    )  # no run, no event (so nothing relayed), no config, no settings rewrite, no job_runs
    assert notifications == 2
    with w.factory() as s:
        keys = sorted(s.execute(select(m.Notification.dedupe_key)).scalars())
    assert keys == ["soak:2026-09-29", "soak:2026-09-29:final"]
    assert len(w.api.calls_of("send_message")) == 2
    data = json_line(outputs[1])
    assert data["days"][-1]["verdict"] == "clean"  # the broken and unconfigured plug-ins are left out quietly
    assert "SECRETSECRET" not in "".join(outputs)


# --- 2. the D1 verdict at its edges -------------------------------------------------------------------------


def test_deadline_boundaries_jobs_by_finish_events_by_start_and_a_forced_scan_is_never_on_time() -> None:
    base = clean_rows(TUE)
    retry = replace_job(
        base,
        "preopen",
        row("preopen", et(TUE, 9, 20), et(TUE, 9, 21), "failed", "TokenExpired"),
        row("preopen", et(TUE, 9, 27), et(TUE, 9, 30)),  # the retry finishes exactly at the deadline
    )
    day = evaluate(TUE, retry, LATER)
    assert (check(day, "preopen").status, check(day, "preopen").attempts, day.verdict) == (
        "succeeded",
        2,
        "clean",
    )
    slow_job = replace_job(base, "preopen", row("preopen", et(TUE, 9, 29, 50), et(TUE, 9, 30, 0.5)))
    assert check(evaluate(TUE, slow_job, LATER), "preopen").status == "late"  # a job counts when it finished
    on_edge = replace_job(base, "event:orb_open", row("event:orb_open", et(TUE, 9, 37, 5), et(TUE, 9, 38)))
    assert check(evaluate(TUE, on_edge, LATER), "event:orb_open").status == "succeeded"
    past_edge = replace_job(
        base, "event:orb_open", row("event:orb_open", et(TUE, 9, 37, 5.5), et(TUE, 9, 37, 40))
    )
    assert check(evaluate(TUE, past_edge, LATER), "event:orb_open").status == "late"
    # no 9:35 row at all, then `trader event orb_open --force` at 09:50 that succeeds: never on time
    forced = replace_job(base, "event:orb_open", row("event:orb_open", et(TUE, 9, 50), et(TUE, 9, 50, 30)))
    day = evaluate(TUE, forced, LATER)
    assert day.orb_open == "late" and day.verdict == "not_clean" and day.orb_open_seconds is None


def test_a_final_token_verdict_never_flips_when_a_later_forced_rerun_succeeds() -> None:
    """D1: a past day's verdict must never change on re-evaluation. A token failure at 17:00 ET after the
    post-close is not recovered by D's close; a `postclose --date D --force` run a week later (keyed to D)
    must not turn the day clean after the fact."""
    fail = (et(TUE, 17, 0),)
    rows = clean_rows(TUE)
    first = evaluate(TUE, rows, LATER, token_failures=fail)
    assert check(first, "token-refresh").status == "failed" and first.verdict == "not_clean"
    week_later = TUE + timedelta(days=7)
    rerun = [*rows, row("postclose", et(week_later, 10, 0), et(week_later, 10, 2))]
    again = evaluate(TUE, rerun, et(week_later, 12, 0), token_failures=fail)
    assert check(again, "token-refresh").status == "failed"
    assert again.verdict == first.verdict


def test_early_close_days_holidays_and_the_week_after_christmas() -> None:
    xmas_eve, black_friday = date(2026, 12, 24), date(2026, 11, 27)
    assert CAL.session_close(xmas_eve) == datetime(2026, 12, 24, 18, 0, tzinfo=UTC)  # 13:00 ET
    for d, saturday in ((xmas_eve, date(2026, 12, 26)), (black_friday, date(2026, 11, 28))):
        exp = {e.job: e.deadline for e in expected_jobs(CAL, d, plan(d), S, set())}
        for key in ("event:flatten", "event:entry_cancel", "event:overlay_decision"):
            assert exp[key] == CAL.session_close(d), (d, key)
        assert exp["event:orb_open"] == et(d, 9, 37, 5)
        assert exp["weekly"] == et(saturday, 12, 0), d  # Christmas Eve is its week's last session
        assert exp["postclose"] == et(d, 18, 0)
    assert expected_jobs(CAL, date(2026, 12, 25), plan(date(2026, 12, 25)), S, set()) == ()
    assert session_window(CAL, date(2026, 12, 28), 4) == (
        date(2026, 12, 22),
        date(2026, 12, 23),
        xmas_eve,
        date(2026, 12, 28),
    )
    assert default_through(CAL, et(date(2026, 12, 25), 18, 5)) == xmas_eve
    # a day the container was down (no rows) is never clean, early close or not
    assert evaluate(xmas_eve, [], et(date(2026, 12, 28), 12, 0)).verdict == "not_clean"


def test_event_grace_and_deadlines_are_et_wall_clock_across_both_dst_changes() -> None:
    fri, mon = date(2026, 10, 30), date(2026, 11, 2)  # EDT, then EST
    same_utc = time(14, 36, 30)
    for d, expected in ((mon, "succeeded"), (fri, "late")):  # 09:36:30 EST vs 10:36:30 EDT
        start = datetime.combine(d, same_utc, tzinfo=UTC)
        rows = replace_job(
            clean_rows(d), "event:orb_open", row("event:orb_open", start, start + timedelta(seconds=30))
        )
        assert check(evaluate(d, rows, et(d, 20, 0)), "event:orb_open").status == expected, d
    # the Monday after the fall-back: its nightly ran on Sunday 11-01 20:00 EST and counts for Monday
    rows = replace_job(clean_rows(mon), "nightly", row("nightly", et(date(2026, 11, 1), 20, 0)))
    assert check(evaluate(mon, rows, et(mon, 20, 0)), "nightly").status == "succeeded"
    assert check(evaluate(mon, rows, et(mon, 20, 0)), "nightly").deadline == datetime(
        2026, 11, 2, 13, 0, tzinfo=UTC
    )
    spring_fri, spring_mon = date(2027, 3, 12), date(2027, 3, 15)
    pre = {e.job: e.deadline for e in expected_jobs(CAL, spring_fri, plan(spring_fri), S, set())}
    post = {e.job: e.deadline for e in expected_jobs(CAL, spring_mon, plan(spring_mon), S, set())}
    assert pre["preopen"] == datetime(2027, 3, 12, 14, 30, tzinfo=UTC)
    assert post["preopen"] == datetime(2027, 3, 15, 13, 30, tzinfo=UTC)
    assert post["event:orb_open"] == datetime(2027, 3, 15, 13, 37, 5, tzinfo=UTC)


@pytest.mark.db
def test_past_verdicts_are_identical_on_every_later_run(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    StrategyRegistry(
        w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    ).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    mon = replace_job(
        clean_rows(MON),
        "preopen",
        row("preopen", et(MON, 9, 20), et(MON, 9, 21), "failed", "TokenExpired"),
        row("preopen", et(MON, 9, 23), et(MON, 9, 24)),
    )
    seed_rows(w.factory, MON, mon)
    tue = [r for r in clean_rows(TUE) if r.job != "checkin@13:30"]  # Tuesday is not clean
    seed_rows(w.factory, TUE, tue)
    seed_event(w.factory, et(TUE, 2, 0), "error", rt.TOKEN_SOURCE, "refresh failed")  # recovered at 08:00
    w.clock.set(et(TUE, 18, 5))
    first = {d.session_date: d for d in soak.load_report(deps(w)).days}
    assert first[MON].verdict == "clean" and first[TUE].verdict == "not_clean"
    # later: Wednesday's rows, a forced Monday premarket re-run that fails, a late Tuesday catch-up
    seed_rows(w.factory, WED, clean_rows(WED))
    seed_rows(
        w.factory,
        MON,
        [
            row("premarket", et(WED, 10, 0), et(WED, 10, 1), "failed", "forced"),
            row("event:orb_open", et(WED, 10, 5), et(WED, 10, 5, 30), "failed", "missed: too late"),
        ],
    )
    seed_rows(w.factory, TUE, [row("checkin@13:30", et(WED, 9, 0), et(WED, 9, 0, 10))])
    seed_event(w.factory, et(WED, 3, 0), "error", rt.TOKEN_SOURCE, "refresh failed")  # Wednesday's window
    for now in (et(WED, 18, 5), et(date(2026, 10, 3), 10, 30), et(date(2026, 10, 12), 18, 5)):
        w.clock.set(now)
        again = {d.session_date: d for d in soak.load_report(deps(w)).days}
        for d in (MON, TUE):
            assert (again[d].verdict, again[d].orb_open) == (first[d].verdict, first[d].orb_open), (now, d)
        statuses = [(c.job, c.status) for c in again[MON].checks]
        assert statuses == [(c.job, c.status) for c in first[MON].checks], now  # check for check


# --- 3. the count, marks and the Saturday line --------------------------------------------------------------


@pytest.mark.db
def test_count_with_a_not_clean_midweek_day_marks_and_the_saturday_final_line(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    StrategyRegistry(
        w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    ).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    week = [date(2026, 10, 5) + timedelta(days=i) for i in range(5)]
    sat = date(2026, 10, 10)
    for d in week:
        rows = clean_rows(d)
        if d == week[2]:
            rows = [r for r in rows if r.job != "preopen"]
        seed_rows(w.factory, d, rows)
    # Friday 18:05: Friday is pending (weekly due Sat) and not counted; the count is Thursday's 1
    w.clock.set(et(week[4], 18, 5))
    friday = soak.load_report(deps(w))
    assert friday.days[-1].verdict == "pending"
    assert (friday.consecutive_clean, friday.last_final) == (1, week[3])
    # Saturday 10:30 --final: Friday settles; no orchestrator session ran in between
    w.clock.set(et(sat, 10, 30))
    result = runner.invoke(app, ["soak-report", "--notify", "--final", "--json"])
    assert result.exit_code == 0, result.output
    data = json_line(result.output)
    assert (data["through"], data["consecutive_clean"], data["last_final"]) == ("2026-10-09", 2, "2026-10-09")
    assert data["earliest_finish"] == "2026-10-21"  # 8 sessions after Fri 9 Oct (Columbus Day is a session)
    [sent] = w.api.calls_of("send_message")
    assert sent["text"].startswith("<b>Soak Fri 9 Oct</b> (final): clean ✅")
    assert "08:30 MT" in sent["text"] and sent["silent"] is True
    # a reset on Friday restarts the count there; an outage on Thursday then clearing it
    soak.record_mark(w.factory, FixedClock(et(sat, 10, 31)), None, "reset", week[4], "abc: rule 3")
    w.clock.set(et(sat, 10, 32))
    assert soak.load_report(deps(w)).consecutive_clean == 1
    soak.record_mark(w.factory, FixedClock(et(sat, 10, 33)), None, "outage", week[3], "down 20 min")
    w.clock.set(et(sat, 10, 34))
    report = soak.load_report(deps(w))
    assert report.consecutive_clean == 1 and report.days[-2].verdict == "not_clean"
    soak.record_mark(w.factory, FixedClock(et(sat, 10, 35)), None, "clear", week[3], "mistake")
    w.clock.set(et(sat, 10, 36))
    assert soak.load_report(deps(w)).consecutive_clean == 1  # Friday's reset still stands


# --- 4. the Telegram line -----------------------------------------------------------------------------------


@pytest.mark.db
def test_the_line_escapes_event_keys_and_stays_one_short_line(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    plugins = {"orb_sip": OrbSip, "spy_overlay": SpyOverlay, "odd_key_sip": OddKeySip}
    StrategyRegistry(w.factory, w.clock, plugins=plugins).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: plugins)
    secret = "Bearer abcd1234efgh5678 " + "<b>" * 200
    rows = replace_job(
        clean_rows(TUE), "postclose", row("postclose", et(TUE, 16, 15), None, "failed", secret)
    )
    seed_rows(w.factory, TUE, rows)
    w.clock.set(et(TUE, 18, 5))
    result = runner.invoke(app, ["soak-report", "--notify"])
    assert result.exit_code == 0, result.output
    [sent] = w.api.calls_of("send_message")
    text = sent["text"]
    assert "event:x&lt;y&amp;z&gt; missing" in text
    assert "x<y" not in text and "abcd1234efgh5678" not in text
    assert re.sub(r"</?b>", "", text).count("<") == 0  # the only tags are the renderer's own bold head
    assert "\n" not in text and len(text) < 600
    assert sent["silent"] is False


@pytest.mark.db
def test_one_line_per_session_mt_after_the_dst_change_and_nothing_on_holidays(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    StrategyRegistry(
        w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    ).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    mon = date(2026, 11, 2)
    seed_rows(w.factory, mon, clean_rows(mon))
    w.clock.set(et(mon, 18, 5))
    for args in (["soak-report", "--notify"], ["soak-report", "--notify"]):
        assert runner.invoke(app, args).exit_code == 0
    w.clock.set(et(mon, 19, 0))
    assert runner.invoke(app, ["soak-report", "--notify", "--through", "2026-11-02"]).exit_code == 0
    [sent] = w.api.calls_of("send_message")
    assert sent["text"].startswith("<b>Soak Mon 2 Nov</b>: clean ✅")
    # 18:05 EST in Mountain Time as the installed tzdata has it (Alberta's rules decide, not a fixed offset)
    local = et(mon, 18, 5).astimezone(ZoneInfo("America/Edmonton"))
    assert f"{local:%H:%M} MT" in sent["text"]
    for holiday in (date(2026, 11, 26), date(2026, 12, 25)):
        w.clock.set(et(holiday, 18, 5))
        result = runner.invoke(app, ["soak-report", "--notify"])
        assert result.exit_code == 0 and "not a trading session, nothing to send" in result.output
    assert len(w.api.calls_of("send_message")) == 1


@pytest.mark.db
def test_orb_sip_disabled_the_scan_is_off_and_the_day_can_be_clean(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch)
    reg = StrategyRegistry(w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    reg.ensure_defaults()
    reg.update("orb_sip", enabled=False, actor="test")
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    seed_rows(w.factory, TUE, [r for r in clean_rows(TUE) if r.job != "event:orb_open"])
    w.clock.set(et(TUE, 18, 5))
    before = all_counts(w.factory)
    result = runner.invoke(app, ["soak-report", "--json", "--sessions", "1", "--notify"])
    assert result.exit_code == 0, result.output
    day = json_line(result.output)["days"][0]
    assert (day["verdict"], day["orb_open"], day["orb_open_seconds"]) == ("clean", "off", None)
    assert "event:orb_open" not in {c["job"] for c in day["checks"]}
    [sent] = w.api.calls_of("send_message")
    assert "9:35 scan off" in sent["text"] and sent["silent"] is True
    after = all_counts(w.factory)
    assert after.pop("notifications") == before.pop("notifications") + 1
    assert after == before


def test_the_line_never_says_scan_off_while_orb_sip_is_enabled_and_the_scan_is_still_pending() -> None:
    """A manual `--notify` at 09:00 ET: the nightly is missing (not clean), the 9:35 scan is still pending.
    orb_sip is enabled, so "9:35 scan off" (the disabled-strategy wording) would be false."""
    now = et(TUE, 9, 0)
    day = evaluate(TUE, [], now)
    assert day.verdict == "not_clean" and day.orb_open == "pending"
    report = build_report([day], cal=CAL, through=TUE, target=10, now=now, env="dev")
    msg = renderer(now).soak_line(line_view(report, final=False))
    assert "9:35 scan off" not in msg.text
    assert msg.silent is False


def test_the_saturday_final_line_settles_a_friday_whose_weekly_failed_for_good() -> None:
    """Plan T2: "the Saturday `--final` line settles it". The weekly job runs at 09:00 ET with its in-process
    retries (done well before 10:00); when its last attempt failed, the 10:30 `--final` line must not
    still call Friday pending (silent), or the one line meant to settle the week hides a not-clean day."""
    fri, sat = date(2026, 10, 2), date(2026, 10, 3)
    rows = replace_job(
        clean_rows(fri),
        "weekly",
        row("weekly", et(sat, 9, 0), et(sat, 9, 1), "failed", "boom"),
        row("weekly", et(sat, 9, 2), et(sat, 9, 3), "failed", "boom"),
        row("weekly", et(sat, 9, 5), et(sat, 9, 6), "failed", "boom"),
    )
    now = et(sat, 10, 30)
    day = evaluate(fri, rows, now)
    report = build_report([day], cal=CAL, through=fri, target=10, now=now, env="dev")
    msg = renderer(now).soak_line(line_view(report, final=True))
    assert day.verdict != "pending", check(day, "weekly")
    assert "pending" not in msg.text and msg.silent is False


# --- 5. the crontab lines and the replay quiet windows ------------------------------------------------------


def test_the_two_cron_lines_their_et_times_and_the_replay_quiet_windows() -> None:
    lines = (ROOT / "Trader" / "docker" / "crontab").read_text().splitlines()
    tz_at = lines.index("CRON_TZ=America/New_York")
    jobs = [re.split(r"\s+", x.strip(), maxsplit=5) for x in lines[tz_at + 1 :] if x.strip() and x[0] != "#"]
    soak_lines = [" ".join(j) for j in jobs if "soak-report" in " ".join(j)]
    assert soak_lines == [
        "5 18 * * 1-5 trader soak-report --notify",
        "30 10 * * 6 trader soak-report --notify --final",
    ]
    # after the post-close (16:15) and the Saturday weekly (09:00) lines
    order = [" ".join(j[5:]) for j in jobs]
    assert order.index("trader soak-report --notify") > order.index("trader postclose")
    assert order.index("trader soak-report --notify --final") > order.index("trader weekly")
    assert (time(18, 5), frozenset({0, 1, 2, 3, 4})) in QUIET_TIMES
    assert (time(10, 30), frozenset({5})) in QUIET_TIMES
    # the added windows only hold back replay fetches around the two lines, weekdays and Saturday
    assert quiet_until(et(TUE, 18, 0)) == datetime.combine(TUE, time(18, 15), tzinfo=ET)
    assert quiet_until(et(TUE, 18, 16)) is None
    assert quiet_until(et(date(2026, 10, 4), 18, 5)) is None  # Sunday
    assert quiet_until(et(date(2026, 10, 3), 10, 25)) == datetime.combine(
        date(2026, 10, 3), time(10, 40), tzinfo=ET
    )
    spec = (ROOT / "Trader" / "docs" / "SPEC.md").read_text()
    assert re.search(r"\| 18:05 Mon–Fri \| 16:05 \|.*`trader soak-report --notify` \|", spec)
    assert re.search(r"\| Sat 10:30 \| 08:30 \|.*`trader soak-report --notify --final` \|", spec)


def test_a_redacted_or_long_line_and_error_text_is_never_cut_inside_an_entity() -> None:
    """The line cuts each error at 60 characters BEFORE escaping, so an ampersand run near the cut can't leave
    a broken entity; the JSON/table error is cut to 200 after masking."""
    err = ("a" * 58) + "&&&&" + ("b" * 300)
    rows = replace_job(clean_rows(TUE), "preopen", row("preopen", et(TUE, 9, 20), None, "failed", err))
    day = evaluate(TUE, rows, LATER)
    assert len(check(day, "preopen").error or "") == 200
    report = build_report([day], cal=CAL, through=TUE, target=10, now=LATER, env="dev")
    text = renderer(LATER).soak_line(line_view(report, final=False)).text
    for amp in re.finditer("&", text):
        assert re.match(r"&(amp|lt|gt|quot|#\d+);", text[amp.start() :]), text[amp.start() : amp.start() + 8]
