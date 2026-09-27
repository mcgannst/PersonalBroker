"""P3-T7: Telegram commands (/status, /positions, /pnl, /pending, /pause, /resume, /help).

Real DB with seeded rows; FakeRenderer, FakeIssuer and FakeMessenger stand in for the other Phase 3 tasks.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_strategy_config, add_symbol
from tests.fakes_telegram import FakeIssuer, FakeMessenger, FakeRenderer
from trader.adapters.questrade.auth import TokenHealth
from trader.adapters.questrade.models import QtQuote
from trader.adapters.telegram.commands import (
    CommandDeps,
    Commands,
    pnl_view,
    position_lines,
    status_view,
)
from trader.adapters.telegram.types import CommandHandler
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.killswitch import KillSwitches
from trader.engine.runs import get_live_run
from trader.engine.scheduler import DayPlan, PlannedEvent
from trader.market.calendar import SessionCalendar
from trader.market.clock import FixedClock
from trader.notify.types import PnlView, StatusView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

CAL = SessionCalendar()
DAY = date(2026, 10, 6)  # Tuesday
T10 = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 10:00 ET
SATURDAY = datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
CHAT = 4242


def _plan(day: date) -> DayPlan:
    if not CAL.is_session(day):
        return DayPlan(day, False, None, None, ())

    def ev(key: str, h: int, mi: int, s: int = 0, late: bool = False) -> PlannedEvent:
        return PlannedEvent(key, datetime(day.year, day.month, day.day, h, mi, s, tzinfo=UTC), ("x",), late)

    return DayPlan(
        day,
        True,
        CAL.session_open(day),
        CAL.session_close(day),
        (
            ev("orb_open", 13, 35, 5),
            ev("entry_cancel", 15, 30, late=True),
            ev("overlay_decision", 19, 30, late=True),
            ev("flatten", 19, 50, late=True),
        ),
    )


@dataclass
class Env:
    factory: sessionmaker[Session]
    clock: FixedClock
    run_id: int
    renderer: FakeRenderer
    issuer: FakeIssuer
    messenger: FakeMessenger
    killswitches: KillSwitches
    fired: set[str] = field(default_factory=set)
    health: TokenHealth | None = None
    quotes: dict[int, QtQuote] = field(default_factory=dict)
    quotes_raise: bool = False
    settings: RuntimeSettings = field(default_factory=RuntimeSettings)

    def deps(self, with_quotes: bool = True) -> CommandDeps:
        async def quotes(ids: Sequence[int]) -> Mapping[int, QtQuote]:
            if self.quotes_raise:
                raise RuntimeError("questrade down")
            return {i: q for i, q in self.quotes.items() if i in ids}

        def token_health() -> TokenHealth:
            assert self.health is not None
            return self.health

        return CommandDeps(
            factory=self.factory,
            clock=self.clock,
            calendar=CAL,
            settings=lambda: self.settings,
            killswitches=self.killswitches,
            run_id=self.run_id,
            chat_id=CHAT,
            plan=_plan,
            fired=lambda d: set(self.fired),
            token_health=token_health,
            quotes=quotes if with_quotes else None,
            messenger=self.messenger,
            issuer=self.issuer,
            render=self.renderer,
        )


@pytest.fixture
def env(db_factory: sessionmaker[Session]) -> Env:
    clock = FixedClock(T10)
    run = get_live_run(db_factory, clock, RuntimeSettings())
    e = Env(
        factory=db_factory,
        clock=clock,
        run_id=run.id,
        renderer=FakeRenderer(),
        issuer=FakeIssuer(),
        messenger=FakeMessenger(),
        killswitches=KillSwitches(db_factory, clock),
    )
    e.health = TokenHealth(
        seeded=True,
        expires_at=T10 + timedelta(minutes=20),
        last_refresh_at=T10 - timedelta(hours=1),
        last_error=None,
    )
    return e


# --- seeding --------------------------------------------------------------------------------------------


def quote(symbol_id: int, last: str) -> QtQuote:
    return QtQuote(
        symbol_id=symbol_id,
        symbol="X",
        bid=None,
        ask=None,
        last=Decimal(last),
        last_regular=Decimal(last),
        volume=1000,
        last_trade_time=T10,
        delay=0,
        is_halted=False,
        vwap=None,
    )


def add_position(
    env: Env,
    ticker: str = "AAA",
    *,
    qty: int = 10,
    avg: str = "20.00",
    stop_loss: str = "19.50",
    stop_order: str | None = "19.50",
    unprotected_since: datetime | None = None,
    unprotected_seconds: int = 0,
) -> tuple[int, int]:
    """An open position (and optionally its working stop order). Returns (position id, symbol id)."""
    with session_scope(env.factory) as s:
        sym = add_symbol(s, ticker)
        entry = m.Order(
            run_id=env.run_id,
            symbol_id=sym,
            side="buy",
            order_type="stop",
            purpose="entry",
            qty=qty,
            stop_price=Decimal(avg),
            tif="day",
            status="filled",
            reason="orb_entry",
            session_date=DAY,
            submitted_at=T10 - timedelta(minutes=20),
            closed_at=T10 - timedelta(minutes=15),
            stale_alerted=False,
        )
        s.add(entry)
        s.flush()
        pos = m.Position(
            run_id=env.run_id,
            symbol_id=sym,
            qty=qty,
            avg_price=Decimal(avg),
            stop_loss=Decimal(stop_loss),
            session_date=DAY,
            opened_at=T10 - timedelta(minutes=15),
            entry_order_id=entry.id,
            unprotected_since=unprotected_since,
            unprotected_seconds=unprotected_seconds,
        )
        s.add(pos)
        s.flush()
        if stop_order is not None:
            stop = m.Order(
                run_id=env.run_id,
                position_id=pos.id,
                symbol_id=sym,
                side="sell",
                order_type="stop",
                purpose="stop",
                qty=qty,
                stop_price=Decimal(stop_order),
                tif="day",
                status="working",
                reason="protective_stop",
                session_date=DAY,
                submitted_at=T10 - timedelta(minutes=14),
                stale_alerted=False,
            )
            s.add(stop)
            s.flush()
            pos.stop_order_id = stop.id
        return pos.id, sym


def add_proposal(env: Env, *, status: str = "pending", created_at: datetime = T10) -> int:
    with session_scope(env.factory) as s:
        cfg = s.execute(select(m.StrategyConfig.id).limit(1)).scalar_one_or_none()
        if cfg is None:
            cfg = add_strategy_config(s)
        sig = m.Signal(
            run_id=env.run_id,
            strategy_config_id=cfg,
            symbol_id=None,
            session_date=DAY,
            event_key="orb_open",
            ts=created_at,
            intent={},
            evidence={},
        )
        s.add(sig)
        s.flush()
        p = m.Proposal(
            run_id=env.run_id,
            signal_id=sig.id,
            kind="entry",
            order_spec={},
            qty=10,
            status=status,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=5),
            escalations=0,
        )
        s.add(p)
        s.flush()
        return p.id


def add_trade(env: Env, session_date: date, pnl: str, ticker: str = "TTT") -> None:
    with session_scope(env.factory) as s:
        sym = add_symbol(s, f"{ticker}{session_date.day}{pnl.replace('-', 'm').replace('.', '')}")
        closed = datetime(session_date.year, session_date.month, session_date.day, 19, 0, tzinfo=UTC)
        pos = m.Position(
            run_id=env.run_id,
            symbol_id=sym,
            qty=0,
            avg_price=Decimal("10"),
            session_date=session_date,
            opened_at=closed - timedelta(hours=5),
            closed_at=closed,
            entry_order_id=0,
            unprotected_seconds=0,
        )
        s.add(pos)
        s.flush()
        s.add(
            m.Trade(
                run_id=env.run_id,
                position_id=pos.id,
                symbol_id=sym,
                session_date=session_date,
                entry_price=Decimal("10"),
                exit_price=Decimal("11"),
                qty=10,
                pnl=Decimal(pnl),
                pnl_r=None,
                exit_reason="flatten_close",
                slippage_total=Decimal(0),
                fees_total=Decimal(0),
                opened_at=closed - timedelta(hours=5),
                closed_at=closed,
            )
        )


def add_heartbeat(env: Env, beat_at: datetime) -> None:
    with session_scope(env.factory) as s:
        s.add(
            m.WorkerHeartbeat(
                process="worker",
                pid=1,
                host="h",
                started_at=beat_at - timedelta(hours=1),
                beat_at=beat_at,
                session_date=DAY,
                phase="session",
            )
        )


def add_snapshot(env: Env, ts: datetime, equity: str, peak: str, dd: str) -> None:
    with session_scope(env.factory) as s:
        s.add(
            m.EquitySnapshot(
                run_id=env.run_id,
                ts=ts,
                equity=Decimal(equity),
                cash=Decimal(equity),
                settled_cash=Decimal(equity),
                peak_equity=Decimal(peak),
                drawdown_pct=Decimal(dd),
            )
        )


# --- 1-3: status ----------------------------------------------------------------------------------------


async def test_status_view_during_the_session(env: Env) -> None:
    env.fired = {"orb_open"}
    add_heartbeat(env, T10 - timedelta(seconds=5))
    add_position(env)
    add_proposal(env)
    v = await status_view(env.deps())
    assert isinstance(v, StatusView)
    assert v.now == T10 and v.phase == "open" and v.session_date == DAY
    assert v.next_event_key == "entry_cancel"
    assert v.next_event_at == datetime(2026, 10, 6, 15, 30, tzinfo=UTC)
    assert v.approval_mode == "manual"
    assert v.blocking_switches == ()
    assert v.token_ok is True and v.token_error is None
    assert v.token_age_hours == pytest.approx(1.0)
    assert v.heartbeat_age_seconds == pytest.approx(5.0)
    assert len(v.positions) == 1 and v.positions[0].ticker == "AAA"
    assert v.pending_count == 1


async def test_status_view_on_saturday(env: Env) -> None:
    env.clock.set(SATURDAY)
    v = await status_view(env.deps())
    assert v.phase == "closed_day"
    assert v.session_date == date(2026, 10, 5)
    assert v.next_event_key is None and v.next_event_at is None
    assert v.heartbeat_age_seconds is None  # no worker heartbeat row


async def test_status_view_after_close_has_no_next_event(env: Env) -> None:
    env.clock.set(datetime(2026, 10, 6, 21, 0, tzinfo=UTC))
    v = await status_view(env.deps())
    assert v.phase == "after_close" and v.next_event_key is None


async def test_status_view_shows_blocking_switches(env: Env) -> None:
    env.killswitches.pause(env.run_id, DAY, actor="test")
    v = await status_view(env.deps())
    assert v.blocking_switches == ("manual_pause",)


async def test_status_token_too_old_is_not_ok(env: Env) -> None:
    env.health = TokenHealth(
        seeded=True, expires_at=None, last_refresh_at=T10 - timedelta(hours=30), last_error=None
    )
    v = await status_view(env.deps())
    assert v.token_ok is False
    assert v.token_age_hours == pytest.approx(30.0)
    assert v.token_error is not None and "30" in v.token_error


async def test_status_token_with_last_error_is_not_ok(env: Env) -> None:
    env.health = TokenHealth(
        seeded=True,
        expires_at=None,
        last_refresh_at=T10 - timedelta(hours=2),
        last_error="refresh token already used or expired",
    )
    v = await status_view(env.deps())
    assert v.token_ok is False
    assert v.token_error == "refresh token already used or expired"


async def test_status_token_health_raising_is_not_ok_and_does_not_fail(env: Env) -> None:
    env.health = None  # token_health() raises AssertionError
    v = await status_view(env.deps())
    assert v.token_ok is False and v.token_error is not None


# --- 4: positions ---------------------------------------------------------------------------------------


async def test_position_lines_with_a_working_stop(env: Env) -> None:
    _, sym = add_position(env, avg="20.00", stop_loss="19.40", stop_order="19.50", qty=10)
    env.quotes = {sym: quote(sym, "20.75")}
    (line,) = await position_lines(env.deps())
    assert line.ticker == "AAA" and line.qty == 10 and line.entry == Decimal("20.00")
    assert line.stop == Decimal("19.50") and line.stop_working is True
    assert line.last == Decimal("20.75") and line.unrealized_pnl == Decimal("7.50")
    assert line.unprotected_seconds == 0


async def test_position_lines_without_a_stop_order_grow_unprotected_time(env: Env) -> None:
    add_position(
        env,
        stop_loss="19.40",
        stop_order=None,
        unprotected_since=T10 - timedelta(seconds=30),
        unprotected_seconds=12,
    )
    (line,) = await position_lines(env.deps())
    assert line.stop == Decimal("19.40") and line.stop_working is False
    assert line.unprotected_seconds == 42
    env.clock.advance(timedelta(seconds=60))
    (line,) = await position_lines(env.deps())
    assert line.unprotected_seconds == 102


async def test_position_lines_quote_failure_gives_no_last(env: Env) -> None:
    add_position(env)
    env.quotes_raise = True
    (line,) = await position_lines(env.deps())
    assert line.last is None and line.unrealized_pnl is None
    (line,) = await position_lines(env.deps(with_quotes=False))
    assert line.last is None


async def test_position_lines_skip_closed_positions_and_other_runs(env: Env) -> None:
    add_trade(env, DAY, "5")  # a closed position
    assert await position_lines(env.deps()) == ()


# --- 5: pnl ---------------------------------------------------------------------------------------------


async def test_pnl_view_sums_today_and_the_week(env: Env) -> None:
    add_trade(env, DAY, "10.50")
    add_trade(env, DAY, "-3.25")
    add_trade(env, date(2026, 10, 5), "4.00")  # Monday of this week
    add_trade(env, date(2026, 10, 2), "100.00")  # last Friday: excluded
    add_snapshot(env, T10 - timedelta(days=1), "700", "760", "0.0789")
    add_snapshot(env, T10 - timedelta(minutes=5), "740", "760", "0.0263")
    _, sym = add_position(env, avg="20.00", qty=10)
    env.quotes = {sym: quote(sym, "21.00")}
    v = await pnl_view(env.deps())
    assert isinstance(v, PnlView)
    assert v.session_date == DAY
    assert v.realized_today == Decimal("7.25")
    assert v.week_to_date == Decimal("11.25")
    assert v.unrealized == Decimal("10.00")
    assert v.equity == Decimal("740") and v.peak_equity == Decimal("760")
    assert v.drawdown_pct == Decimal("0.0263")


async def test_pnl_view_without_snapshots_uses_starting_cash(env: Env) -> None:
    v = await pnl_view(env.deps())
    with env.factory() as s:
        starting = s.execute(select(m.SimAccount.starting_cash)).scalar_one()
    assert v.realized_today == Decimal(0) and v.week_to_date == Decimal(0) and v.unrealized == Decimal(0)
    assert v.equity == v.peak_equity == starting and starting > 0
    assert v.drawdown_pct == Decimal(0)


# --- 6: /pending ----------------------------------------------------------------------------------------


async def test_pending_resends_oldest_first(env: Env) -> None:
    newer = add_proposal(env, created_at=T10)
    older = add_proposal(env, created_at=T10 - timedelta(minutes=2))
    add_proposal(env, status="submitted", created_at=T10 - timedelta(minutes=3))
    out = await Commands(env.deps()).handle("/pending")
    assert env.messenger.calls == [(older, True), (newer, True)]
    assert out == []


async def test_pending_with_none_replies(env: Env) -> None:
    out = await Commands(env.deps()).handle("/pending")
    assert env.messenger.calls == []
    assert len(out) == 1 and ("reply", ("No pending proposals.",)) in env.renderer.calls


# --- 7: /pause ------------------------------------------------------------------------------------------


async def test_pause_asks_then_confirm_yes_blocks_entries(env: Env) -> None:
    cmds = Commands(env.deps())
    out = await cmds.handle("/pause")
    assert len(out) == 1
    (issued,) = env.issuer.issued
    assert issued["kind"] == "pause" and issued["ref"] == str(env.run_id)
    assert issued["actions"] == ["y", "n"] and issued["chat_id"] == CHAT
    assert issued["ttl_seconds"] == RuntimeSettings().telegram_confirm_ttl_seconds
    datas = {b.callback_data for row in out[0].buttons for b in row}
    assert datas == {issued["data"]["y"], issued["data"]["n"]}
    assert env.renderer.calls[-1][0] == "pause_confirm"
    assert env.killswitches.blocking(env.run_id, DAY) is None  # not before the confirmation

    reply = await cmds.confirm_pause("y")
    assert reply == "Paused: new entries are blocked."
    assert env.killswitches.blocking(env.run_id, DAY) == "manual_pause"
    with env.factory() as s:
        audit = s.execute(select(m.AuditLog).where(m.AuditLog.action == "killswitch.pause")).scalar_one()
    assert audit.actor == "telegram"


async def test_pause_confirm_no_changes_nothing(env: Env) -> None:
    cmds = Commands(env.deps())
    await cmds.handle("/pause")
    assert await cmds.confirm_pause("n") == "Cancelled."
    assert env.killswitches.blocking(env.run_id, DAY) is None
    with env.factory() as s:
        assert s.execute(select(m.AuditLog)).scalars().all() == []


async def test_pause_when_already_paused(env: Env) -> None:
    env.killswitches.pause(env.run_id, DAY, actor="web")
    out = await Commands(env.deps()).handle("/pause")
    assert len(out) == 1 and env.issuer.issued == []
    assert ("reply", ("Already paused.",)) in env.renderer.calls


# --- 8: /resume -----------------------------------------------------------------------------------------


async def test_resume_lifts_manual_pause_only(env: Env) -> None:
    env.killswitches.pause(env.run_id, DAY, actor="test")
    with session_scope(env.factory) as s:
        s.add(
            m.KillSwitchEvent(
                run_id=env.run_id,
                switch="max_drawdown_pct",
                session_date=DAY,
                tripped_at=T10,
                value=Decimal("0.2"),
                threshold=Decimal("0.15"),
            )
        )
    out = await Commands(env.deps()).handle("/resume")
    assert len(out) == 1
    method, args = env.renderer.calls[-1]
    assert method == "reply"
    assert args[0].startswith("Manual pause lifted.")
    assert "Still blocked by: max_drawdown_pct. Reset them in the web app." in args[0]
    assert env.killswitches.blocking(env.run_id, DAY) == "max_drawdown_pct"
    active = {a.switch for a in env.killswitches.active(env.run_id, DAY)}
    assert active == {"max_drawdown_pct"}


async def test_resume_plain(env: Env) -> None:
    env.killswitches.pause(env.run_id, DAY, actor="test")
    await Commands(env.deps()).handle("/resume")
    assert env.renderer.calls[-1] == ("reply", ("Manual pause lifted.",))
    assert env.killswitches.blocking(env.run_id, DAY) is None


async def test_resume_when_not_paused(env: Env) -> None:
    await Commands(env.deps()).handle("/resume")
    assert env.renderer.calls[-1] == ("reply", ("Not paused.",))


# --- 9: parsing -----------------------------------------------------------------------------------------


async def test_command_with_bot_suffix_and_case(env: Env) -> None:
    out = await Commands(env.deps()).handle("/Status@StephenTraderDevBot")
    assert len(out) == 1 and env.renderer.calls[-1][0] == "status"
    assert isinstance(env.renderer.calls[-1][1][0], StatusView)


async def test_unknown_command_and_plain_text(env: Env) -> None:
    cmds = Commands(env.deps())
    for text in ("/nonsense", "hello there", "", "   "):
        out = await cmds.handle(text)
        assert len(out) == 1
        assert env.renderer.calls[-1] == ("reply", ("Unknown command. Try /help.",))


async def test_help_positions_and_pnl_render(env: Env) -> None:
    cmds = Commands(env.deps())
    assert len(await cmds.handle("/help")) == 1
    await cmds.handle("/positions extra words")
    await cmds.handle("/pnl")
    methods = [c[0] for c in env.renderer.calls]
    assert methods == ["help", "positions", "pnl"]
    assert env.renderer.calls[0] == ("help", ())
    assert env.renderer.calls[1] == ("positions", ((), T10))


def test_commands_satisfy_the_protocol(env: Env) -> None:
    handler: CommandHandler = Commands(env.deps())
    assert handler is not None
