"""P3-T8: the notification relay (DB rows to Telegram), against a real database with the Phase 3 fakes."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import add_run, add_strategy_config, add_symbol
from tests.fakes_telegram import FakeMessenger, FakeRenderer, FakeTelegramApi, RecordingNotifier
from trader.adapters.telegram.api import TelegramNotSentError
from trader.adapters.telegram.types import TelegramApiError
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import FixedClock
from trader.notify.notifier import TelegramNotifier
from trader.notify.relay import STREAMS, NotificationRelay, RelayReport, alert_kind
from trader.notify.types import AlertView, FillView, OutboundMessage, OverlayView, ProposalView
from trader.settings_store import RuntimeSettings

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=UTC)  # 10:00 ET
SESSION = date(2026, 10, 6)


@dataclass
class World:
    factory: sessionmaker[Session]
    clock: FixedClock
    run_id: int
    symbol_id: int
    config_id: int
    notifier: RecordingNotifier
    render: FakeRenderer
    messenger: FakeMessenger
    settings: RuntimeSettings

    def relay(self, notifier: Any = None) -> NotificationRelay:
        return NotificationRelay(
            self.factory,
            self.clock,
            notifier if notifier is not None else self.notifier,
            self.render,
            self.messenger,
            self.run_id,
            settings=lambda: self.settings,
        )


@pytest.fixture
def world(db_factory: sessionmaker[Session]) -> World:
    with session_scope(db_factory) as s:
        run_id = add_run(s)
        symbol_id = add_symbol(s, "AAA")
        config_id = add_strategy_config(s, "orb_sip")
    return World(
        db_factory,
        FixedClock(NOW),
        run_id,
        symbol_id,
        config_id,
        RecordingNotifier(),
        FakeRenderer(),
        FakeMessenger(),
        RuntimeSettings(),
    )


# --- seeding --------------------------------------------------------------------------------------------
def add_proposal(
    w: World,
    *,
    kind: str = "entry",
    status: str = "pending",
    decided_via: str | None = None,
    decided_by: str | None = None,
    position_id: int | None = None,
) -> int:
    with session_scope(w.factory) as s:
        sig = m.Signal(
            run_id=w.run_id,
            strategy_config_id=w.config_id,
            symbol_id=w.symbol_id,
            session_date=SESSION,
            event_key="orb_open",
            ts=NOW,
            intent={"type": "enter_long", "reason": "orb breakout"},
            evidence={},
        )
        s.add(sig)
        s.flush()
        spec = {
            "symbol_id": w.symbol_id,
            "side": "buy" if kind == "entry" else "sell",
            "order_type": "stop" if kind in ("entry", "stop") else "market",
            "qty": 33,
            "stop": "21.55" if kind in ("entry", "stop") else None,
            "limit": None,
            "tif": "day",
            "purpose": kind,
            "position_id": position_id,
            "proposal_id": None,
            "strategy_config_id": w.config_id,
            "stop_loss": "21.41",
            "reason": "orb breakout" if kind == "entry" else "flatten_close",
        }
        p = m.Proposal(
            run_id=w.run_id,
            signal_id=sig.id,
            kind=kind,
            order_spec=spec,
            qty=33,
            status=status,
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=90),
            decided_via=decided_via,
            decided_by=decided_by,
            decided_at=NOW if decided_by else None,
            position_id=position_id,
            sizing={"risk_dollars": "5.00"} if kind == "entry" else None,
            escalations=0,
        )
        s.add(p)
        s.flush()
        return p.id


def add_order(
    s: Session,
    w: World,
    *,
    purpose: str,
    reason: str,
    position_id: int | None,
    order_type: str = "market",
) -> int:
    o = m.Order(
        run_id=w.run_id,
        symbol_id=w.symbol_id,
        strategy_config_id=w.config_id,
        position_id=position_id,
        side="buy" if purpose == "entry" else "sell",
        order_type=order_type,
        purpose=purpose,
        qty=33,
        stop_price=Decimal("21.41") if purpose == "stop" else None,
        stop_loss=Decimal("21.41"),
        tif="day",
        status="filled",
        reason=reason,
        session_date=SESSION,
        submitted_at=NOW,
        closed_at=NOW,
        stale_alerted=False,
    )
    s.add(o)
    s.flush()
    return o.id


def add_fill(s: Session, w: World, order_id: int, price: str) -> int:
    f = m.Fill(
        run_id=w.run_id,
        order_id=order_id,
        ts=NOW,
        qty=33,
        price=Decimal(price),
        fees={"commission": "0", "ecn": "0", "sec": "0", "total": "0"},
        quote_snapshot={},
        slippage=Decimal("0"),
    )
    s.add(f)
    s.flush()
    return f.id


def add_position(s: Session, w: World, entry_order_id: int, *, closed: bool) -> int:
    pos = m.Position(
        run_id=w.run_id,
        symbol_id=w.symbol_id,
        strategy_config_id=w.config_id,
        qty=0 if closed else 33,
        avg_price=Decimal("21.5608"),
        stop_loss=Decimal("21.41"),
        planned_risk=Decimal("4.97"),
        session_date=SESSION,
        opened_at=NOW,
        closed_at=NOW if closed else None,
        entry_order_id=entry_order_id,
        unprotected_seconds=0,
    )
    s.add(pos)
    s.flush()
    return pos.id


def add_trade(s: Session, w: World, position_id: int, exit_price: str, pnl: str, pnl_r: str) -> None:
    s.add(
        m.Trade(
            run_id=w.run_id,
            position_id=position_id,
            symbol_id=w.symbol_id,
            session_date=SESSION,
            entry_price=Decimal("21.5608"),
            exit_price=Decimal(exit_price),
            qty=33,
            pnl=Decimal(pnl),
            pnl_r=Decimal(pnl_r),
            planned_risk=Decimal("4.97"),
            exit_reason="x",
            slippage_total=Decimal("0"),
            fees_total=Decimal("0"),
            opened_at=NOW,
            closed_at=NOW,
        )
    )
    s.flush()


def add_round_trip(w: World, *, exit_purpose: str, exit_reason: str, exit_price: str) -> tuple[int, int, int]:
    """An entry fill and a closing fill with its trade. Returns (entry fill, exit fill, position)."""
    with session_scope(w.factory) as s:
        entry_order = add_order(
            s, w, purpose="entry", reason="orb breakout", position_id=None, order_type="stop"
        )
        pos_id = add_position(s, w, entry_order, closed=True)
        s.get_one(m.Order, entry_order).position_id = pos_id
        entry_fill = add_fill(s, w, entry_order, "21.5608")
        exit_order = add_order(s, w, purpose=exit_purpose, reason=exit_reason, position_id=pos_id)
        exit_fill = add_fill(s, w, exit_order, exit_price)
        add_trade(s, w, pos_id, exit_price, "10.8200", "2.1700")
    return entry_fill, exit_fill, pos_id


def add_event(
    w: World,
    level: str,
    source: str,
    message: str = "something happened",
    data: dict[str, Any] | None = None,
    run_id: int | None | str = "run",
) -> int:
    with session_scope(w.factory) as s:
        e = m.EventLog(
            ts=NOW,
            level=level,
            source=source,
            run_id=w.run_id if run_id == "run" else run_id,
            message=message,
            data=data,
        )
        s.add(e)
        s.flush()
        return e.id


def rendered(w: World, method: str) -> list[Any]:
    return [args[0] for name, args in w.render.calls if name == method]


def cursor(w: World, stream: str) -> int | None:
    with session_scope(w.factory) as s:
        c = s.get(m.NotifyCursor, stream)
        return None if c is None else c.last_id


async def started(w: World) -> NotificationRelay:
    """A relay that has pumped once on an empty database, so its cursors exist at the current maximum."""
    relay = w.relay()
    await relay.pump()
    w.render.calls.clear()
    w.notifier.sent.clear()
    w.messenger.sent.clear()
    return relay


# --- alert_kind -------------------------------------------------------------------------------------------
def test_alert_kind_mapping() -> None:
    assert alert_kind("killswitch", "error", "max_drawdown_pct tripped") == "kill_switch"
    assert alert_kind("job.nightly", "error", "boom") == "job_failure"
    assert alert_kind("job.event:orb_open", "error", "missed: 295s late") == "job_failure"
    assert alert_kind("questrade.token", "error", "refresh failed") == "token_failure"
    assert alert_kind("proposals", "error", "position 3 is still unprotected") == "escalation"
    assert alert_kind("engine", "critical", "position still open at the close") == "escalation"
    assert alert_kind("engine", "error", "strategy failed") == "alert"
    assert alert_kind("broker", "error", "anything") == "alert"
    assert alert_kind("jobs", "error", "not a job source") == "alert"


# --- 1. proposals -----------------------------------------------------------------------------------------
async def test_pending_proposal_goes_to_messenger_and_auto_one_is_relayed_silently(world: World) -> None:
    relay = await started(world)
    pending = add_proposal(world)
    auto = add_proposal(world, status="submitted", decided_via="auto", decided_by="auto")
    flatten = add_proposal(
        world, kind="exit", status="submitted", decided_via="auto", decided_by="auto_flatten_on_expiry"
    )

    report = await relay.pump()

    assert world.messenger.sent == [pending]
    assert report.proposals == 2  # one messenger send + one auto message
    views = rendered(world, "proposal")
    assert [v.proposal_id for v in views] == [auto]
    view = views[0]
    assert isinstance(view, ProposalView)
    assert (view.ticker, view.kind, view.side, view.qty) == ("AAA", "entry", "buy", 33)
    assert view.stop == Decimal("21.55") and view.stop_loss == Decimal("21.41")
    # P3-T12: the shared view builder shows the actual risk, qty x (entry - stop loss) = 33 x 0.14
    assert view.risk_usd == Decimal("4.62") and view.strategy_key == "orb_sip"
    assert view.decided_via == "auto" and view.reason == "orb breakout"
    [msg] = world.notifier.sent
    assert msg.silent is True and msg.buttons == () and msg.dedupe_key == f"proposal:{auto}"
    assert all(args[1] == () for name, args in world.render.calls if name == "proposal")
    assert flatten not in [v.proposal_id for v in views]
    assert cursor(world, "proposals") == flatten


# --- 2. fills ---------------------------------------------------------------------------------------------
async def test_entry_stop_and_flatten_fills_each_produce_one_message(world: World) -> None:
    relay = await started(world)
    entry_fill, stop_fill, pos = add_round_trip(
        world, exit_purpose="stop", exit_reason="stop_loss", exit_price="21.40"
    )
    _, flatten_fill, pos2 = add_round_trip(
        world, exit_purpose="exit", exit_reason="flatten_close", exit_price="21.89"
    )

    report = await relay.pump()

    views: list[FillView] = rendered(world, "fill")
    assert report.fills == 4
    by_id = {v.fill_id: v for v in views}
    assert len(by_id) == 4
    e = by_id[entry_fill]
    assert (e.purpose, e.side, e.ticker, e.qty, e.price) == ("entry", "buy", "AAA", 33, Decimal("21.5608"))
    assert e.reason == "orb breakout" and e.position_id == pos and e.stop_loss == Decimal("21.41")
    assert e.pnl is None and e.pnl_r is None
    st = by_id[stop_fill]
    assert (st.purpose, st.reason, st.position_id) == ("stop", "stop_loss", pos)
    assert st.pnl == Decimal("10.8200") and st.pnl_r == Decimal("2.1700")
    fl = by_id[flatten_fill]
    assert (fl.purpose, fl.reason, fl.position_id) == ("exit", "flatten_close", pos2)
    assert fl.pnl == Decimal("10.8200")
    keys = [msg.dedupe_key for msg in world.notifier.sent]
    assert sorted(keys) == sorted(f"fill:{v.fill_id}" for v in views)
    assert [v.fill_id for v in views] == sorted(v.fill_id for v in views)  # id order
    assert cursor(world, "fills") == max(by_id)


# --- 3. alerts --------------------------------------------------------------------------------------------
async def test_error_events_become_alerts_by_kind(world: World) -> None:
    relay = await started(world)
    add_event(world, "error", "killswitch", "max_drawdown_pct tripped", {"switch": "max_drawdown_pct"})
    add_event(world, "error", "job.nightly", "job nightly failed", run_id=None)
    add_event(world, "error", "proposals", "position 3 is still unprotected (alert 2)")
    add_event(world, "info", "engine", "proposal 1 approved")
    add_event(world, "warning", "telegram", "telegram send failed")
    add_event(world, "error", "telegram", "telegram broke")

    report = await relay.pump()

    views: list[AlertView] = rendered(world, "alert")
    assert [v.kind for v in views] == ["kill_switch", "job_failure", "escalation"]
    assert views[0].data == {"switch": "max_drawdown_pct"} and views[0].source == "killswitch"
    assert views[1].data == {}
    assert report.events == 3
    assert [msg.kind for msg in world.notifier.sent] == ["kill_switch", "job_failure", "escalation"]
    assert all(msg.dedupe_key and msg.dedupe_key.startswith("event:") for msg in world.notifier.sent)


async def test_events_of_another_run_are_not_relayed(world: World) -> None:
    relay = await started(world)
    with session_scope(world.factory) as s:
        other = add_run(s, mode="replay")
    add_event(world, "error", "engine", "replay went wrong", run_id=other)
    await relay.pump()
    assert world.notifier.sent == []


# --- 4. overlay -------------------------------------------------------------------------------------------
async def test_overlay_decision_notes_render_as_overlay_messages(world: World) -> None:
    relay = await started(world)
    add_event(
        world,
        "info",
        "strategy.spy_overlay",
        "overlay: decision",
        {
            "decision": "hold",
            "spy_return": "0.001234",
            "prior_close": "570.10",
            "price": "570.80",
            "position_ids": [4, 5],
        },
    )
    add_event(
        world,
        "error",
        "strategy.spy_overlay",
        "overlay: benchmark unknown, holding",
        {"decision": "hold", "benchmark": "SPY", "position_ids": []},
    )
    add_event(world, "info", "strategy.spy_overlay", "overlay: something else", {"note": 1})

    await relay.pump()

    views: list[OverlayView] = rendered(world, "overlay")
    assert rendered(world, "alert") == []
    assert len(views) == 2
    hold, unknown = views
    assert hold.decision == "hold" and hold.spy_return == Decimal("0.001234")
    assert hold.prior_close == Decimal("570.10") and hold.price == Decimal("570.80")
    assert hold.position_ids == (4, 5) and hold.note == "overlay: decision"
    assert unknown.spy_return is None and unknown.prior_close is None and unknown.price is None
    assert unknown.note == "overlay: benchmark unknown, holding"
    assert [msg.kind for msg in world.notifier.sent] == ["overlay", "overlay"]


# --- 5. cursors start at the current maximum --------------------------------------------------------------
async def test_missing_cursor_starts_at_current_maximum(world: World) -> None:
    add_round_trip(world, exit_purpose="stop", exit_reason="stop_loss", exit_price="21.40")
    add_event(world, "error", "engine", "old error")
    add_proposal(world, status="submitted", decided_via="auto", decided_by="auto")
    relay = world.relay()

    await relay.pump()
    assert world.notifier.sent == []
    assert all(cursor(world, s) is not None for s in STREAMS)

    new_event = add_event(world, "error", "engine", "new error")
    await relay.pump()
    assert [msg.dedupe_key for msg in world.notifier.sent] == [f"event:{new_event}"]


# --- 6. exactly once --------------------------------------------------------------------------------------
async def test_pump_twice_and_two_instances_send_each_row_once(world: World) -> None:
    relay_a = await started(world)
    relay_b = world.relay()
    add_round_trip(world, exit_purpose="exit", exit_reason="flatten_close", exit_price="21.89")
    add_event(world, "critical", "engine", "position still open at the close")
    add_proposal(world, status="submitted", decided_via="auto", decided_by="auto")

    await relay_a.pump()
    await relay_a.pump()
    assert len(world.render.calls) == 4  # the same instance doesn't even render a row twice
    # a second instance (a restart) re-scans the last 2 minutes below the cursor for late commits: it
    # re-renders those rows, but the notifier's dedupe keys drop every re-send
    await relay_b.pump()

    keys = [msg.dedupe_key for msg in world.notifier.sent]
    assert len(keys) == 4 and len(set(keys)) == 4


async def test_two_instances_racing_over_the_same_rows_send_once(world: World) -> None:
    """Two relays that both read the same cursor (a crash between send and advance looks the same)."""
    await started(world)
    add_event(world, "error", "engine", "e1")
    add_event(world, "error", "engine", "e2")
    relay_a, relay_b = world.relay(), world.relay()
    await asyncio.gather(relay_a.pump(), relay_b.pump())
    keys = [msg.dedupe_key for msg in world.notifier.sent]
    assert len(keys) == 2 and len(set(keys)) == 2


# --- 7. a failing notifier --------------------------------------------------------------------------------
class FlakyNotifier(RecordingNotifier):
    """Raises for the first message whose text contains `poison` (a notifier bug), sends the rest."""

    def __init__(self, poison: str) -> None:
        super().__init__()
        self.poison = poison
        self.failed = 0

    async def send(self, msg: OutboundMessage) -> None:
        if self.poison in msg.text and not self.failed:
            self.failed += 1
            raise RuntimeError("notifier bug")
        await super().send(msg)


async def test_failing_notifier_does_not_stop_cursor_or_other_streams(world: World) -> None:
    await started(world)
    flaky = FlakyNotifier(poison="first error")
    relay = world.relay(notifier=flaky)
    first = add_event(world, "error", "engine", "first error")
    second = add_event(world, "error", "engine", "second error")
    entry_fill, exit_fill, _ = add_round_trip(
        world, exit_purpose="stop", exit_reason="stop_loss", exit_price="21.40"
    )

    await relay.pump()

    assert flaky.failed == 1
    keys = {msg.dedupe_key for msg in flaky.sent}
    assert f"event:{second}" in keys and f"event:{first}" not in keys
    assert {f"fill:{entry_fill}", f"fill:{exit_fill}"} <= keys
    assert cursor(world, "events") == second
    assert cursor(world, "fills") == exit_fill

    await relay.pump()  # the failed row is the notifier's to log; the relay does not retry it
    assert f"event:{first}" not in {msg.dedupe_key for msg in flaky.sent}


# --- 8. catch-up cap --------------------------------------------------------------------------------------
async def test_catch_up_sends_at_most_the_cap_plus_one_summary(world: World) -> None:
    await started(world)
    ids = [add_event(world, "error", "engine", f"error {i}") for i in range(50)]
    world.settings = RuntimeSettings(telegram_relay_catchup_max=20)
    relay = world.relay()  # the worker restarts after the outage: its first pump is a catch-up

    report = await relay.pump()

    sent = world.notifier.sent
    assert len(sent) == 21
    event_keys = [msg.dedupe_key for msg in sent if msg.dedupe_key and msg.dedupe_key.startswith("event:")]
    assert event_keys == [f"event:{i}" for i in ids[-20:]]  # the newest 20, in order
    summaries = [v for v in rendered(world, "alert") if v.source == "relay"]
    assert len(summaries) == 1
    assert "30 older alerts not sent; see the System page" in summaries[0].message
    assert report.events == 20 and report.skipped == 30
    assert cursor(world, "events") == ids[-1]

    world.notifier.sent.clear()
    again = await relay.pump()
    assert world.notifier.sent == [] and again == RelayReport(0, 0, 0, 0, 0)


# --- 9. a broken messenger --------------------------------------------------------------------------------
async def test_messenger_raising_does_not_stop_fills_and_events(world: World) -> None:
    relay = await started(world)
    add_proposal(world)
    world.messenger.raise_on_send = RuntimeError("bot down")
    add_round_trip(world, exit_purpose="stop", exit_reason="stop_loss", exit_price="21.40")
    add_event(world, "error", "killswitch", "daily loss tripped")
    world.messenger.closed = 2

    report = await relay.pump()

    assert report.fills == 2 and report.events == 1 and report.closed == 2
    assert world.messenger.sync_calls == 2  # once in started(), once here
    assert len(world.notifier.sent) == 3


# --- fix round 1 ------------------------------------------------------------------------------------------
async def test_cap_zero_means_no_cap(world: World) -> None:
    await started(world)
    ids = [add_event(world, "error", "engine", f"error {i}") for i in range(30)]
    world.settings = RuntimeSettings(telegram_relay_catchup_max=0)
    report = await world.relay().pump()  # a restart, so it would be a catch-up
    assert report.events == 30 and report.skipped == 0
    assert [msg.dedupe_key for msg in world.notifier.sent] == [f"event:{i}" for i in ids]
    assert [v for v in rendered(world, "alert") if v.source == "relay"] == []


async def test_a_fresh_burst_in_normal_running_is_not_capped(world: World) -> None:
    relay = await started(world)  # this instance has pumped before: not a restart
    world.settings = RuntimeSettings(telegram_relay_catchup_max=20)
    ids = [add_event(world, "error", "engine", f"error {i}") for i in range(25)]
    report = await relay.pump()
    assert report.events == 25 and report.skipped == 0
    assert [msg.dedupe_key for msg in world.notifier.sent] == [f"event:{i}" for i in ids]


async def test_rows_waiting_past_the_backlog_age_are_capped_without_a_restart(world: World) -> None:
    relay = await started(world)
    world.settings = RuntimeSettings(telegram_relay_catchup_max=20)
    for i in range(25):
        add_event(world, "error", "engine", f"error {i}")  # ts = NOW
    world.clock.advance(timedelta(minutes=10))  # the worker was stuck for 10 minutes
    report = await relay.pump()
    assert report.events == 20 and report.skipped == 5


async def test_a_row_committed_below_the_cursor_is_still_relayed(world: World) -> None:
    relay = await started(world)
    slow = world.factory()
    try:
        row = m.EventLog(
            ts=NOW, level="error", source="killswitch", run_id=world.run_id, message="slow", data={}
        )
        slow.add(row)
        slow.flush()  # takes its id, not committed yet
        fast = add_event(world, "error", "engine", "fast")
        await relay.pump()
        slow.commit()
    finally:
        slow.close()
    world.clock.advance(timedelta(seconds=30))
    await relay.pump()
    await relay.pump()
    assert sorted(msg.dedupe_key or "" for msg in world.notifier.sent) == sorted(
        [f"event:{fast}", f"event:{row.id}"]
    )
    # outside the 2-minute window a row below the cursor is no longer looked for
    world.notifier.sent.clear()
    world.clock.advance(timedelta(minutes=3))
    await world.relay().pump()
    assert world.notifier.sent == []


class PoisonRenderer(FakeRenderer):
    def alert(self, v: AlertView) -> OutboundMessage:
        if "poison" in v.message:
            raise ValueError("cannot render")
        return super().alert(v)


async def test_an_unrenderable_row_is_skipped_logged_once_and_never_relayed(world: World) -> None:
    await started(world)
    world.render = PoisonRenderer()
    relay = world.relay()
    bad = add_event(world, "error", "engine", "poison row")
    good = add_event(world, "error", "engine", "fine row")
    with structlog.testing.capture_logs() as logs:
        await relay.pump()
    assert [msg.dedupe_key for msg in world.notifier.sent] == [f"event:{good}"]
    assert cursor(world, "events") == good
    assert any(e["event"] == "relay.row_failed" and e["error_type"] == "ValueError" for e in logs)
    for _ in range(3):  # later pumps and a restarted relay meet it again in the re-scan window
        await relay.pump()
        await world.relay().pump()
    with session_scope(world.factory) as s:
        rows = s.execute(select(m.EventLog).where(m.EventLog.source == "notify.relay")).scalars().all()
        assert [(r.level, r.data["row_id"]) for r in rows] == [("error", bad)]
    assert [msg.dedupe_key for msg in world.notifier.sent] == [f"event:{good}"]  # its own error isn't relayed


# --- Fix round 2 (outage delivery): the retry pass, its backoff and the original order ----------------------
UNREACHABLE = TelegramNotSentError(None, "NetworkError: httpx.ConnectError: connection refused")


class OutageApi(FakeTelegramApi):
    """A FakeTelegramApi that can be switched off: while `down`, every send is unreachable (surely not
    delivered) and is logged as `send_message_failed`."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.chat: list[str] = []  # what was delivered, in order: message texts and "proposal <id>"

    async def send_message(self, chat_id: int, text: str, buttons: Any = (), silent: bool = False) -> int:
        if self.down:
            self.calls.append(("send_message_failed", {"text": text}))
            raise UNREACHABLE
        message_id = await super().send_message(chat_id, text, buttons, silent)
        self.chat.append(text)
        return message_id


class OutageMessenger(FakeMessenger):
    """Records proposal sends in the api's call log (so the order can be read); fails while it is down."""

    def __init__(self, api: OutageApi) -> None:
        super().__init__()
        self.api = api

    async def send_proposal(self, proposal_id: int, *, resend: bool = False) -> bool:
        if proposal_id in self.sent:
            return False  # like the bot: a proposal whose message is out is not sent again
        if self.api.down:
            self.api.calls.append(("proposal_failed", {"id": proposal_id}))
            return False
        self.api.calls.append(("proposal", {"id": proposal_id}))
        self.api.chat.append(f"proposal {proposal_id}")
        return await super().send_proposal(proposal_id, resend=resend)


class Outage:
    """A relay over a real TelegramNotifier whose Telegram can be switched off."""

    def __init__(self, w: World) -> None:
        self.w = w
        self.api = OutageApi()
        self.messenger = OutageMessenger(self.api)

        async def no_sleep(seconds: float) -> None:
            return None

        self.notifier = TelegramNotifier(self.api, 4242, w.factory, w.clock, sleep=no_sleep)

    @property
    def down(self) -> bool:
        return self.api.down

    @down.setter
    def down(self, value: bool) -> None:
        self.api.down = value

    def relay(self) -> NotificationRelay:
        return NotificationRelay(
            self.w.factory,
            self.w.clock,
            self.notifier,
            self.w.render,
            self.messenger,
            self.w.run_id,
            settings=lambda: self.w.settings,
        )

    def delivered(self) -> list[str]:
        """What reached the chat, in order: proposals and message texts."""
        return list(self.api.chat)

    def attempts(self) -> int:
        return sum(1 for method, _ in self.api.calls if method == "send_message_failed")


def notification(w: World, key: str) -> m.Notification:
    with session_scope(w.factory) as s:
        row = s.execute(select(m.Notification).where(m.Notification.dedupe_key == key)).scalar_one()
        s.expunge(row)
        return row


def alert_text(w: World, event_id: int) -> str:
    return notification(w, f"event:{event_id}").text


def created_now(w: World, proposal_id: int) -> None:
    with session_scope(w.factory) as s:
        s.get_one(m.Proposal, proposal_id).created_at = w.clock.now()


async def test_a_failed_message_is_retried_after_the_backoff_and_then_never_again(world: World) -> None:
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.down = True
    ev = add_event(world, "error", "killswitch", "max_drawdown_pct tripped")
    await relay.pump()
    row = notification(world, f"event:{ev}")
    assert (row.status, row.attempts) == ("failed", 1)
    assert o.attempts() == 2  # the notifier's own single retry inside the send

    o.down = False
    world.clock.advance(timedelta(seconds=29))
    report = await relay.pump()
    assert report.held and report.retried == 0
    assert notification(world, f"event:{ev}").attempts == 1  # not due yet: not tried
    world.clock.advance(timedelta(seconds=1))
    report = await relay.pump()
    assert report.retried == 1 and not report.held
    row = notification(world, f"event:{ev}")
    assert (row.status, row.attempts) == ("sent", 2)
    assert o.delivered() == [row.text]

    for _ in range(3):  # sent: never again, from this relay or a restarted one
        world.clock.advance(timedelta(minutes=1))
        await relay.pump()
        await o.relay().pump()
    assert o.delivered() == [row.text]


async def test_a_message_that_may_have_been_delivered_is_never_retried(world: World) -> None:
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.api.fail("send_message", TelegramApiError(None, "timed out"))  # after sending: maybe delivered
    ev = add_event(world, "error", "killswitch", "daily_loss_pct tripped")
    await relay.pump()
    assert notification(world, f"event:{ev}").status == "unknown"
    for _ in range(4):
        world.clock.advance(timedelta(minutes=1))
        report = await relay.pump()
        assert not report.held
    assert len(o.api.calls_of("send_message")) == 1


async def test_a_message_is_sent_at_most_three_times_then_the_streams_move_on(world: World) -> None:
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.down = True
    ev = add_event(world, "error", "killswitch", "max_drawdown_pct tripped")
    await relay.pump()  # send 1
    world.clock.advance(timedelta(seconds=30))
    await relay.pump()  # send 2
    assert notification(world, f"event:{ev}").attempts == 2
    world.clock.advance(timedelta(seconds=30))
    report = await relay.pump()  # 30 s is not enough after a second failed send while Telegram is away
    assert report.held and notification(world, f"event:{ev}").attempts == 2
    world.clock.advance(timedelta(seconds=30))
    await relay.pump()  # send 3, 60 s after the second
    row = notification(world, f"event:{ev}")
    assert (row.status, row.attempts) == ("failed", 3)
    sends = o.attempts()

    later = add_event(world, "error", "engine", "strategy failed")
    o.down = False
    for _ in range(3):
        world.clock.advance(timedelta(minutes=2))
        report = await relay.pump()
        assert not report.held
    assert o.attempts() == sends  # out of sends: never again
    assert o.delivered() == [alert_text(world, later)]  # and it no longer holds anything back


async def test_after_an_outage_messages_go_out_in_their_original_order(world: World) -> None:
    """Telegram goes away: the first alert fails, later rows (an alert, a pending proposal) wait instead of
    each hitting Telegram. Once it is back, everything goes out oldest first, each exactly once."""
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.down = True
    first = add_event(world, "error", "killswitch", "first alert")
    await relay.pump()
    world.clock.advance(timedelta(seconds=10))
    pid = add_proposal(world)
    created_now(world, pid)
    second = add_event(world, "error", "engine", "second alert")
    report = await relay.pump()
    assert report.held
    assert o.attempts() == 2  # only the first alert's send (and its inner retry) hit Telegram
    assert not any(method.startswith("proposal") for method, _ in o.api.calls)  # the proposal waits

    o.down = False
    world.clock.advance(timedelta(seconds=10))
    report = await relay.pump()  # 20 s after the failure: still waiting
    assert report.held and o.delivered() == []
    world.clock.advance(timedelta(seconds=10))
    await relay.pump()
    await relay.pump()
    assert o.delivered() == [alert_text(world, first), f"proposal {pid}", alert_text(world, second)]


async def test_a_proposal_older_than_the_failed_messages_is_offered_every_pump(world: World) -> None:
    """A proposal that needs a decision is not held behind messages that failed after it: it is offered
    each pump (the bot sends it the moment Telegram is back), and still goes first."""
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.down = True
    pid = add_proposal(world)
    created_now(world, pid)
    ev = add_event(world, "error", "killswitch", "alert after the proposal")
    await relay.pump()
    world.clock.advance(timedelta(seconds=5))
    await relay.pump()
    assert [kw["id"] for method, kw in o.api.calls if method == "proposal_failed"] == [pid, pid]
    o.down = False
    world.clock.advance(timedelta(seconds=5))
    await relay.pump()
    assert o.delivered() == [f"proposal {pid}"]  # at once, while the alert waits for its backoff
    world.clock.advance(timedelta(seconds=20))
    await relay.pump()
    assert o.delivered() == [f"proposal {pid}", alert_text(world, ev)]


async def test_a_refused_message_is_retried_but_holds_nothing_back(world: World) -> None:
    """A 400 is about that message, not about Telegram: it is retried after its backoff like any failed
    message, but the streams keep flowing meanwhile."""
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.api.fail("send_message", TelegramApiError(400, "Bad Request: can't parse entities"))
    bad = add_event(world, "error", "engine", "odd text")
    await relay.pump()
    assert notification(world, f"event:{bad}").status == "failed"
    world.clock.advance(timedelta(seconds=5))
    good = add_event(world, "error", "killswitch", "next alert")
    report = await relay.pump()
    assert not report.held
    assert o.delivered() == [alert_text(world, good)]
    world.clock.advance(timedelta(seconds=30))
    await relay.pump()
    assert o.delivered() == [alert_text(world, good), alert_text(world, bad)]
    assert notification(world, f"event:{bad}").attempts == 2


async def test_failed_messages_older_than_the_retry_window_are_left_alone(world: World) -> None:
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    o.down = True
    ev = add_event(world, "error", "killswitch", "long ago")
    await relay.pump()
    o.down = False
    world.clock.advance(timedelta(minutes=31))
    report = await relay.pump()
    assert not report.held and o.delivered() == []
    assert notification(world, f"event:{ev}").attempts == 1


async def test_a_failed_job_message_is_retried_with_its_stored_text(world: World) -> None:
    """A message a cron job sent through the notifier (the pre-market brief) is retried by the worker's
    relay too, with the same dedupe key, so it still goes out at most once."""
    o = Outage(world)
    relay = o.relay()
    await relay.pump()
    brief = OutboundMessage(kind="premarket_brief", text="Pre-market brief", dedupe_key="premarket:x")
    o.down = True
    await o.notifier.send(brief)
    assert notification(world, "premarket:x").status == "failed"
    o.down = False
    world.clock.advance(timedelta(seconds=30))
    await relay.pump()
    await o.notifier.send(brief)  # a re-run of the job: already sent
    assert o.delivered() == ["Pre-market brief"]
    assert notification(world, "premarket:x").status == "sent"
