"""Phase 3 test fakes for the Telegram and notification contracts (P3-T1). No network, no real sleeping.

- FakeTelegramApi: a TelegramApi that records every call (failed attempts included) in order.
- RecordingNotifier: a Notifier that keeps what it sent and honours dedupe_key like the real one.
- FakeRenderer: a Renderer whose messages carry the view's repr, recording (method, args).
- FakeIssuer: a CallbackIssuer with deterministic nonces n1, n2, ... and data "<kind>:<ref>:<action>:<nonce>".
- FakeMessenger: a ProposalMessenger that records the proposal ids it was asked to send.
"""

import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from trader.adapters.telegram.types import CallbackKind, CallbackQuery, Update
from trader.notify.types import (
    AlertView,
    Buttons,
    DailySummaryView,
    FillView,
    MessageKind,
    OutboundMessage,
    OverlayView,
    PnlView,
    PositionLine,
    PreopenView,
    ProposalView,
    StatusView,
    WeeklyReportView,
)


class FakeTelegramApi:
    """Records calls as (method, kwargs). Messages are numbered from 1. `fail(method, exc, times)` makes
    the next `times` calls of `method` raise `exc` (the attempt is still recorded).

    get_updates follows Telegram's offset rule: an offset confirms (drops) every queued update with a
    smaller id, and the call returns the rest, which stay queued until a later offset confirms them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_message_id = 1
        self._next_update_id = 1
        self._next_callback_id = 1
        self._queue: list[Update] = []
        self._failures: dict[str, list[BaseException]] = {}

    # --- test controls ---------------------------------------------------------------------------------
    def queue_updates(self, *updates: Update) -> None:
        self._queue.extend(updates)

    def fail(self, method: str, exc: BaseException, times: int = 1) -> None:
        self._failures.setdefault(method, []).extend([exc] * times)

    def callback_update(
        self, data: str, chat_id: int, message_id: int | None, from_id: int | None = None
    ) -> Update:
        """A button-tap update (not queued). `from_id` defaults to the chat id (a private chat)."""
        sender = chat_id if from_id is None else from_id
        cb = CallbackQuery(
            id=f"cb{self._next_callback_id}",
            data=data,
            chat_id=chat_id,
            from_id=sender,
            message_id=message_id,
        )
        self._next_callback_id += 1
        return self._update(chat_id=chat_id, from_id=sender, text=None, callback=cb)

    def text_update(self, text: str, chat_id: int, from_id: int | None = None) -> Update:
        """A text-message update (not queued)."""
        sender = chat_id if from_id is None else from_id
        return self._update(chat_id=chat_id, from_id=sender, text=text, callback=None)

    def last_sent(self) -> dict[str, Any]:
        """The kwargs of the latest send_message call."""
        return [kw for name, kw in self.calls if name == "send_message"][-1]

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == method]

    def _update(
        self, chat_id: int, from_id: int | None, text: str | None, callback: CallbackQuery | None
    ) -> Update:
        update = Update(
            update_id=self._next_update_id, chat_id=chat_id, from_id=from_id, text=text, callback=callback
        )
        self._next_update_id += 1
        return update

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        pending = self._failures.get(method)
        if pending:
            raise pending.pop(0)

    # --- TelegramApi -------------------------------------------------------------------------------------
    async def get_updates(self, offset: int | None, timeout: int) -> list[Update]:
        self._record("get_updates", offset=offset, timeout=timeout)
        await asyncio.sleep(0)  # yield to the event loop, as a real long poll would
        if offset is not None:
            self._queue = [u for u in self._queue if u.update_id >= offset]
        return list(self._queue)

    async def send_message(self, chat_id: int, text: str, buttons: Buttons = (), silent: bool = False) -> int:
        self._record("send_message", chat_id=chat_id, text=text, buttons=buttons, silent=silent)
        message_id = self._next_message_id
        self._next_message_id += 1
        return message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons: Buttons = ()) -> None:
        self._record("edit_message", chat_id=chat_id, message_id=message_id, text=text, buttons=buttons)

    async def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons = ()) -> None:
        self._record("edit_buttons", chat_id=chat_id, message_id=message_id, buttons=buttons)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self._record("answer_callback", callback_id=callback_id, text=text)

    async def aclose(self) -> None:
        self._record("aclose")


class RecordingNotifier:
    """Keeps every message it would send; a dedupe_key already sent is skipped (like TelegramNotifier)."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._keys: set[str] = set()

    async def send(self, msg: OutboundMessage) -> None:
        if msg.dedupe_key is not None:
            if msg.dedupe_key in self._keys:
                return
            self._keys.add(msg.dedupe_key)
        self.sent.append(msg)


class FakeRenderer:
    """Every method returns an OutboundMessage whose text is the repr of its first argument (the view),
    or the method name when it has none; buttons passed in are kept. Calls are recorded as
    (method, args)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _msg(self, method: str, kind: MessageKind, *args: Any, buttons: Buttons = ()) -> OutboundMessage:
        self.calls.append((method, args))
        text = repr(args[0]) if args else method
        return OutboundMessage(kind=kind, text=text, buttons=buttons)

    def proposal(self, v: ProposalView, buttons: Buttons) -> OutboundMessage:
        return self._msg("proposal", "proposal", v, buttons, buttons=buttons)

    def proposal_closed(self, v: ProposalView, final_status: str, via: str | None) -> str:
        self.calls.append(("proposal_closed", (v, final_status, via)))
        return repr(v)

    def fill(self, v: FillView) -> OutboundMessage:
        return self._msg("fill", "fill", v)

    def overlay(self, v: OverlayView) -> OutboundMessage:
        return self._msg("overlay", "overlay", v)

    def alert(self, v: AlertView) -> OutboundMessage:
        return self._msg("alert", v.kind, v)

    def daily_summary(self, v: DailySummaryView, buttons: Buttons) -> OutboundMessage:
        return self._msg("daily_summary", "daily_summary", v, buttons, buttons=buttons)

    def journal_answered(self, session_date: date, rules_followed: bool) -> OutboundMessage:
        return self._msg("journal_answered", "reply", session_date, rules_followed)

    def premarket_brief(self, session_date: date, brief: str) -> OutboundMessage:
        return self._msg("premarket_brief", "premarket_brief", session_date, brief)

    def preopen(self, v: PreopenView) -> OutboundMessage:
        return self._msg("preopen", "preopen", v)

    def checkin(self, v: StatusView, at_label: str) -> OutboundMessage:
        return self._msg("checkin", "checkin", v, at_label)

    def status(self, v: StatusView) -> OutboundMessage:
        return self._msg("status", "reply", v)

    def positions(self, lines: Sequence[PositionLine], now: datetime) -> OutboundMessage:
        return self._msg("positions", "reply", lines, now)

    def pnl(self, v: PnlView) -> OutboundMessage:
        return self._msg("pnl", "reply", v)

    def pause_confirm(self, buttons: Buttons) -> OutboundMessage:
        return self._msg("pause_confirm", "reply", buttons, buttons=buttons)

    def help(self) -> OutboundMessage:
        return self._msg("help", "reply")

    def reply(self, text: str) -> OutboundMessage:
        return self._msg("reply", "reply", text)

    def weekly_link(self, week_ending: date) -> OutboundMessage:
        return self._msg("weekly_link", "weekly_report", week_ending)

    def weekly_report(self, v: WeeklyReportView) -> OutboundMessage:
        return self._msg("weekly_report", "weekly_report", v)


class FakeIssuer:
    """Deterministic nonces n1, n2, ...; callback data "<kind>:<ref>:<action>:<nonce>" (unsigned)."""

    def __init__(self) -> None:
        self.issued: list[dict[str, Any]] = []
        self.bound: dict[str, int] = {}

    def issue(
        self,
        kind: CallbackKind,
        ref: str,
        actions: Sequence[str],
        chat_id: int,
        ttl_seconds: int | None,
    ) -> tuple[str, dict[str, str]]:
        nonce = f"n{len(self.issued) + 1}"
        data = {action: f"{kind}:{ref}:{action}:{nonce}" for action in actions}
        self.issued.append(
            {
                "nonce": nonce,
                "kind": kind,
                "ref": ref,
                "actions": list(actions),
                "chat_id": chat_id,
                "ttl_seconds": ttl_seconds,
                "data": data,
            }
        )
        return nonce, data

    def bind(self, nonce: str, message_id: int) -> None:
        self.bound[nonce] = message_id


class FakeMessenger:
    """Records send_proposal calls; set `raise_on_send` to make send_proposal raise it."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.calls: list[tuple[int, bool]] = []
        self.sync_calls = 0
        self.closed = 0  # what sync_closed returns
        self.raise_on_send: BaseException | None = None

    async def send_proposal(self, proposal_id: int, *, resend: bool = False) -> bool:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.calls.append((proposal_id, resend))
        self.sent.append(proposal_id)
        return True

    async def sync_closed(self) -> int:
        self.sync_calls += 1
        return self.closed
