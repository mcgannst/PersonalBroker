"""P5-T18 acceptance test 5: the weekly report day, end to end (BR-61, SPEC §4.3, §9).

A live week (2026-11-23 to 2026-11-27) with trades, journal answers and one kill-switch trip, plus a trade
of the week before (so the run to date differs from the week). It is Saturday 09:00 ET. `trader weekly`
(CliRunner, the real runtime, a fake Anthropic client whose commentary quotes only numbers from the facts it
was given, the fake Telegram API) stores the report, sends ONE message whose numbers equal
`compute_metrics` for that week, the commentary passes `check_numbers`, and `GET /api/reports/weekly`
returns it. A second run is skipped and a forced run sends nothing more (dedupe `weekly:<week_ending>`).
"""

import json
import re
from datetime import UTC, date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

import trader.bootstrap
import trader.runtime as rt
from tests.api.conftest import make_client
from tests.factories import add_symbol
from tests.fakes_api import make_services
from tests.test_runtime import World, world  # noqa: F401 (the fixture)
from trader.api.routers import reports
from trader.bootstrap import Core
from trader.cli import app
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.runs import get_live_run
from trader.market.clock import ET
from trader.notify.types import WeeklyReportView
from trader.reports.metrics import compute_metrics
from trader.reports.weekly import check_numbers
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

MON, TUE, WED, FRI = (date(2026, 11, d) for d in (23, 24, 25, 27))
PREV_FRI = date(2026, 11, 20)
SAT_0900 = datetime(2026, 11, 28, 9, 0, tzinfo=ET).astimezone(UTC)
# (session, ticker, pnl, pnl_r)
LIVE_TRADES = (
    (PREV_FRI, "AAA", "4.0000", "0.8000"),
    (MON, "AAA", "10.5000", "1.0500"),
    (TUE, "BBB", "-7.2000", "-1.2000"),
    (WED, "AAA", "6.0000", "0.6000"),
    (FRI, "BBB", "-2.1000", "-0.3500"),
)
JOURNAL = {MON: True, TUE: False, WED: True}


def et(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


def seed_week(factory: sessionmaker[Session], clock: Any) -> int:
    run = get_live_run(factory, clock, RuntimeSettings())
    with session_scope(factory) as s:
        symbols = {t: add_symbol(s, t) for t in ("AAA", "BBB")}
        equity = Decimal("720")
        for day, ticker, pnl, pnl_r in LIVE_TRADES:
            opened, closed = et(day, 9, 36), et(day, 15, 50)
            pos = m.Position(
                run_id=run.id,
                symbol_id=symbols[ticker],
                strategy_config_id=None,
                qty=0,
                avg_price=Decimal("10"),
                stop_loss=Decimal("9.90"),
                planned_risk=Decimal("10"),
                session_date=day,
                opened_at=opened,
                closed_at=closed,
                entry_order_id=1,
                stop_order_id=None,
                unprotected_since=None,
                unprotected_seconds=0,
            )
            s.add(pos)
            s.flush()
            s.add(
                m.Trade(
                    run_id=run.id,
                    position_id=pos.id,
                    symbol_id=symbols[ticker],
                    session_date=day,
                    entry_price=Decimal("10"),
                    exit_price=Decimal("10") + Decimal(pnl) / 50,
                    qty=50,
                    pnl=Decimal(pnl),
                    pnl_r=Decimal(pnl_r),
                    planned_risk=Decimal("10"),
                    exit_reason="flatten_close" if Decimal(pnl) > 0 else "protective_stop",
                    slippage_total=Decimal("0.0200"),
                    fees_total=Decimal("0.0100"),
                    opened_at=opened,
                    closed_at=closed,
                )
            )
            equity += Decimal(pnl)
            s.add(
                m.EquitySnapshot(
                    run_id=run.id,
                    ts=et(day, 16, 0),
                    equity=equity,
                    cash=equity,
                    settled_cash=equity,
                    peak_equity=equity,
                    drawdown_pct=Decimal("0"),
                )
            )
        for day, followed in JOURNAL.items():
            s.add(
                m.Journal(run_id=run.id, session_date=day, rules_followed=followed, answered_via="telegram")
            )
        s.add(
            m.KillSwitchEvent(
                run_id=run.id,
                switch="daily_loss_pct",
                session_date=TUE,
                tripped_at=et(TUE, 11, 5),
                value=Decimal("0.010000"),
                threshold=Decimal("0.010000"),
            )
        )
    return run.id


def commentary_from(facts: dict[str, Any]) -> str:
    """A commentary that quotes only numbers present in the facts (and enough words to pass the length)."""
    wm = facts["week_metrics"]
    trip = facts["kill_switch_trips"][0]
    best = facts["best_trade"]
    return (
        f"This week the strategy closed {wm['trades']} trades with {wm['wins']} winners and a total of "
        f"{wm['total_pnl']} dollars. The best trade was {best['ticker']} at {best['pnl_r']} R. "
        f"The {trip['switch']} kill switch tripped once during the week, which stopped new entries for the "
        "rest of that day as designed. Rules were not followed on one answered day, which is worth a look "
        "before next week. Keep sizing unchanged and review the losing trades in the journal."
    )


class FakeMessages:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        (msg,) = kwargs["messages"]
        block = re.search(r"<facts>\n(.*)\n</facts>", str(msg["content"]), re.DOTALL)
        assert block is not None
        text = commentary_from(json.loads(block.group(1)))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=2500, output_tokens=300),
            stop_reason="end_turn",
        )


def keyed_core(core: Core) -> Core:
    return core.__class__(
        **{**core.__dict__, "env": core.env.model_copy(update={"anthropic_api_key": SecretStr("test-key")})}
    )


def test_5_the_weekly_report_goes_from_the_data_to_telegram_once(
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world.clock.set(SAT_0900)
    run_id = seed_week(world.factory, world.clock)
    core = keyed_core(world.core)
    calls: list[dict[str, Any]] = []
    built: list[dict[str, Any]] = []

    class FakeAnthropic:
        def __init__(self, **kw: Any) -> None:
            built.append({k: v for k, v in kw.items() if k != "api_key"})
            self.messages = FakeMessages(calls)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(trader.bootstrap, "build_core", lambda *a, **k: core)
    monkeypatch.setattr(rt, "claude_client_class", lambda: FakeAnthropic)

    first = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert first.exit_code == 0, first.output
    assert "weekly 2026-11-27: succeeded" in first.output
    assert len(calls) == 1 and len(built) == 1

    # the stored report
    with world.factory() as s:
        report = s.get(m.WeeklyReport, FRI)
        assert report is not None
        facts, commentary = report.facts, report.commentary
        assert (report.week_start, report.run_id, report.commentary_status) == (MON, run_id, "ok")
        assert report.cost_usd <= Decimal("0.05") and report.cost_usd > 0
        job = s.execute(select(m.JobRun).where(m.JobRun.job == "weekly")).scalars().one()
        assert (job.session_date, job.status) == (FRI, "succeeded")
    assert commentary == commentary_from(facts)
    assert check_numbers(commentary, facts) == []

    # the facts' week numbers are compute_metrics for the week (not the run to date)
    week = compute_metrics(world.factory, run_id, MON, FRI)
    wm = facts["week_metrics"]
    assert wm["trades"] == week.trades == 4 and wm["wins"] == week.wins == 2
    assert Decimal(wm["total_pnl"]) == week.total_pnl == Decimal("7.2000")
    assert Decimal(wm["expectancy_r"]) == week.expectancy_r
    assert Decimal(wm["win_rate"]) == week.win_rate
    assert Decimal(wm["adherence_pct"]) == week.adherence_pct
    assert facts["run_to_date"]["trades"] == 5
    assert [t["switch"] for t in facts["kill_switch_trips"]] == ["daily_loss_pct"]

    # one Telegram message, whose numbers are the week's metrics, carrying the commentary
    sent = world.api.calls_of("send_message")
    assert len(sent) == 1
    expected = rt.build_renderer(core).weekly_report(
        WeeklyReportView(
            week_start=MON,
            week_ending=FRI,
            trades=week.trades,
            wins=week.wins,
            win_rate=week.win_rate,
            expectancy_r=week.expectancy_r,
            total_pnl=week.total_pnl,
            max_drawdown_pct=week.max_drawdown_pct,
            adherence_pct=week.adherence_pct,
            commentary=commentary,
            commentary_note=None,
        )
    )
    assert sent[0]["text"] == expected.text
    assert "Weekly report" in sent[0]["text"] and "/reports?week=2026-11-27" in sent[0]["text"]
    with world.factory() as s:
        notes = (
            s.execute(select(m.Notification).where(m.Notification.kind == "weekly_report")).scalars().all()
        )
        assert [(n.dedupe_key, n.status) for n in notes] == [("weekly:2026-11-27", "sent")]

    # the web API returns it
    client = make_client(make_services(core), reports.router)
    got = client.get("/api/reports/weekly", params={"week": "2026-11-25"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["week_ending"] == "2026-11-27" and body["commentary"] == commentary
    assert body["commentary_status"] == "ok" and body["facts"] == facts
    assert Decimal(body["cost_usd"]) <= Decimal("0.05")

    # a second run is skipped; a forced run re-builds the report but sends nothing more
    second = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25"])
    assert second.exit_code == 0 and "weekly 2026-11-27: skipped" in second.output, second.output
    forced = CliRunner().invoke(app, ["weekly", "--date", "2026-11-25", "--force"])
    assert forced.exit_code == 0, forced.output
    assert len(world.api.calls_of("send_message")) == 1
    with world.factory() as s:
        count = (
            s.execute(select(m.Notification).where(m.Notification.kind == "weekly_report")).scalars().all()
        )
        assert len(count) == 1
