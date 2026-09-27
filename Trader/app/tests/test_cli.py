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
