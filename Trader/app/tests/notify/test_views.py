"""P3-T12: the shared view builders. The bot and the relay build the same ProposalView (the actual dollar
risk), and /status and the check-ins build the same StatusView."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

import trader.adapters.telegram.bot as bot_module
import trader.notify.relay as relay_module
from tests.factories import add_strategy_config, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeMessenger, FakeRenderer, RecordingNotifier
from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.commands import CommandDeps, status_view
from trader.db import models as m
from trader.engine.killswitch import KillSwitches
from trader.engine.runs import get_live_run
from trader.engine.scheduler import DayPlan, FireResult, PlannedEvent
from trader.jobs.checkin import CheckinDeps, run_checkin
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify import views
from trader.notify.types import StatusView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db
CAL = SessionCalendar()
DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 15, 30, tzinfo=UTC)  # 11:30 ET


def test_the_bot_and_the_relay_use_the_one_shared_builder() -> None:
    assert bot_module.proposal_view is views.proposal_view
    assert relay_module.proposal_view is views.proposal_view


def _proposal(
    s: Session, run_id: int, *, sizing: dict[str, str] | None, kind: str = "entry", qty: int = 33
) -> m.Proposal:
    sym = add_symbol(s, f"S{kind}{qty}{len(str(sizing))}")
    cfg = add_strategy_config(s, f"k{kind}{qty}{len(str(sizing))}")
    sig = m.Signal(
        run_id=run_id,
        strategy_config_id=cfg,
        symbol_id=sym,
        session_date=DAY,
        event_key="orb_open",
        ts=NOW,
        intent={"reason": "from intent"},
        evidence={},
    )
    s.add(sig)
    s.flush()
    p = m.Proposal(
        run_id=run_id,
        signal_id=sig.id,
        kind=kind,
        order_spec={
            "symbol_id": sym,
            "side": "buy",
            "order_type": "stop",
            "stop": "21.55",
            "stop_loss": "21.41",
        },
        qty=qty,
        status="pending",
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=90),
        sizing=sizing,
        escalations=0,
    )
    s.add(p)
    s.flush()
    return p


def test_risk_is_quantity_times_per_share_risk(db_factory: sessionmaker[Session]) -> None:
    run = get_live_run(db_factory, FixedClock(NOW), RuntimeSettings())
    with db_factory() as s:
        sized = _proposal(s, run.id, sizing={"per_share_risk": "0.10", "risk_dollars": "5.00"})
        derived = _proposal(s, run.id, sizing={"risk_dollars": "5.00"}, qty=10)
        stop = _proposal(s, run.id, sizing=None, kind="stop")
        a, b, c = views.proposal_view(s, sized), views.proposal_view(s, derived), views.proposal_view(s, stop)
    assert a.risk_usd == Decimal("3.30")  # 33 x 0.10, not the 5.00 budget
    assert b.risk_usd == Decimal("1.40")  # 10 x (21.55 - 21.41)
    assert c.risk_usd is None  # only entries carry a risk
    assert a.reason == "from intent" and a.ticker.startswith("S") and a.stop == Decimal("21.55")


class _Quotes:
    async def __call__(self, ids: Sequence[int]) -> Mapping[int, QtQuote]:
        return {
            i: QtQuote(i, "AAA", None, None, Decimal("21.00"), None, 0, None, 0, False, None) for i in ids
        }


async def test_status_and_checkin_build_the_same_view(db_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(NOW)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    with db_factory() as s:
        sym = add_symbol(s, "AAA")
        s.add(
            m.Position(
                run_id=run.id,
                symbol_id=sym,
                qty=10,
                avg_price=Decimal("20.00"),
                stop_loss=Decimal("19.50"),
                session_date=DAY,
                opened_at=NOW - timedelta(hours=1),
                entry_order_id=1,
                unprotected_since=NOW - timedelta(seconds=40),
                unprotected_seconds=5,
            )
        )
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=NOW - timedelta(hours=1),
                beat_at=NOW - timedelta(seconds=7),
                session_date=DAY,
                phase="session",
            )
        )
        s.commit()
    plan = DayPlan(
        DAY,
        True,
        None,
        None,
        (
            PlannedEvent("orb_open", NOW - timedelta(hours=2), ("orb_sip",), False),
            PlannedEvent("entry_cancel", NOW, ("orb_sip",), True),
        ),
    )
    health = TokenHealth(True, None, NOW - timedelta(hours=3), None)
    switches = KillSwitches(db_factory, clock)
    switches.pause(run.id, DAY, actor="telegram")
    quotes = _Quotes()
    command_deps = CommandDeps(
        factory=db_factory,
        clock=clock,
        calendar=CAL,
        settings=RuntimeSettings,
        killswitches=switches,
        run_id=run.id,
        chat_id=1,
        plan=lambda d: plan,
        fired=lambda d: {"orb_open"},
        token_health=lambda: health,
        quotes=quotes,
        messenger=FakeMessenger(),
        issuer=FakeIssuer(),
        render=FakeRenderer(),
    )
    from_status = await status_view(command_deps)

    render = FakeRenderer()

    async def fire(key: str, d: date) -> FireResult:
        return FireResult(key, d, "skipped", {})

    await run_checkin(
        CheckinDeps(
            factory=db_factory,
            clock=clock,
            calendar=CAL,
            settings=RuntimeSettings,
            run_id=run.id,
            notifier=RecordingNotifier(),
            render=render,
            plan=lambda d: plan,
            fired=lambda d: {"orb_open"},
            fire=fire,
            quotes=quotes,
            token_health=lambda: health,
            killswitches=switches,
        ),
        DAY,
        "11:30",
    )
    [(name, args)] = render.calls
    from_checkin = args[0]
    assert name == "checkin" and isinstance(from_checkin, StatusView)
    assert from_checkin == from_status
    assert from_status.next_event_key == "entry_cancel" and from_status.blocking_switches == ("manual_pause",)
    assert from_status.heartbeat_age_seconds == pytest.approx(7.0)
    [line] = from_status.positions
    assert line.unrealized_pnl == Decimal("10.0000") and line.unprotected_seconds == 45


@pytest.mark.parametrize(("phase", "running"), [("session", True), ("stopping", False), ("stopped", False)])
def test_a_stopping_worker_has_no_heartbeat_age_like_the_preopen_check(
    db_factory: sessionmaker[Session], phase: str, running: bool
) -> None:
    """Fix round 1: `stopping` is a worker going away, as in the pre-open check (one shared rule)."""
    from trader.jobs import preopen

    assert preopen.STOPPED_PHASES is views.STOPPED_PHASES and preopen.WORKER_PROCESS == views.WORKER_PROCESS
    with db_factory() as s:
        s.add(
            m.WorkerHeartbeat(
                process=views.WORKER_PROCESS,
                pid=1,
                host="h",
                started_at=NOW - timedelta(hours=1),
                beat_at=NOW - timedelta(seconds=3),
                phase=phase,
            )
        )
        s.commit()
    age = views.heartbeat_age(db_factory, NOW)
    assert (age == pytest.approx(3.0)) if running else (age is None)
