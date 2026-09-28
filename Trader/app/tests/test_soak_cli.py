"""P6-T2 acceptance test 15: `trader soak-mark` (validation, one `info` event with source `soak`, never
relayed), and the soak command registration. Real test database; no network."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
from tests.fakes_api import test_core
from tests.fakes_telegram import FakeMessenger, FakeRenderer, RecordingNotifier
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import get_live_run
from trader.events import log_event
from trader.market.clock import ET, FixedClock
from trader.notify.relay import NotificationRelay
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

runner = CliRunner()
TUE = date(2026, 9, 29)


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


@pytest.fixture
def core(db_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> Iterator[Core]:
    c = test_core(db_factory, FixedClock(et(TUE, 20, 0)))
    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: c)
    yield c


def soak_events(factory: sessionmaker[Session]) -> list[m.EventLog]:
    with factory() as s:
        return list(s.execute(select(m.EventLog).where(m.EventLog.source == "soak")).scalars())


def event_count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(m.EventLog)).scalar_one()


def test_a_mark_on_a_non_session_is_refused(core: Core) -> None:
    result = runner.invoke(app, ["soak-mark", "outage", "--date", "2026-09-26", "--reason", "down"])
    assert result.exit_code == 1
    assert "2026-09-26 is not a trading session" in result.output
    assert event_count(core.factory) == 0


@pytest.mark.parametrize("reason", ["", "   ", "r" * 201])
def test_the_reason_must_be_1_to_200_characters(core: Core, reason: str) -> None:
    result = runner.invoke(app, ["soak-mark", "outage", "--date", "2026-09-29", "--reason", reason])
    assert result.exit_code == 1
    assert "reason must be 1-200 characters" in result.output
    assert event_count(core.factory) == 0


def test_an_unknown_mark_or_bad_date_is_refused(core: Core) -> None:
    assert runner.invoke(app, ["soak-mark", "pause", "--date", "2026-09-29", "--reason", "x"]).exit_code == 1
    assert runner.invoke(app, ["soak-mark", "reset", "--date", "29-09-2026", "--reason", "x"]).exit_code == 1
    assert event_count(core.factory) == 0


def test_a_valid_mark_is_one_info_event_never_relayed(core: Core) -> None:
    run = get_live_run(core.factory, core.clock, RuntimeSettings())
    notifier = RecordingNotifier()
    relay = NotificationRelay(
        core.factory,
        core.clock,
        notifier,
        FakeRenderer(),
        FakeMessenger(),
        run.id,
        settings=RuntimeSettings,
    )
    asyncio.run(relay.pump())  # the cursors start here
    result = runner.invoke(
        app,
        ["soak-mark", "reset", "--date", "2026-09-29", "--reason", "abc123: rule 3 token=SECRETSECRET"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "marked reset 2026-09-29"
    [row] = soak_events(core.factory)
    assert (row.level, row.source, row.run_id) == ("info", "soak", run.id)
    assert row.data == {"mark": "reset", "session_date": "2026-09-29"}
    assert "SECRETSECRET" not in row.message and row.message.startswith("abc123: rule 3")
    # a control error event IS relayed by the same pump; the mark is not
    with session_scope(core.factory) as s:
        log_event(s, core.clock, "error", "worker", "control alert", {})
    asyncio.run(relay.pump())
    assert [msg.kind for msg in notifier.sent] == ["alert"]
    assert "control alert" in notifier.sent[0].text


def test_a_mark_without_a_live_run_creates_none(core: Core) -> None:
    result = runner.invoke(app, ["soak-mark", "outage", "--date", "2026-09-29", "--reason", "down 12 min"])
    assert result.exit_code == 0, result.output
    [row] = soak_events(core.factory)
    assert row.run_id is None
    with core.factory() as s:
        assert s.execute(select(func.count()).select_from(m.Run)).scalar_one() == 0


# --- 17. the docs -------------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[3]


def test_spec_section_9_and_the_master_plan_have_the_soak_rows() -> None:
    spec = (ROOT / "Trader" / "docs" / "SPEC.md").read_text()
    section = spec.split("## 9. Schedule", 1)[1].split("## 10.", 1)[0]
    assert "| 18:05 Mon–Fri | 16:05 | Soak/ops line" in section
    assert "| Sat 10:30 | 08:30 | Final soak/ops line" in section  # the Saturday line (P6-T2)
    assert "17:05 MT from 2026-11-01" in section and "09:30 MT from 2026-11-01" in section
    assert "UTC−6 all year" in section
    plan = (ROOT / "Trader" / "docs" / "plans" / "2026-09-26-build-master-plan.md").read_text()
    contracts = plan.split("### 7.1 Cross-phase contracts", 1)[1].split("### 7.2", 1)[0]
    rows = {line.split("|")[1].strip() for line in contracts.splitlines() if line.startswith("| ")}
    assert "Soak report" in rows
    assert "soak_line" in contracts and "source `soak`" in contracts
