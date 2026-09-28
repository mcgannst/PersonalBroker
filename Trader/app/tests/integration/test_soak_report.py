"""P6-T2 integration (testcontainers database, fake Telegram): `load_report` over seeded `job_runs` and
`event_log` rows (acceptance test 11), the report's read-only guarantee through `trader soak-report` (11),
one Telegram line per session with the real dedupe table (13), masking in the table, the JSON and the message
(14), marks, and the "changed" line."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.runtime as rt
import trader.strategies.registry as registry_mod
from tests.fakes_api import test_core
from tests.fakes_telegram import FakeTelegramApi
from tests.jobs.test_soak import clean_rows
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.jobs import soak
from trader.jobs.soak import JobRunRow
from trader.market.calendar import SessionCalendar
from trader.market.clock import ET, FixedClock
from trader.notify.messages import MessageRenderer
from trader.settings_store import RuntimeSettings
from trader.strategies.base import ScheduledEvent, SessionOffset
from trader.strategies.orb_sip import OrbSip
from trader.strategies.registry import StrategyRegistry
from trader.strategies.spy_overlay import SpyOverlay

pytestmark = pytest.mark.db

CAL = SessionCalendar()
FRI, MON, TUE = date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29)
CHAT = 4242
runner = CliRunner()


def et(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(UTC)


@dataclass
class SoakWorld:
    core: Core
    clock: FixedClock
    api: FakeTelegramApi

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.core.factory


def make_world(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: str = "dev",
    telegram: bool = True,
) -> SoakWorld:
    clock = FixedClock(et(TUE, 20, 0))
    extra: dict[str, Any] = {"app_env": env}
    if telegram:
        extra |= {"telegram_bot_token": SecretStr("123456:TEST-TOKEN-NOT-REAL"), "telegram_chat_id": CHAT}
    core = test_core(db_factory, clock, **extra)
    w = SoakWorld(core, clock, FakeTelegramApi())
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "build_telegram_api", lambda env: w.api)
    return w


@pytest.fixture
def world(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[SoakWorld]:
    w = make_world(db_factory, monkeypatch)
    StrategyRegistry(
        w.factory, w.clock, plugins={"orb_sip": OrbSip, "spy_overlay": SpyOverlay}
    ).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    yield w


def seed_rows(factory: sessionmaker[Session], d: date, rows: list[JobRunRow]) -> None:
    with session_scope(factory) as s:
        for r in rows:
            s.add(
                m.JobRun(
                    job=r.job,
                    session_date=d,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    status=r.status,
                    error=r.error,
                )
            )


def seed_event(factory: sessionmaker[Session], at: datetime, level: str, source: str, message: str) -> None:
    with session_scope(factory) as s:
        log_event(s, FixedClock(at), level, source, message, {"error": message})


def deps(w: SoakWorld) -> soak.SoakDeps:
    settings = rt.quiet_settings(w.core)
    return soak.SoakDeps(
        factory=w.factory,
        clock=w.clock,
        calendar=CAL,
        settings=settings,
        plan=soak.readonly_plan(w.factory, w.clock, CAL, settings),
        orb_enabled=soak.orb_enabled_reader(w.factory, w.clock),
        notifier=rt.build_notifier(w.core, w.api),
        render=MessageRenderer("https://trader.test", ZoneInfo("America/Edmonton"), clock=w.clock),
        env="dev",
    )


def last_json(output: str) -> Any:
    """The JSON document: the output's last line (the tests' unconfigured structlog prints debug lines too;
    in the container only INFO and above are logged)."""
    return json.loads(output.strip().splitlines()[-1])


def counts(factory: sessionmaker[Session]) -> dict[str, int]:
    tables = {
        "job_runs": m.JobRun,
        "event_log": m.EventLog,
        "settings": m.Setting,
        "runs": m.Run,
        "strategy_configs": m.StrategyConfig,
        "audit_log": m.AuditLog,
        "notifications": m.Notification,
    }
    with factory() as s:
        return {
            name: s.execute(select(func.count()).select_from(t)).scalar_one() for name, t in tables.items()
        }


# --- 11. load_report over seeded rows -----------------------------------------------------------------------


def test_load_report_over_seeded_rows(world: SoakWorld) -> None:
    seed_rows(world.factory, MON, clean_rows(MON))
    seed_rows(world.factory, TUE, clean_rows(TUE))
    seed_event(world.factory, et(TUE, 2, 0), "error", rt.TOKEN_SOURCE, "refresh failed")  # recovered at 08:00
    report = soak.load_report(deps(world), sessions=3)
    assert [d.session_date for d in report.days] == [FRI, MON, TUE]
    assert [d.verdict for d in report.days] == ["not_clean", "clean", "clean"]  # Friday: no container yet
    assert report.days[0].failed[0] == "nightly missing"
    assert "weekly missing" in report.days[0].failed
    assert report.consecutive_clean == 2
    assert report.total_clean == 2
    assert report.last_final == TUE
    assert report.earliest_finish == date(2026, 10, 9)
    assert report.days[2].orb_open_seconds == pytest.approx(31.2)
    assert report.through == TUE and report.env == "dev" and report.target == 10


def test_marks_from_event_log_apply(world: SoakWorld) -> None:
    seed_rows(world.factory, MON, clean_rows(MON))
    seed_rows(world.factory, TUE, clean_rows(TUE))
    soak.record_mark(world.factory, world.clock, None, "reset", TUE, "abc123: rule 3")
    report = soak.load_report(deps(world), sessions=2)
    assert report.days[1].reset is True
    assert report.consecutive_clean == 1
    soak.record_mark(world.factory, FixedClock(et(TUE, 20, 1)), None, "outage", MON, "worker down 25 min")
    world.clock.set(et(TUE, 20, 2))
    report = soak.load_report(deps(world), sessions=2)
    assert report.days[0].verdict == "not_clean"
    assert report.days[0].outage == "worker down 25 min"
    soak.record_mark(world.factory, FixedClock(et(TUE, 20, 3)), None, "clear", MON, "mistake")
    world.clock.set(et(TUE, 20, 4))
    assert soak.load_report(deps(world), sessions=2).days[0].verdict == "clean"


def test_a_job_keyed_to_another_session_does_not_count(world: SoakWorld) -> None:
    """Rows are matched by `session_date`: the Monday nightly run on Sunday counts for Monday only."""
    rows = [r for r in clean_rows(MON) if r.job != "nightly"]
    seed_rows(world.factory, MON, rows)
    with session_scope(world.factory) as s:
        s.add(
            m.JobRun(
                job="nightly",
                session_date=TUE,  # the Tuesday nightly, run on Monday evening
                started_at=et(MON, 20, 0),
                finished_at=et(MON, 20, 4),
                status="succeeded",
            )
        )
    report = soak.load_report(deps(world), sessions=2)
    nightly = {d.session_date: [c for c in d.checks if c.job == "nightly"][0].status for d in report.days}
    assert nightly == {MON: "missing", TUE: "succeeded"}


# --- 11. read-only through the CLI --------------------------------------------------------------------------


class LongKeySip(OrbSip):
    """A plug-in whose plan has a problem (a key over 39 characters)."""

    key = "long_key_sip"

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [*super().schedule(cal), ScheduledEvent("k" * 40, SessionOffset.parse("open+10m"))]


@pytest.mark.parametrize("live_run", [False, True])
def test_soak_report_writes_nothing(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, live_run: bool
) -> None:
    w = make_world(db_factory, monkeypatch)
    plugins = {"orb_sip": OrbSip, "long_key_sip": LongKeySip}
    StrategyRegistry(w.factory, w.clock, plugins=plugins).ensure_defaults()
    monkeypatch.setattr(registry_mod, "load_all", lambda: plugins)
    if live_run:
        get_live_run(w.factory, w.clock, RuntimeSettings())
    seed_rows(w.factory, TUE, clean_rows(TUE))
    before = counts(w.factory)
    plan = soak.readonly_plan(w.factory, w.clock, CAL, RuntimeSettings)(TUE)
    assert plan.problems and plan.problems[0].kind == "key_too_long"  # the plan really has a problem
    for args in (["soak-report"], ["soak-report", "--json"], ["soak-report", "--through", "2026-09-29"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert counts(w.factory) == before  # no live run, no plan-problem events, no defaults, no job_runs row
    with w.factory() as s:
        assert s.execute(select(m.JobRun).where(m.JobRun.job.like("soak%"))).first() is None


def test_soak_report_with_no_strategy_configs_writes_nothing(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh database: no config for any plug-in (a `KeyError` in the registry) is never an event."""
    w = make_world(db_factory, monkeypatch)
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    before = counts(w.factory)
    result = runner.invoke(app, ["soak-report", "--json"])
    assert result.exit_code == 0, result.output
    assert counts(w.factory) == before
    day = last_json(result.output)["days"][-1]
    assert "event:orb_open" in {c["job"] for c in day["checks"]}  # no config yet: orb_sip counts as enabled


def test_json_shape(world: SoakWorld) -> None:
    seed_rows(world.factory, MON, clean_rows(MON))
    seed_rows(world.factory, TUE, clean_rows(TUE))
    result = runner.invoke(app, ["soak-report", "--json", "--sessions", "2"])
    assert result.exit_code == 0, result.output
    data = last_json(result.output)
    assert list(data) == [
        "env",
        "generated_at",
        "through",
        "target",
        "consecutive_clean",
        "total_clean",
        "last_final",
        "earliest_finish",
        "days",
    ]
    assert data["env"] == "dev" and data["through"] == "2026-09-29" and data["consecutive_clean"] == 2
    assert data["generated_at"] == "2026-09-30T00:00:00Z"
    assert data["earliest_finish"] == "2026-10-09"
    day = data["days"][0]
    assert list(day) == [
        "session_date",
        "verdict",
        "failed",
        "orb_open",
        "orb_open_seconds",
        "reset",
        "outage",
        "checks",
    ]
    assert day["orb_open"] == "succeeded" and day["orb_open_seconds"] == 31.2
    first = day["checks"][0]
    assert list(first) == ["job", "status", "attempts", "deadline", "finished_at", "error"]
    assert first["deadline"].endswith("Z")


def test_the_table_and_its_summary(world: SoakWorld) -> None:
    seed_rows(world.factory, TUE, clean_rows(TUE))
    result = runner.invoke(app, ["soak-report", "--sessions", "2"])
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0].startswith("2026-09-28 Mon  not_clean")
    assert "nightly missing" in lines[0]
    assert lines[1].startswith("2026-09-29 Tue  clean") and "9:35 31.2s" in lines[1]
    assert lines[2] == (
        "dev: 1/10 clean in a row, 1 clean in the window, last final 2026-09-29, earliest finish 2026-10-12"
    )


def test_an_unreadable_database_is_exit_1(world: SoakWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(soak, "load_report", broken)
    result = runner.invoke(app, ["soak-report"])
    assert result.exit_code == 1
    assert "soak-report: failed: RuntimeError: connection refused" in result.output


def test_a_bad_through_is_exit_1(world: SoakWorld) -> None:
    result = runner.invoke(app, ["soak-report", "--through", "29/09/2026"])
    assert result.exit_code == 1


# --- 13. the Telegram line ----------------------------------------------------------------------------------


def test_notify_twice_sends_one_message(world: SoakWorld) -> None:
    seed_rows(world.factory, MON, clean_rows(MON))
    seed_rows(world.factory, TUE, clean_rows(TUE))
    world.clock.set(et(TUE, 18, 5))
    first = runner.invoke(app, ["soak-report", "--notify"])
    assert first.exit_code == 0, first.output
    assert "sent soak:2026-09-29" in first.output
    second = runner.invoke(app, ["soak-report", "--notify"])
    assert second.exit_code == 0, second.output
    assert "already sent soak:2026-09-29" in second.output
    sends = world.api.calls_of("send_message")
    assert len(sends) == 1
    text = sends[0]["text"]
    assert text.startswith("<b>Soak Tue 29 Sep</b>: clean ✅")
    assert "16:05 MT" in text
    assert "2 clean days in a row (target 10)" in text
    assert sends[0]["silent"] is True
    with world.factory() as s:
        keys = list(s.execute(select(m.Notification.dedupe_key, m.Notification.kind)).all())
    assert keys == [("soak:2026-09-29", "soak")]
    # the Saturday line is its own message; the weekday one is not sent again
    final = runner.invoke(app, ["soak-report", "--notify", "--final", "--through", "2026-09-29"])
    assert "sent soak:2026-09-29:final" in final.output
    assert len(world.api.calls_of("send_message")) == 2


def test_a_not_clean_line_sounds_and_prod_says_ops(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch, env="prod")
    monkeypatch.setattr(registry_mod, "load_all", lambda: {"orb_sip": OrbSip, "spy_overlay": SpyOverlay})
    rows = [r for r in clean_rows(TUE) if r.job != "preopen"]
    seed_rows(w.factory, TUE, rows)
    w.clock.set(et(TUE, 18, 5))
    result = runner.invoke(app, ["soak-report", "--notify"])
    assert result.exit_code == 0, result.output
    [sent] = w.api.calls_of("send_message")
    assert sent["text"].startswith("<b>Ops Tue 29 Sep</b>: NOT clean ❌ preopen missing")
    assert "target" not in sent["text"]
    assert sent["silent"] is False


def test_notify_on_a_non_session_day_sends_nothing(world: SoakWorld) -> None:
    world.clock.set(et(date(2026, 10, 3), 10, 30))  # a Saturday, without --final
    result = runner.invoke(app, ["soak-report", "--notify"])
    assert result.exit_code == 0
    assert "not a trading session, nothing to send" in result.output
    assert world.api.calls_of("send_message") == []


def test_the_saturday_final_line_settles_friday(world: SoakWorld) -> None:
    fri = date(2026, 10, 2)
    seed_rows(world.factory, fri, clean_rows(fri))
    world.clock.set(et(date(2026, 10, 3), 10, 30))
    result = runner.invoke(app, ["soak-report", "--notify", "--final"])
    assert result.exit_code == 0, result.output
    [sent] = world.api.calls_of("send_message")
    assert sent["text"].startswith("<b>Soak Fri 2 Oct</b> (final): clean ✅")


def test_without_telegram_nothing_is_sent(
    db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(db_factory, monkeypatch, telegram=False)
    result = runner.invoke(app, ["soak-report", "--notify"])
    assert result.exit_code == 0
    assert "Telegram not configured" in result.output
    assert counts(w.factory)["notifications"] == 0


def test_a_day_that_changed_since_the_last_line_is_named(world: SoakWorld) -> None:
    seed_rows(world.factory, MON, clean_rows(MON))
    world.clock.set(et(MON, 18, 5))
    assert runner.invoke(app, ["soak-report", "--notify"]).exit_code == 0
    soak.record_mark(world.factory, FixedClock(et(TUE, 10, 0)), None, "outage", MON, "container down 40 min")
    seed_rows(world.factory, TUE, clean_rows(TUE))
    world.clock.set(et(TUE, 18, 5))
    assert runner.invoke(app, ["soak-report", "--notify"]).exit_code == 0
    first, second = (c["text"] for c in world.api.calls_of("send_message"))
    assert "is now" not in first
    assert "Mon 28 Sep is now not clean: outage (container down 40 min)" in second


# --- 14. masking --------------------------------------------------------------------------------------------


def test_errors_are_masked_in_the_table_json_and_message(world: SoakWorld) -> None:
    secret = (
        "HTTP 401 Bearer abcd1234efgh5678 https://api01.iq.questrade.com/v1?access_token=SECRETVALUE123 "
        + ("x" * 5000)
    )
    rows = [r for r in clean_rows(TUE) if r.job != "preopen"]
    rows.append(JobRunRow("preopen", "failed", et(TUE, 9, 20), et(TUE, 9, 21), secret))
    seed_rows(world.factory, TUE, rows)
    world.clock.set(et(TUE, 18, 5))
    table = runner.invoke(app, ["soak-report", "--sessions", "1"]).output
    as_json = runner.invoke(app, ["soak-report", "--sessions", "1", "--json"]).output
    runner.invoke(app, ["soak-report", "--sessions", "1", "--notify"])
    message = world.api.calls_of("send_message")[0]["text"]
    for text in (table, as_json, message):
        assert "abcd1234efgh5678" not in text
        assert "SECRETVALUE123" not in text
        assert "[REDACTED]" in text
    [check] = [c for c in last_json(as_json)["days"][0]["checks"] if c["job"] == "preopen"]
    assert len(check["error"]) == 200
    assert "x" * 201 not in table


def test_the_line_ignores_detail_payloads(world: SoakWorld) -> None:
    with session_scope(world.factory) as s:
        s.add(
            m.JobRun(
                job="preopen",
                session_date=TUE,
                started_at=et(TUE, 9, 20),
                finished_at=et(TUE, 9, 20, 30),
                status="succeeded",
                detail={"password": "hunter2hunter2"},
            )
        )
    result = runner.invoke(app, ["soak-report", "--json", "--sessions", "1"])
    assert result.exit_code == 0, result.output
    assert "hunter2" not in result.output
