"""P3-T1 acceptance test 5: the Phase 3 contracts that T2-T11 build against.

The typed assignments in `_structural_checks` are what mypy verifies (run mypy on this file); the runtime
tests below check the same things by name and signature, and pin every public name T1 created, so a
builder who renames a contract breaks this test.
"""

import dataclasses
import importlib
import inspect
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.fakes_telegram import (
    FakeIssuer,
    FakeMessenger,
    FakeRenderer,
    FakeTelegramApi,
    RecordingNotifier,
)
from trader.adapters.telegram.types import (
    CallbackIssuer,
    CallbackQuery,
    CommandHandler,
    ProposalMessenger,
    TelegramApi,
    TelegramApiError,
    Update,
)
from trader.engine.orchestrator import Engine
from trader.engine.scheduler import EventRunner
from trader.jobs.postclose import ArchiveData
from trader.market.data_service import MarketDataService
from trader.notify import types as nt
from trader.notify.types import Button, Notifier, OutboundMessage, Renderer
from trader.worker import WorkerEngine

T0 = datetime(2026, 10, 6, 13, 35, 5, tzinfo=UTC)

STUB_MODULES: dict[str, set[str]] = {
    "trader.engine.scheduler": {
        "EVENT_JOB_PREFIX",
        "MAX_EVENT_ATTEMPTS",
        "event_job",
        "PlannedEvent",
        "DayPlan",
        "day_plan",
        "fired_keys",
        "due_events",
        "EventRunner",
        "FireDeps",
        "FireStatus",
        "FireResult",
        "MissedEvent",
        "fire_event",
    },
    "trader.jobs.runner": {"run_job", "run_job_async", "JobOutcome", "JobRunMissing"},
    "trader.notify.messages": {
        "TELEGRAM_LIMIT",
        "MessageRenderer",
        "link",
        "fmt_money",
        "fmt_price",
        "fmt_pct",
        "fmt_time",
        "fmt_duration",
    },
    "trader.notify.notifier": {"TelegramNotifier", "NullNotifier", "split_text"},
    "trader.notify.relay": {"STREAMS", "NotificationRelay", "RelayReport", "alert_kind"},
    "trader.adapters.telegram.api": {"PtbTelegramApi"},
    "trader.adapters.telegram.callbacks": {
        "CallbackSigner",
        "ParsedCallback",
        "DbCallbackIssuer",
        "ClaimResult",
    },
    "trader.adapters.telegram.bot": {"TelegramBot"},
    "trader.adapters.telegram.commands": {
        "CommandDeps",
        "Commands",
        "status_view",
        "position_lines",
        "pnl_view",
    },
    "trader.worker": {
        "WorkerEngine",
        "WorkerDeps",
        "StepReport",
        "Worker",
        "acquire_single_instance",
        "main",
    },
    "trader.jobs.preopen": {"PreopenDeps", "run_preopen"},
    "trader.jobs.checkin": {"CheckinDeps", "run_checkin"},
    "trader.jobs.events": {"run_event_backup"},
    "trader.jobs.postclose": {
        "ArchiveData",
        "PostcloseDeps",
        "run_postclose",
        "archive_candles",
        "daily_summary_view",
    },
    "trader.runtime": {
        "build_notifier",
        "build_renderer",
        "build_signer",
        "build_decider",
        "open_engine",
        "fire_deps",
        "run_worker",
        "run_cli_job",
    },
    "trader.market.sessions": {"SessionPhase", "session_phase", "current_session"},
    "trader.notify.types": {
        "MessageKind",
        "Button",
        "Buttons",
        "OutboundMessage",
        "ProposalView",
        "FillView",
        "OverlayView",
        "AlertView",
        "PositionLine",
        "StatusView",
        "PnlView",
        "TradeLine",
        "DailySummaryView",
        "Check",
        "PreopenView",
        "Notifier",
        "Renderer",
    },
    "trader.adapters.telegram.types": {
        "CallbackQuery",
        "Update",
        "TelegramApiError",
        "TelegramApi",
        "CallbackKind",
        "CallbackIssuer",
        "ProposalMessenger",
        "CommandHandler",
    },
}


@pytest.mark.parametrize("module", sorted(STUB_MODULES))
def test_every_contract_module_imports_with_its_names(module: str) -> None:
    mod = importlib.import_module(module)
    missing = sorted(name for name in STUB_MODULES[module] if not hasattr(mod, name))
    assert missing == []


VIEW_FIELDS: dict[type, list[str]] = {
    nt.Button: ["text", "callback_data"],
    nt.OutboundMessage: ["kind", "text", "buttons", "dedupe_key", "silent"],
    nt.ProposalView: [
        "proposal_id",
        "kind",
        "status",
        "ticker",
        "side",
        "order_type",
        "qty",
        "stop",
        "limit",
        "stop_loss",
        "risk_usd",
        "reason",
        "strategy_key",
        "created_at",
        "expires_at",
        "decided_via",
        "error",
        "decided_at",  # P4-T1 (contract refinement 2): last, defaulted to None
    ],
    nt.FillView: [
        "fill_id",
        "ticker",
        "side",
        "purpose",
        "qty",
        "price",
        "ts",
        "reason",
        "position_id",
        "stop_loss",
        "pnl",
        "pnl_r",
    ],
    nt.OverlayView: ["decision", "spy_return", "prior_close", "price", "position_ids", "ts", "note"],
    nt.AlertView: ["kind", "level", "source", "message", "ts", "data"],
    nt.PositionLine: [
        "position_id",
        "ticker",
        "qty",
        "entry",
        "last",
        "stop",
        "unrealized_pnl",
        "unprotected_seconds",
        "stop_working",
    ],
    nt.StatusView: [
        "now",
        "phase",
        "session_date",
        "next_event_key",
        "next_event_at",
        "approval_mode",
        "blocking_switches",
        "token_ok",
        "token_age_hours",
        "token_error",
        "heartbeat_age_seconds",
        "positions",
        "pending_count",
    ],
    nt.PnlView: [
        "session_date",
        "realized_today",
        "unrealized",
        "week_to_date",
        "equity",
        "peak_equity",
        "drawdown_pct",
    ],
    nt.TradeLine: ["ticker", "qty", "entry", "exit", "pnl", "pnl_r", "exit_reason"],
    nt.DailySummaryView: [
        "session_date",
        "trades",
        "realized_pnl",
        "fees",
        "equity",
        "drawdown_pct",
        "open_positions",
        "decisions",
        "avg_decision_seconds",
        "unprotected_seconds",
        "blocking_switches",
        "archive",
        "run_to_date",  # P5-T1 (defaulted)
        "decision_log",  # P6-T11 (defaulted, last)
    ],
    nt.Check: ["name", "ok", "level", "detail"],
    nt.PreopenView: ["session_date", "approval_mode", "checks"],
    CallbackQuery: ["id", "data", "chat_id", "from_id", "message_id"],
    Update: ["update_id", "chat_id", "from_id", "text", "callback"],
}


@pytest.mark.parametrize("cls", list(VIEW_FIELDS), ids=lambda c: c.__name__)
def test_views_are_frozen_slotted_dataclasses_with_the_planned_fields(cls: type) -> None:
    assert dataclasses.is_dataclass(cls)
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen
    assert "__slots__" in vars(cls)
    assert [f.name for f in dataclasses.fields(cls)] == VIEW_FIELDS[cls]


def test_outbound_message_defaults() -> None:
    msg = OutboundMessage(kind="alert", text="x")
    assert (msg.buttons, msg.dedupe_key, msg.silent) == ((), None, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.text = "y"  # type: ignore[misc]


def test_telegram_api_error_fields() -> None:
    err = TelegramApiError(429, "Too Many Requests: retry after 3", retry_after=3.0)
    assert (err.status, err.description, err.retry_after) == (429, "Too Many Requests: retry after 3", 3.0)
    assert isinstance(err, Exception)
    assert "Too Many Requests" in str(err)
    plain = TelegramApiError(None, "timed out")
    assert (plain.status, plain.retry_after) == (None, None)


# --- structural checks ----------------------------------------------------------------------------------


def _structural_checks(
    api: FakeTelegramApi,
    notifier: RecordingNotifier,
    renderer: FakeRenderer,
    issuer: FakeIssuer,
    messenger: FakeMessenger,
    engine: Engine,
    market: MarketDataService,
) -> None:
    """Never called: mypy checks these assignments (the protocols are satisfied structurally)."""
    a: TelegramApi = api
    n: Notifier = notifier
    r: Renderer = renderer
    i: CallbackIssuer = issuer
    p: ProposalMessenger = messenger
    w: WorkerEngine = engine
    e: EventRunner = engine
    d: ArchiveData = market
    del a, n, r, i, p, w, e, d


def _members(proto: type) -> dict[str, Callable[..., Any]]:
    return {
        name: value for name, value in vars(proto).items() if callable(value) and not name.startswith("_")
    }


def _params(fn: Callable[..., Any]) -> list[str]:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


@pytest.mark.parametrize(
    ("impl", "proto"),
    [
        (FakeTelegramApi, TelegramApi),
        (RecordingNotifier, Notifier),
        (FakeRenderer, Renderer),
        (FakeIssuer, CallbackIssuer),
        (FakeMessenger, ProposalMessenger),
        (Engine, WorkerEngine),
        (Engine, EventRunner),
        (MarketDataService, ArchiveData),
    ],
    ids=lambda c: c.__name__,
)
def test_implementation_has_every_protocol_member(impl: type, proto: type) -> None:
    members = _members(proto)
    assert members, f"{proto.__name__} declares no methods"
    for name, spec in members.items():
        got = getattr(impl, name, None)
        assert got is not None, f"{impl.__name__} lacks {name}"
        assert _params(got) == _params(spec), f"{impl.__name__}.{name} parameters differ"
        assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(spec), name


def test_command_handler_protocol_shape() -> None:
    members = _members(CommandHandler)
    assert set(members) == {"handle", "confirm_pause"}
    assert all(inspect.iscoroutinefunction(f) for f in members.values())


# --- the fakes behave as documented -------------------------------------------------------------------


async def test_fake_telegram_api_records_and_numbers_messages() -> None:
    api = FakeTelegramApi()
    buttons = ((Button("Approve", "p:1:a:n1"), Button("Reject", "p:1:r:n1")),)
    assert await api.send_message(42, "one", buttons) == 1
    assert await api.send_message(42, "two", silent=True) == 2
    await api.edit_message(42, 1, "one, edited")
    await api.edit_buttons(42, 2)
    await api.answer_callback("cb1", "Approved")
    await api.aclose()
    assert [name for name, _ in api.calls] == [
        "send_message",
        "send_message",
        "edit_message",
        "edit_buttons",
        "answer_callback",
        "aclose",
    ]
    assert api.calls[0][1] == {"chat_id": 42, "text": "one", "buttons": buttons, "silent": False}
    assert api.calls[1][1]["silent"] is True
    assert api.last_sent() == {"chat_id": 42, "text": "two", "buttons": (), "silent": True}


async def test_fake_telegram_api_fail_raises_then_recovers() -> None:
    api = FakeTelegramApi()
    api.fail("answer_callback", TelegramApiError(400, "Bad Request: query is too old"), times=2)
    for _ in range(2):
        with pytest.raises(TelegramApiError):
            await api.answer_callback("cb1", "x")
    await api.answer_callback("cb1", "x")
    assert [name for name, _ in api.calls] == ["answer_callback"] * 3  # failed attempts are recorded too


async def test_fake_telegram_api_updates_follow_the_offset() -> None:
    api = FakeTelegramApi()
    cb = api.callback_update("p:1:a:n1:mac", chat_id=42, message_id=7)
    txt = api.text_update("/status", chat_id=42)
    assert cb.callback is not None and cb.callback.message_id == 7 and cb.callback.chat_id == 42
    assert cb.chat_id == 42 and cb.text is None
    assert txt.text == "/status" and txt.callback is None
    assert txt.update_id == cb.update_id + 1
    api.queue_updates(cb, txt)
    assert await api.get_updates(None, 30) == [cb, txt]
    assert await api.get_updates(txt.update_id, 30) == [txt]  # an offset confirms the earlier ones
    assert await api.get_updates(txt.update_id + 1, 30) == []
    assert [name for name, _ in api.calls] == ["get_updates"] * 3


async def test_recording_notifier_honours_dedupe_key() -> None:
    notifier = RecordingNotifier()
    await notifier.send(OutboundMessage(kind="fill", text="a", dedupe_key="fill:1"))
    await notifier.send(OutboundMessage(kind="fill", text="a again", dedupe_key="fill:1"))
    await notifier.send(OutboundMessage(kind="alert", text="b"))
    await notifier.send(OutboundMessage(kind="alert", text="b"))  # no key: sent every time
    assert [m.text for m in notifier.sent] == ["a", "b", "b"]


def test_fake_renderer_returns_the_view_repr_and_records_calls() -> None:
    renderer = FakeRenderer()
    line = nt.PositionLine(1, "AAA", 33, Decimal("21.56"), None, Decimal("21.41"), None, 0, True)
    buttons = ((Button("Yes", "j:20261006:y:n1"),),)
    pnl = nt.PnlView(
        date(2026, 10, 6),
        Decimal("1"),
        Decimal("0"),
        Decimal("1"),
        Decimal("721"),
        Decimal("721"),
        Decimal("0"),
    )
    msg = renderer.pnl(pnl)
    assert msg.text == repr(pnl) and msg.kind == "reply"
    assert renderer.pause_confirm(buttons).buttons == buttons
    assert renderer.help().text
    assert renderer.positions((line,), T0).text == repr((line,))
    assert renderer.proposal_closed(_proposal_view(), "approved", "telegram") == repr(_proposal_view())
    assert [name for name, _ in renderer.calls] == [
        "pnl",
        "pause_confirm",
        "help",
        "positions",
        "proposal_closed",
    ]
    assert renderer.calls[3][1] == ((line,), T0)


def _proposal_view() -> nt.ProposalView:
    return nt.ProposalView(
        proposal_id=1,
        kind="entry",
        status="pending",
        ticker="AAA",
        side="buy",
        order_type="stop",
        qty=33,
        stop=Decimal("21.55"),
        limit=None,
        stop_loss=Decimal("21.41"),
        risk_usd=Decimal("14.40"),
        reason="ORB long",
        strategy_key="orb_sip",
        created_at=T0,
        expires_at=T0,
        decided_via=None,
        error=None,
    )


def test_fake_issuer_is_deterministic() -> None:
    issuer = FakeIssuer()
    n1, data1 = issuer.issue("proposal", "12", ["a", "r"], 42, None)
    n2, data2 = issuer.issue("pause", "1", ["y", "n"], 42, 60)
    issuer.bind(n1, 99)
    assert (n1, n2) == ("n1", "n2")
    assert data1 == {"a": "proposal:12:a:n1", "r": "proposal:12:r:n1"}
    assert data2 == {"y": "pause:1:y:n2", "n": "pause:1:n:n2"}
    assert issuer.bound == {"n1": 99}
    assert [(i["kind"], i["ttl_seconds"]) for i in issuer.issued] == [("proposal", None), ("pause", 60)]


async def test_fake_messenger_records() -> None:
    messenger = FakeMessenger()
    assert await messenger.send_proposal(3) is True
    assert await messenger.send_proposal(4, resend=True) is True
    assert await messenger.sync_closed() == 0
    assert messenger.sent == [3, 4]
    assert messenger.calls == [(3, False), (4, True)]
    assert messenger.sync_calls == 1
    messenger.raise_on_send = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await messenger.send_proposal(5)
