from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from trader import __version__
from trader.cli import app
from trader.jobs.runner import JobOutcome
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import RuntimeSettings


def test_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_premarket_command_is_registered() -> None:
    from typer.testing import CliRunner

    from trader.cli import app

    result = CliRunner().invoke(app, ["premarket", "--help"])
    assert result.exit_code == 0 and "--date" in result.output


# --- premarket: --date validation, the pre-market window, fresh screens, skipped output ---


class _Scraper:
    made: ClassVar[list[dict[str, object]]] = []

    def __init__(self, **kwargs: object) -> None:
        _Scraper.made.append(kwargs)

    def __enter__(self) -> "_Scraper":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _premarket(
    monkeypatch: pytest.MonkeyPatch, now: datetime, outcome: JobOutcome | None = None
) -> list[Any]:
    """Patch build_core, the scraper and run_job; return the list run_job's calls are recorded in."""
    import trader.adapters.finviz.scraper
    import trader.bootstrap
    import trader.jobs.runner

    core = SimpleNamespace(
        settings=SimpleNamespace(load=RuntimeSettings),
        clock=FixedClock(now),
        calendar=SessionCalendar(),
        env=SimpleNamespace(anthropic_api_key=None),
        factory=None,
        crypto=None,
    )
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    _Scraper.made = []
    monkeypatch.setattr(trader.adapters.finviz.scraper, "FinvizScraper", _Scraper)
    calls: list[Any] = []

    def fake_run_job(*args: Any, **kwargs: Any) -> JobOutcome:
        calls.append((args, kwargs))
        return outcome or JobOutcome("succeeded", {"brief": "Pre-market brief"})

    monkeypatch.setattr(trader.jobs.runner, "run_job", fake_run_job)
    return calls


PRE_OPEN = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # Tue 08:00 ET


@pytest.mark.parametrize(
    ("value", "message"),
    [("2026-13-40", "not a valid date"), ("yesterday", "not a valid date"), ("2035-01-02", "outside")],
)
def test_premarket_bad_date_exits_1_cleanly(
    monkeypatch: pytest.MonkeyPatch, value: str, message: str
) -> None:
    calls = _premarket(monkeypatch, PRE_OPEN)
    result = CliRunner().invoke(app, ["premarket", "--date", value])
    assert result.exit_code == 1 and message in result.output and "Traceback" not in result.output
    assert calls == []


@pytest.mark.parametrize(
    ("args", "now", "why"),
    [
        (["--date", "2026-10-07"], PRE_OPEN, "not today's ET date"),  # tomorrow's session
        (["--date", "2026-10-05"], PRE_OPEN, "not today's ET date"),  # yesterday's session
        ([], datetime(2026, 10, 6, 13, 30, tzinfo=UTC), "already opened"),  # 09:30 ET
        ([], datetime(2026, 10, 6, 19, 0, tzinfo=UTC), "already opened"),  # 15:00 ET
    ],
)
def test_premarket_outside_the_window_needs_force(
    monkeypatch: pytest.MonkeyPatch, args: list[str], now: datetime, why: str
) -> None:
    calls = _premarket(monkeypatch, now)
    result = CliRunner().invoke(app, ["premarket", *args])
    assert result.exit_code == 1 and why in result.output and "--force" in result.output
    assert calls == []  # no job_runs row is written

    calls = _premarket(monkeypatch, now)
    forced = CliRunner().invoke(app, ["premarket", *args, "--force"])
    assert forced.exit_code == 0, forced.output
    assert "WARNING: forced run outside the pre-market window" in forced.output and why in forced.output
    assert len(calls) == 1 and calls[0][1]["force"] is True


def test_premarket_in_the_window_runs_with_uncached_screens(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _premarket(monkeypatch, PRE_OPEN)
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 0 and result.stdout == "Pre-market brief\n"
    assert len(calls) == 1 and calls[0][0][2:4] == ("premarket", date(2026, 10, 6))
    assert _Scraper.made[0]["cache_screens"] is False


def test_premarket_skipped_prints_its_reason_without_a_trailing_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _premarket(monkeypatch, PRE_OPEN, JobOutcome("skipped", {"reason": "already succeeded"}))
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 0
    assert result.stdout == "premarket 2026-10-06: skipped (already succeeded)\n"


def test_premarket_failure_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _premarket(monkeypatch, PRE_OPEN, JobOutcome("failed", error="RuntimeError: no universe"))
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 1 and "failed: RuntimeError: no universe" in result.output


# --- P3-T12: the cron commands, telegram-test, logging first ---------------------------------------------

SESSION = date(2026, 10, 6)  # Tue
HOLIDAY = date(2026, 11, 26)  # Thanksgiving
AT_0930 = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)


def _fake_core(now: datetime = AT_0930) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(load=RuntimeSettings),
        clock=FixedClock(now),
        calendar=SessionCalendar(),
        env=SimpleNamespace(anthropic_api_key=None, telegram_bot_token=None, telegram_chat_id=None),
        factory=None,
        crypto=None,
    )


class _Jobs:
    """Records the runtime job calls the CLI makes; returns the configured outcomes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.outcome = JobOutcome("succeeded", {"ok": True})
        self.fire_results: list[Any] | None = None
        self.raises: BaseException | None = None

    def job(self, name: str) -> Any:
        async def run(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if self.raises is not None:
                raise self.raises
            if name == "event_backup":
                from trader.engine.scheduler import FireResult

                key = args[1] or "due"
                return self.fire_results or [FireResult(key, args[2], "fired", {"outcomes": 1})]
            return self.outcome

        return run


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> _Jobs:
    import trader.bootstrap
    import trader.runtime

    rec = _Jobs()
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: _fake_core())
    for name in ("preopen_job", "checkin_job", "event_backup", "postclose_job"):
        monkeypatch.setattr(trader.runtime, name, rec.job(name))
    return rec


@pytest.mark.parametrize(
    ("args", "job"),
    [
        (["preopen"], "preopen_job"),
        (["checkin", "--at", "11:30"], "checkin_job"),
        (["event", "orb_open"], "event_backup"),
        (["event", "--due"], "event_backup"),
        (["postclose"], "postclose_job"),
    ],
)
def test_cron_commands_run_on_a_session_and_print_their_result(
    jobs: _Jobs, args: list[str], job: str
) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert [c[0] for c in jobs.calls] == [job]
    assert "2026-10-06" in result.output and ("succeeded" in result.output or "fired" in result.output)
    assert "Traceback" not in result.output


def test_job_arguments_reach_the_runtime(jobs: _Jobs) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["preopen", "--date", "2026-10-07", "--force"]).exit_code == 0
    assert runner.invoke(app, ["checkin", "--at", "13:30", "--force"]).exit_code == 0
    assert runner.invoke(app, ["event", "flatten", "--force"]).exit_code == 0
    assert runner.invoke(app, ["event", "--due"]).exit_code == 0
    assert runner.invoke(app, ["postclose"]).exit_code == 0
    (_, a1, k1), (_, a2, k2), (_, a3, k3), (_, a4, k4), (_, a5, k5) = jobs.calls
    assert a1[1:] == (date(2026, 10, 7),) and k1 == {"force": True}
    assert a2[1:] == (SESSION, "13:30") and k2 == {"force": True}  # a job re-run, not a forced fire
    assert a3[1:] == ("flatten", SESSION) and k3 == {"due": False, "force": True}
    assert a4[1:] == (None, SESSION) and k4 == {"due": True, "force": False}
    assert a5[1:] == (SESSION,) and k5 == {"force": False}


@pytest.mark.parametrize(
    "args",
    [["preopen"], ["checkin", "--at", "11:30"], ["event", "orb_open"], ["event", "--due"], ["postclose"]],
)
def test_on_a_holiday_they_print_not_a_trading_session_and_exit_0(jobs: _Jobs, args: list[str]) -> None:
    result = CliRunner().invoke(app, [*args, "--date", HOLIDAY.isoformat()])
    assert result.exit_code == 0, result.output
    assert "not a trading session" in result.output
    assert jobs.calls == []  # nothing is built on a holiday


@pytest.mark.parametrize("status", ["missed", "failed"])
def test_an_event_missed_or_failed_exits_1_with_one_line(jobs: _Jobs, status: str) -> None:
    from trader.engine.scheduler import FireResult

    jobs.fire_results = [FireResult("orb_open", SESSION, status, {"error": "missed: 295s late"})]  # type: ignore[arg-type]
    result = CliRunner().invoke(app, ["event", "orb_open"])
    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1 and status in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("status", ["skipped", "too_early", "not_scheduled", "fired"])
def test_harmless_event_statuses_exit_0(jobs: _Jobs, status: str) -> None:
    from trader.engine.scheduler import FireResult

    jobs.fire_results = [FireResult("flatten", SESSION, status, {})]  # type: ignore[arg-type]
    assert CliRunner().invoke(app, ["event", "flatten"]).exit_code == 0


def test_event_due_force_is_rejected(jobs: _Jobs) -> None:
    result = CliRunner().invoke(app, ["event", "--due", "--force"])
    assert result.exit_code != 0
    assert "--due --force" in result.output
    assert jobs.calls == []


@pytest.mark.parametrize("args", [["event"], ["event", "orb_open", "--due"]])
def test_event_needs_exactly_one_of_a_key_and_due(jobs: _Jobs, args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0 and jobs.calls == []


@pytest.mark.parametrize("at", ["1130", "25:00", "11:3", "noon"])
def test_checkin_rejects_a_bad_time(jobs: _Jobs, at: str) -> None:
    result = CliRunner().invoke(app, ["checkin", "--at", at])
    assert result.exit_code != 0 and jobs.calls == []


def test_a_failed_job_exits_1_with_one_line(jobs: _Jobs) -> None:
    jobs.outcome = JobOutcome("failed", error="RuntimeError: universe missing")
    result = CliRunner().invoke(app, ["postclose"])
    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1 and "RuntimeError: universe missing" in result.output


def test_a_crash_outside_the_job_exits_1_without_a_traceback(jobs: _Jobs) -> None:
    jobs.raises = RuntimeError("database unreachable")
    result = CliRunner().invoke(app, ["preopen"])
    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1 and "RuntimeError" in result.output
    assert "Traceback" not in result.output


def test_a_skipped_job_prints_its_reason(jobs: _Jobs) -> None:
    jobs.outcome = JobOutcome("skipped", {"reason": "already succeeded"})
    result = CliRunner().invoke(app, ["preopen"])
    assert result.exit_code == 0 and "skipped (already succeeded)" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["version"],
        ["preopen"],
        ["checkin", "--at", "11:30"],
        ["event", "orb_open"],
        ["postclose"],
        ["nightly"],
        ["premarket"],
        ["token-refresh"],
        ["questrade-seed"],
        ["questrade-check"],
        ["notify", "hi"],
        ["telegram-test"],
    ],
)
def test_every_command_configures_logging_first(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    import trader.bootstrap
    import trader.config
    import trader.logging_setup

    order: list[str] = []

    def configure(process: str, **kwargs: Any) -> None:
        order.append(f"log:{process}")

    def stop(*a: Any, **k: Any) -> Any:
        order.append("setup")
        raise RuntimeError("stop here")

    monkeypatch.setattr(trader.logging_setup, "configure_logging", configure)
    monkeypatch.setattr(trader.bootstrap, "build_core", stop)
    monkeypatch.setattr(trader.config, "get_env", stop)
    CliRunner().invoke(app, args)
    assert order and order[0] == "log:cron", order


# --- premarket sends its brief; telegram-test ---------------------------------------------------------------


def test_premarket_sends_the_brief_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.runtime
    from tests.fakes_telegram import FakeRenderer, RecordingNotifier

    notifier = RecordingNotifier()
    renderer = FakeRenderer()

    async def no_telegram(*a: Any) -> None:
        return None

    monkeypatch.setattr(trader.runtime, "telegram_configured", lambda env: True)
    monkeypatch.setattr(trader.runtime, "open_telegram", no_telegram)
    monkeypatch.setattr(trader.runtime, "build_notifier", lambda *a, **k: notifier)
    monkeypatch.setattr(trader.runtime, "build_renderer", lambda *a, **k: renderer)
    _premarket(monkeypatch, PRE_OPEN)
    assert CliRunner().invoke(app, ["premarket"]).exit_code == 0
    assert [m.dedupe_key for m in notifier.sent] == ["premarket:2026-10-06"]
    assert notifier.sent[0].kind == "premarket_brief"
    assert renderer.calls[0] == ("premarket_brief", (date(2026, 10, 6), "Pre-market brief"))

    _premarket(monkeypatch, PRE_OPEN, JobOutcome("skipped", {"reason": "already succeeded"}))
    assert CliRunner().invoke(app, ["premarket"]).exit_code == 0
    _premarket(monkeypatch, PRE_OPEN)  # a forced re-run that succeeds again: the dedupe key holds
    assert CliRunner().invoke(app, ["premarket", "--force"]).exit_code == 0
    assert len(notifier.sent) == 1


def test_premarket_brief_failure_does_not_fail_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.runtime

    async def broken(*a: Any) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(trader.runtime, "telegram_configured", lambda env: True)
    monkeypatch.setattr(trader.runtime, "open_telegram", broken)
    _premarket(monkeypatch, PRE_OPEN)
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 0 and result.stdout.startswith("Pre-market brief\n")
    assert "telegram down" not in result.output  # only the exception type is logged


def _telegram_env(configured: bool = True) -> Any:
    from pydantic import SecretStr

    from trader.config import EnvSettings

    return EnvSettings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr("postgresql+psycopg://unused"),
        migration_database_url=SecretStr("postgresql+psycopg://unused"),
        app_encryption_key=SecretStr("k"),
        session_secret=SecretStr("s"),
        telegram_bot_token=SecretStr("123456:TEST-TOKEN-NOT-REAL") if configured else None,
        telegram_chat_id=4242 if configured else None,
    )


@pytest.mark.parametrize("buttons", [False, True])
def test_telegram_test_sends_one_labelled_message(monkeypatch: pytest.MonkeyPatch, buttons: bool) -> None:
    import trader.config
    import trader.runtime
    from tests.fakes_telegram import FakeTelegramApi

    api = FakeTelegramApi()
    monkeypatch.setattr(trader.config, "get_env", lambda: _telegram_env())
    monkeypatch.setattr(trader.runtime, "build_telegram_api", lambda env: api)
    args = ["telegram-test", "--buttons"] if buttons else ["telegram-test"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "sent message 1"
    [sent] = api.calls_of("send_message")
    assert sent["chat_id"] == 4242 and "test" in sent["text"].lower()
    if buttons:
        [row] = sent["buttons"]
        assert [b.callback_data for b in row] == ["test", "test"]
    else:
        assert sent["buttons"] == ()
    assert api.calls_of("get_updates") == []  # it only sends, never polls
    assert api.calls_of("aclose")  # the client is closed


def test_telegram_test_without_configuration_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.config

    monkeypatch.setattr(trader.config, "get_env", lambda: _telegram_env(configured=False))
    result = CliRunner().invoke(app, ["telegram-test"])
    assert result.exit_code == 1 and "isn't configured" in result.output


def test_telegram_test_failure_is_one_line_without_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    import trader.config
    import trader.runtime
    from tests.fakes_telegram import FakeTelegramApi
    from trader.adapters.telegram.types import TelegramApiError

    api = FakeTelegramApi()
    api.fail("send_message", TelegramApiError(400, "Bad Request: chat not found"))
    monkeypatch.setattr(trader.config, "get_env", lambda: _telegram_env())
    monkeypatch.setattr(trader.runtime, "build_telegram_api", lambda env: api)
    result = CliRunner().invoke(app, ["telegram-test"])
    assert result.exit_code == 1
    assert len(result.output.strip().splitlines()) == 1 and "chat not found" in result.output
    assert "TEST-TOKEN-NOT-REAL" not in result.output
