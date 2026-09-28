"""P6-T11 acceptance tests 2 and 6: the CLI's decision log hooks and `trader decisions` (record, show,
export, prune).

2. `record_decisions_quietly` runs after a succeeded `premarket` (after its brief was handed off) and after
   `trader event` only when a result is `fired`; a raising recorder changes neither the job's `job_runs` row
   nor the CLI's exit code (one masked `warning` event instead).
6. The four subcommands on a seeded database: an unknown run exits 1, a non-session prints a message and exits
   0, `export` equals `decisions_csv`. None writes a `job_runs` row.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.runtime as rt
from tests.decisions.test_read import D3, seed_day
from tests.factories import add_run
from tests.test_cli import PRE_OPEN, SESSION, _fake_core, _premarket
from trader.bootstrap import Core
from trader.cli import app
from trader.config import EnvSettings
from trader.crypto import Crypto
from trader.db import models as m
from trader.decisions.export import decisions_csv
from trader.engine.scheduler import FireResult
from trader.jobs.runner import JobOutcome
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.settings_store import SettingsStore

CAL = SessionCalendar()
D = date(2026, 10, 6)
HOLIDAY = "2026-11-26"


# --- 2. the hooks -------------------------------------------------------------------------------------------


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records the order of the brief hand-off and the decision log pass (after `_premarket`'s patches)."""
    order: list[str] = []

    async def brief(core: Any, day: date, text: str) -> None:
        order.append(f"brief {day}")

    async def record(core: Any, day: date, *, final: bool = False) -> None:
        order.append(f"decisions {day}")

    monkeypatch.setattr(rt, "send_premarket_brief", brief)
    monkeypatch.setattr(rt, "record_decisions_quietly", record)
    return order


def test_a_succeeded_premarket_records_after_handing_off_the_brief(
    monkeypatch: pytest.MonkeyPatch, hooks: list[str]
) -> None:
    _premarket(monkeypatch, PRE_OPEN)
    monkeypatch.setattr(rt, "send_premarket_brief", _append(hooks, "brief"))
    monkeypatch.setattr(rt, "record_decisions_quietly", _append(hooks, "decisions"))
    result = CliRunner().invoke(app, ["premarket"])
    assert result.exit_code == 0 and result.stdout == "Pre-market brief\n"
    assert hooks == ["brief 2026-10-06", "decisions 2026-10-06"]


@pytest.mark.parametrize(
    "outcome",
    [JobOutcome("skipped", {"reason": "already succeeded"}), JobOutcome("failed", error="RuntimeError: x")],
)
def test_a_skipped_or_failed_premarket_records_nothing(
    monkeypatch: pytest.MonkeyPatch, hooks: list[str], outcome: JobOutcome
) -> None:
    _premarket(monkeypatch, PRE_OPEN, outcome)
    monkeypatch.setattr(rt, "record_decisions_quietly", _append(hooks, "decisions"))
    CliRunner().invoke(app, ["premarket"])
    assert hooks == []


def _append(order: list[str], label: str) -> Any:
    async def hook(core: Any, day: date, *args: Any, **kwargs: Any) -> None:
        order.append(f"{label} {day}")

    return hook


def _event_cli(monkeypatch: pytest.MonkeyPatch, results: list[FireResult]) -> list[str]:
    order: list[str] = []

    async def backup(core: Any, key: Any, day: date, **kw: Any) -> list[FireResult]:
        order.append("fire")
        return results

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: _fake_core())
    monkeypatch.setattr(rt, "event_backup", backup)
    monkeypatch.setattr(rt, "record_decisions_quietly", _append(order, "decisions"))
    return order


@pytest.mark.parametrize(
    ("statuses", "records", "code"),
    [
        (["fired"], True, 0),
        (["skipped"], False, 0),  # the worker fired it first: nothing new to record
        (["too_early"], False, 0),
        (["missed"], False, 1),
        (["fired", "failed"], True, 1),  # --due: one fired, one failed; the exit code stays the event's
    ],
)
def test_trader_event_records_only_when_an_event_fired(
    monkeypatch: pytest.MonkeyPatch, statuses: list[str], records: bool, code: int
) -> None:
    results = [FireResult(f"k{i}", SESSION, s, {}) for i, s in enumerate(statuses)]  # type: ignore[arg-type]
    order = _event_cli(monkeypatch, results)
    args = ["event", "--due"] if len(results) > 1 else ["event", "orb_open"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == code, result.output
    assert order == (["fire", f"decisions {SESSION}"] if records else ["fire"])


def test_a_hook_that_raises_anyway_changes_no_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    order = _event_cli(monkeypatch, [FireResult("orb_open", SESSION, "fired", {"outcomes": 1})])

    async def boom(core: Any, day: date, **kw: Any) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(rt, "record_decisions_quietly", boom)
    result = CliRunner().invoke(app, ["event", "orb_open"])
    assert result.exit_code == 0 and "event orb_open 2026-10-06: fired" in result.output
    assert "Traceback" not in result.output and order == ["fire"]


# --- the database side: a real Core over the test database --------------------------------------------------


def _env() -> EnvSettings:
    return EnvSettings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr("postgresql+psycopg://unused"),
        migration_database_url=SecretStr("postgresql+psycopg://unused"),
        app_encryption_key=SecretStr(Fernet.generate_key().decode()),
        session_secret=SecretStr("p6-t11-secret"),
        public_base_url="https://trader.test",
        tz_display="America/Edmonton",
    )


@pytest.fixture
def core(
    db_factory: sessionmaker[Session], migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Core]:
    clock = FixedClock(datetime(2026, 10, 6, 21, 0, tzinfo=UTC))  # 17:00 ET, after the post-close
    env = _env()
    store = SettingsStore(db_factory, now=clock.now)
    c = Core(
        env, migrated_engine, db_factory, Crypto(env.app_encryption_key.get_secret_value()), clock, CAL, store
    )
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


def _count(factory: sessionmaker[Session], model: Any, *where: Any) -> int:
    with factory() as s:
        return int(s.execute(select(func.count()).select_from(model).where(*where)).scalar_one())


@pytest.mark.db
async def test_a_raising_recorder_leaves_the_job_row_and_writes_one_masked_warning(
    core: Core, monkeypatch: pytest.MonkeyPatch
) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
        s.add(
            m.JobRun(
                job="premarket",
                session_date=D,
                started_at=core.clock.now(),
                finished_at=core.clock.now(),
                status="succeeded",
                detail={"brief": "b"},
            )
        )

    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("connection lost: password=hunter2")

    monkeypatch.setattr(rt, "record_day", broken)
    await rt.record_decisions_quietly(core, D)  # never raises
    with core.factory() as s:
        jobs = s.execute(select(m.JobRun.job, m.JobRun.status)).all()
        events = s.execute(select(m.EventLog).where(m.EventLog.source == "decisions")).scalars().all()
    assert jobs == [("premarket", "succeeded")]
    assert [(e.level, e.run_id) for e in events] == [("warning", run_id)]
    assert "decision log pass for 2026-10-06 failed: RuntimeError" in events[0].message
    assert "hunter2" not in events[0].message


@pytest.mark.db
async def test_record_decisions_quietly_records_the_live_runs_day(core: Core) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
    await rt.record_decisions_quietly(core, D)
    assert _count(core.factory, m.DecisionLog, m.DecisionLog.run_id == run_id) > 0
    assert _count(core.factory, m.DecisionLog, m.DecisionLog.final.is_(True)) == 0


@pytest.mark.db
async def test_without_a_live_run_the_hook_does_nothing(core: Core) -> None:
    await rt.record_decisions_quietly(core, D)
    assert _count(core.factory, m.DecisionLog) == 0
    assert _count(core.factory, m.EventLog) == 0


# --- 6. trader decisions record|show|export|prune -----------------------------------------------------------


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, ["decisions", *args])


@pytest.mark.db
def test_record_then_unchanged_then_final_then_rebuild_keeps_it_final(core: Core) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
    out = _invoke("record", "--date", D.isoformat())
    assert out.exit_code == 0, out.output
    assert f"decisions record {D} run {run_id}: " in out.output and " rows" in out.output
    assert "skipped (unchanged)" in _invoke("record", "--date", D.isoformat()).output
    assert _invoke("record", "--date", D.isoformat(), "--final").output.strip().endswith("final")
    assert "skipped (final, final)" in _invoke("record", "--date", D.isoformat()).output
    rebuilt = _invoke("record", "--date", D.isoformat(), "--rebuild")
    assert rebuilt.exit_code == 0 and rebuilt.output.strip().endswith("rows, final")
    assert _count(core.factory, m.DecisionLog, m.DecisionLog.final.is_(False)) == 0
    assert _count(core.factory, m.JobRun) == 0  # not a job


@pytest.mark.db
def test_show_prints_the_summary_then_one_line_per_row_in_mt(core: Core) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
        seed_day(s, run_id, D3)
    out = _invoke("show", "--date", D3.isoformat())
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert lines[0] == f"decisions {D3} run {run_id} (live, final), 9 rows"
    assert lines[1].startswith("811 scanned · 20 ranked")
    rows = [x.split() for x in lines if " MT  " in x]
    assert len(rows) == 9
    assert ["07:35:05", "MT", "scan", "NVDA", "passed", "-"] in rows  # 09:35:05 ET
    assert ["07:35:05", "MT", "scan", "AMD", "rejected", "rvol_below_min"] in rows
    scan = _invoke("show", "--date", D3.isoformat(), "--stage", "scan", "--outcome", "rejected")
    picked = [x.split() for x in scan.output.splitlines() if " MT  " in x]
    assert [r[3] for r in picked] == ["AMD", "AAPL"]
    limited = _invoke("show", "--date", D3.isoformat(), "--limit", "2")
    assert "... 7 more (use --limit)" in limited.output
    assert (
        _invoke("show", "--date", D.isoformat()).output.strip()
        == f"decisions show {D}: no decisions recorded"
    )
    assert _invoke("show", "--date", D3.isoformat(), "--stage", "nope").exit_code == 2


@pytest.mark.db
def test_export_equals_decisions_csv_on_stdout_and_in_a_file(core: Core, tmp_path: Path) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
        seed_day(s, run_id, D3)
    expected = "".join(decisions_csv(core.factory, run_id, D3))
    out = _invoke("export", "--date", D3.isoformat())
    assert out.exit_code == 0 and out.stdout_bytes.decode() == expected  # .stdout folds CRLF
    path = tmp_path / "d.csv"
    written = _invoke("export", "--date", D3.isoformat(), "--out", str(path))
    assert written.exit_code == 0 and f"written to {path}" in written.output
    assert path.read_bytes().decode() == expected
    empty = _invoke("export", "--date", D.isoformat())
    assert empty.exit_code == 0 and empty.stdout_bytes == b""


@pytest.mark.db
def test_an_export_that_fails_mid_read_emits_no_partial_csv(
    core: Core, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1 (nit): the CSV is buffered and emitted only once the read completed, so a database error
    after some lines leaves nothing on stdout and no (or the old) file at --out, with exit 1."""
    import trader.decisions.export as export_mod

    with core.factory.begin() as s:
        run_id = add_run(s)
        seed_day(s, run_id, D3)

    def breaks_midway(factory: Any, run: int, day: Any) -> Any:
        full = decisions_csv(factory, run, day)
        yield next(full)
        yield next(full)
        full.close()
        raise RuntimeError("server closed the connection: password=hunter2")

    monkeypatch.setattr(export_mod, "decisions_csv", breaks_midway)
    out = _invoke("export", "--date", D3.isoformat())
    assert out.exit_code == 1 and out.stdout_bytes == b""
    assert "failed: RuntimeError" in out.output and "hunter2" not in out.output
    path = tmp_path / "d.csv"
    path.write_text("old export\n")
    to_file = _invoke("export", "--date", D3.isoformat(), "--out", str(path))
    assert to_file.exit_code == 1 and "written to" not in to_file.output
    assert path.read_text() == "old export\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.csv"]  # no leftover side file


@pytest.mark.db
def test_prune_deletes_rows_past_the_retention(core: Core) -> None:
    with core.factory.begin() as s:
        run_id = add_run(s)
        seed_day(s, run_id, D3 - timedelta(days=500), prefix="OLD")
        seed_day(s, run_id, D3)
    out = _invoke("prune")
    assert out.exit_code == 0 and out.output.strip() == "decisions prune: deleted 9 live rows, 0 replay rows"
    assert _count(core.factory, m.DecisionLog) == 9


@pytest.mark.db
@pytest.mark.parametrize("cmd", ["record", "show", "export"])
def test_an_unknown_run_exits_1_and_a_holiday_exits_0(core: Core, cmd: str) -> None:
    with core.factory.begin() as s:
        add_run(s)
    out = _invoke(cmd, "--date", D.isoformat(), "--run", "999")
    assert out.exit_code == 1 and "unknown run 999" in out.output and "Traceback" not in out.output
    holiday = _invoke(cmd, "--date", HOLIDAY)
    assert holiday.exit_code == 0 and "not a trading session" in holiday.output
    bad = _invoke(cmd, "--date", "2026-13-01")
    assert bad.exit_code == 1 and "not a valid date" in bad.output


@pytest.mark.db
def test_record_without_any_live_run_exits_1(core: Core) -> None:
    out = _invoke("record", "--date", D.isoformat())
    assert out.exit_code == 1 and "there is no live run" in out.output


def test_a_database_error_exits_1_with_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_core()

    def broken() -> Any:
        raise RuntimeError("could not connect")

    fake.factory = broken
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: fake)
    monkeypatch.setattr(rt, "install_log_mirror", lambda *a, **k: None)
    for args in (["record", "--date", "2026-10-06"], ["prune"]):
        out = _invoke(*args)
        assert out.exit_code == 1, (args, out.output)
        assert "could not connect" in out.output and "Traceback" not in out.output
