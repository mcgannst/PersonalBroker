"""P6-T6 tests 10, 11 (and the parsing half of 14): `build/cron_gap.py` lists the crontab lines that fell in a
deploy's downtime window, each with the session it must be run for. The real `docker/crontab` is read; the
assertions name the lines that must be present, not the whole output (P6-T2 adds lines in parallel)."""

from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from tests.build import load_build_script

CRONTAB = Path(__file__).resolve().parents[3] / "docker" / "crontab"
EXEC = "docker --context shared-docker-server exec trader-dev trader"


@pytest.fixture(scope="module")
def cron_gap() -> ModuleType:
    return load_build_script("cron_gap")


def _run(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str], start: str, end: str, *extra: str
) -> tuple[int, list[str], str]:
    code = cron_gap.main(["--from", start, "--to", end, *extra])
    captured = capsys.readouterr()
    return code, captured.out.splitlines(), captured.err


def _lines_with(lines: list[str], text: str) -> list[str]:
    return [line for line in lines if text in line]


def test_the_real_crontab_is_the_default(cron_gap: ModuleType) -> None:
    assert Path(cron_gap.DEFAULT_CRONTAB).resolve() == CRONTAB


def test_nightly_is_pinned_to_the_next_session(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T19:58:00-04:00", "2026-10-05T20:03:00-04:00")
    assert code == 0
    [line] = _lines_with(lines, "nightly")
    assert line.endswith(f"{EXEC} nightly --date 2026-10-06")
    assert "2026-10-05 20:00 EDT" in line and "18:00 MDT" in line  # ET then MT


def test_nightly_before_thanksgiving_skips_the_holiday(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-11-25T19:58:00-05:00", "2026-11-25T20:03:00-05:00")
    assert code == 0
    [line] = _lines_with(lines, "nightly")
    assert line.endswith(f"{EXEC} nightly --date 2026-11-27")
    assert "EST" in line


def test_the_935_event_is_listed_with_the_market_hours_warning(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T09:30:00-04:00", "2026-10-05T09:40:00-04:00")
    assert code == 0
    [line] = _lines_with(lines, "orb_open")
    assert line.endswith(f"{EXEC} event orb_open --date 2026-10-05")
    assert "09:36 EDT" in line
    assert "warning: the window overlaps 09:15-16:30 ET on a weekday" in lines


def test_saturday_weekly_is_pinned_to_the_weeks_last_session(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-03T08:55:00-04:00", "2026-10-03T09:05:00-04:00")
    assert code == 0
    [line] = _lines_with(lines, "weekly")
    assert line.endswith(f"{EXEC} weekly --date 2026-10-02")
    assert not _lines_with(lines, "warning")  # Saturday is not a weekday


def test_preopen_after_the_dst_change_uses_eastern_standard_time(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # 09:20 EST is 14:20Z: the window is given in UTC to prove the conversion.
    code, lines, _ = _run(cron_gap, capsys, "2026-11-02T14:19:00+00:00", "2026-11-02T14:21:00Z")
    assert code == 0
    [line] = _lines_with(lines, "preopen")
    assert line.endswith(f"{EXEC} preopen --date 2026-11-02")
    # MT from the tz database, not a hard-coded offset: tzdata 2026 has Alberta on UTC-6 all year after
    # 2026-11-01 (abbreviation "CST"), so this prints 08:20.
    mountain = datetime(2026, 11, 2, 14, 20, tzinfo=UTC).astimezone(ZoneInfo("America/Edmonton"))
    assert f"2026-11-02 09:20 EST  {mountain:%Y-%m-%d %H:%M %Z}  " in line


def test_a_holiday_morning_prints_not_a_session(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-11-26T07:55:00-05:00", "2026-11-26T08:05:00-05:00")
    assert code == 0
    [line] = _lines_with(lines, "premarket")
    assert "not a session (2026-11-26): nothing to run" in line
    assert "docker" not in line


def test_token_refresh_overnight_has_no_date(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-06T02:30:00-04:00")
    assert code == 0
    [line] = _lines_with(lines, "token-refresh")
    assert line.endswith(f"{EXEC} token-refresh")
    assert not _lines_with(lines, "nightly")  # 20:00 is before the window (and its 60 s margin)


def test_an_empty_window_prints_nothing_skipped(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T21:00:00-04:00", "2026-10-05T21:10:00-04:00")
    assert code == 0
    assert lines == ["nothing skipped"]


def test_the_window_has_a_60_second_margin(cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T20:00:30-04:00", "2026-10-05T20:05:00-04:00")
    assert code == 0 and _lines_with(lines, "nightly")
    code, lines, _ = _run(cron_gap, capsys, "2026-10-05T20:01:01-04:00", "2026-10-05T20:05:00-04:00")
    assert code == 0 and lines == ["nothing skipped"]


def test_lines_are_in_time_order_and_name_the_container(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    code, lines, _ = _run(
        cron_gap, capsys, "2026-10-05T11:00:00-04:00", "2026-10-05T13:00:00-04:00", "--container", "trader"
    )
    assert code == 0
    commands = [line.split("exec trader trader ", 1)[1] for line in lines if "exec trader trader " in line]
    assert commands == [
        "checkin --at 11:30 --date 2026-10-05",
        "event --due --date 2026-10-05",
        "event flatten --date 2026-10-05",
        "event flatten --date 2026-10-05",
    ]


def test_soak_report_is_pinned_with_through_and_keeps_its_flags(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    crontab = tmp_path / "crontab"
    crontab.write_text(
        "# comment\nCRON_TZ=America/New_York\n"
        "5 18 * * 1-5  trader soak-report --notify\n"
        "30 10 * * 6   trader soak-report --notify --final\n"
        "0 3 * * *     trader something-new --flag\n"
    )
    code, lines, _ = _run(
        cron_gap, capsys, "2026-10-02T18:00:00-04:00", "2026-10-03T10:40:00-04:00", "--crontab", str(crontab)
    )
    assert code == 0
    assert _lines_with(lines, "soak-report --notify --through 2026-10-02")
    assert _lines_with(lines, "soak-report --notify --final --through 2026-10-02")
    [unknown] = _lines_with(lines, "something-new")
    assert unknown.endswith("trader something-new --flag (no date pinned)")


@pytest.mark.parametrize(
    "line",
    ["*/5 * * * * trader token-refresh", "0 2 * jan * trader token-refresh", "0 2 * * mon trader nightly"],
)
def test_an_unsupported_line_is_refused_with_exit_2(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path, line: str
) -> None:
    crontab = tmp_path / "crontab"
    crontab.write_text(f"CRON_TZ=America/New_York\n{line}\n")
    code, _, err = _run(
        cron_gap, capsys, "2026-10-05T19:58:00-04:00", "2026-10-05T20:03:00-04:00", "--crontab", str(crontab)
    )
    assert code == 2
    assert line in err


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-05T19:58:00", "2026-10-05T20:03:00-04:00"),  # naive
        ("2026-10-05T20:03:00-04:00", "2026-10-05T19:58:00-04:00"),  # backwards
        ("yesterday", "2026-10-05T20:03:00-04:00"),
    ],
)
def test_bad_window_is_refused_with_exit_2(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str], start: str, end: str
) -> None:
    code, _, err = _run(cron_gap, capsys, start, end)
    assert code == 2
    assert err


def test_the_live_dry_check_from_the_plan(cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, lines, _ = _run(
        cron_gap,
        capsys,
        "2026-09-28T19:55:00-04:00",
        "2026-09-28T20:05:00-04:00",
        "--container",
        "trader-dev",
    )
    assert code == 0
    assert _lines_with(lines, f"{EXEC} nightly --date 2026-09-29")


# --- fix round 1: jobs that may have been interrupted by the recreate (plan D3) -----------------------------


def test_a_job_started_before_the_down_stamp_is_listed_as_maybe_interrupted(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The nightly fired at 20:00 and may still be running at a 20:30 down stamp: the recreate kills it. It is
    not a skipped fire (stdout stays `nothing skipped`), but stderr lists it, pinned, with the job_runs
    check."""
    code, lines, err = _run(cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-05T20:40:00-04:00")
    assert code == 0
    assert lines == ["nothing skipped"]
    err_lines = err.splitlines()
    assert err_lines[0].startswith("may have been interrupted: started in the 120 min before the down stamp")
    assert "2026-10-06T00:30:00Z" in err_lines[0]
    assert "job_runs" in err_lines[0] and "running or failed" in err_lines[0]
    assert err_lines[1:] == [
        f"  2026-10-05 20:00 EDT  2026-10-05 18:00 MDT  {EXEC} nightly --date 2026-10-06",
    ]  # the 18:05 soak line is 145 min before: outside the default lookback


def test_the_lookback_flag_widens_narrows_or_turns_it_off(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, err = _run(
        cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-05T20:40:00-04:00", "--lookback", "150"
    )
    assert f"{EXEC} soak-report --notify --through 2026-10-05" in err
    assert "the 150 min before" in err
    _, _, err = _run(
        cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-05T20:40:00-04:00", "--lookback", "20"
    )
    assert err.splitlines() == ["interrupted: no cron job started in the 20 min before the down stamp"]
    _, lines, err = _run(
        cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-05T20:40:00-04:00", "--lookback", "0"
    )
    assert lines == ["nothing skipped"] and err == ""
    code, _, err = _run(
        cron_gap, capsys, "2026-10-05T20:30:00-04:00", "2026-10-05T20:40:00-04:00", "--lookback", "-5"
    )
    assert code == 2 and "--lookback" in err


def test_a_fire_inside_the_margin_is_skipped_not_interrupted(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fire at or after `from - 60 s` is in the skipped list only, never listed twice."""
    _, lines, err = _run(cron_gap, capsys, "2026-10-05T20:01:00-04:00", "2026-10-05T20:05:00-04:00")
    assert _lines_with(lines, "nightly --date 2026-10-06")
    assert "nightly" not in err
    _, lines, err = _run(cron_gap, capsys, "2026-10-05T20:01:01-04:00", "2026-10-05T20:05:00-04:00")
    assert lines == ["nothing skipped"]
    assert f"{EXEC} nightly --date 2026-10-06" in err


def test_a_holiday_fire_is_not_listed_as_interrupted(
    cron_gap: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Thanksgiving 09:40 EST: the 08:00 premarket and 09:20 preopen only printed "not a trading session"."""
    _, lines, err = _run(
        cron_gap, capsys, "2026-11-26T09:40:00-05:00", "2026-11-26T09:45:00-05:00", "--container", "trader"
    )
    assert lines[0] == "nothing skipped"
    assert err.splitlines() == ["interrupted: no cron job started in the 120 min before the down stamp"]
    _, _, err = _run(
        cron_gap, capsys, "2026-11-27T09:40:00-05:00", "2026-11-27T09:45:00-05:00", "--container", "trader"
    )
    assert "exec trader trader premarket --date 2026-11-27" in err
    assert "exec trader trader event orb_open --date 2026-11-27" in err
    assert err.index("premarket") < err.index("preopen") < err.index("orb_open")
