"""The worker's Telegram side: long-poll updates from the configured chat only, verify signed single-use
callbacks, approvals through ProposalService.decide, commands, the pause confirmation, journal answers,
and proposal messages (BR-31, BR-34, BR-60; SPEC §4.4, §14).

Callback order (S6): verify the MAC, claim the nonce, act, answer the callback, and only then edit the
message. A failed answer or edit is logged with Telegram's description and never changes a decision.
Callback answers are plain text, at most 200 characters, no emoji. The bot never logs update text,
callback data, a URL or an exception's repr (the token travels in PTB's URLs).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trader.adapters.telegram.callbacks import CallbackSigner, ClaimResult, DbCallbackIssuer, ParsedCallback
from trader.adapters.telegram.types import (
    CallbackQuery,
    CommandHandler,
    TelegramApi,
    TelegramApiError,
    Update,
)
from trader.db import models as m
from trader.db.session import session_scope
from trader.engine.proposals import Decision, DecisionResult, Via
from trader.events import log_event
from trader.market.clock import Clock
from trader.notify.types import Button, Buttons, OutboundMessage, ProposalView, Renderer
from trader.settings_store import RuntimeSettings

SOURCE = "telegram"
ANSWER_LIMIT = 200  # Telegram's callback answer limit
CONFLICT_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 60.0
APPROVE_TEXT = "✅ Approve"
REJECT_TEXT = "❌ Reject"
CLAIM_ANSWERS: dict[ClaimResult, str] = {
    "unknown": "Invalid button",
    "used": "Already answered",
    "expired": "Button expired",
    "wrong_message": "Old message",
}
DECISIONS: dict[str, Decision] = {"a": "approve", "r": "reject"}
PROPOSAL_ACTIONS = ("a", "r")
# How a proposal status reads after "Already ..." (anything else is shown as is).
STATUS_LABELS = {"auto_approved": "auto-approved"}
# A proposal message that could not be sent even on its one re-send. The relay never forwards source
# "telegram" (its own failures could loop), so this alert uses a source it does forward.
LOST_SEND_SOURCE = "telegram.proposal"
# Updates from another chat (or another sender in a private chat): at most this many warning events per
# foreign id per window; the rest are only logged.
FOREIGN_EVENT_BURST = 5
FOREIGN_EVENT_WINDOW = timedelta(minutes=10)

log = structlog.get_logger("telegram.bot")


def _toast(text: str) -> str:
    return text if len(text) <= ANSWER_LIMIT else text[: ANSWER_LIMIT - 3] + "..."


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _risk_usd(p: m.Proposal, spec: dict[str, Any]) -> Decimal | None:
    """Dollar risk of an entry: qty x per-share risk (from the sizing, else entry price - stop loss)."""
    if p.kind != "entry":
        return None
    sizing = p.sizing if isinstance(p.sizing, dict) else {}
    per_share = _dec(sizing.get("per_share_risk"))
    if per_share is None:
        entry = _dec(spec.get("stop")) or _dec(spec.get("limit"))
        stop_loss = _dec(spec.get("stop_loss"))
        if entry is None or stop_loss is None:
            return None
        per_share = entry - stop_loss
    return per_share * p.qty


def _error_text(exc: BaseException) -> dict[str, Any]:
    """What may be logged about a failure: Telegram's status and description, else only the type."""
    if isinstance(exc, TelegramApiError):
        return {"status": exc.status, "description": exc.description}
    return {"error_type": type(exc).__name__}


def _refused(exc: BaseException) -> bool:
    """Telegram answered with a 4xx (429 included): the request was not carried out."""
    return isinstance(exc, TelegramApiError) and exc.status is not None and exc.status < 500


def _retry_after(exc: BaseException) -> float | None:
    """Telegram's retry_after of a 429, else None."""
    if isinstance(exc, TelegramApiError) and exc.status == 429 and exc.retry_after:
        return max(float(exc.retry_after), 0.0)
    return None


class TelegramBot:
    """Implements trader.adapters.telegram.types.ProposalMessenger. `decide` has
    ProposalService.decide's signature: (proposal_id, decision, via, actor)."""

    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        factory: sessionmaker[Session],
        clock: Clock,
        issuer: DbCallbackIssuer,
        signer: CallbackSigner,
        decide: Callable[[int, Decision, Via, str], DecisionResult],
        commands: CommandHandler,
        render: Renderer,
        run_id: int,
        *,
        settings: Callable[[], RuntimeSettings],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.factory = factory
        self.clock = clock
        self.issuer = issuer
        self.signer = signer
        self.decide = decide
        self.commands = commands
        self.render = render
        self.run_id = run_id
        self.settings = settings
        self.sleep = sleep
        self._offset: int | None = None
        # Unbound proposal nonces whose one re-send also failed (in memory: a restart allows one more try).
        self._lost_sends: set[str] = set()
        # sync_closed waits until then after a 429 on an edit.
        self._edits_paused_until: datetime | None = None
        # Foreign id -> times of its recent warning events (the rate limit).
        self._foreign_events: dict[int, list[datetime]] = {}

    # --- polling loop ----------
    async def run(self, stop: asyncio.Event) -> None:
        """Long-poll until `stop` is set (checked after each call; a back-off sleep ends early when it is
        set). One bad update never stops the loop. The offset is kept on the bot, so a restarted loop in
        the same process confirms what was handled."""
        backoff = 1.0
        conflict_logged = False
        while not stop.is_set():
            try:
                updates = await self.api.get_updates(
                    self._offset, timeout=self.settings().telegram_poll_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_after = _retry_after(exc)
                if isinstance(exc, TelegramApiError) and exc.status == 409:
                    if not conflict_logged:
                        self._event(
                            "critical", "another poller is using this bot (409 Conflict)", _error_text(exc)
                        )
                        conflict_logged = True
                    log.error("telegram.poll_conflict", **_error_text(exc))
                    await self._pause(CONFLICT_BACKOFF_SECONDS, stop)
                elif retry_after is not None:
                    log.warning("telegram.poll_rate_limited", retry_after=retry_after, **_error_text(exc))
                    await self._pause(retry_after, stop)
                else:
                    log.warning("telegram.poll_failed", backoff=backoff, **_error_text(exc))
                    await self._pause(backoff, stop)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff, conflict_logged = 1.0, False
            for u in updates:
                self._offset = u.update_id + 1
                try:
                    await self.handle_update(u)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("telegram.update_failed", update_id=u.update_id, **_error_text(exc))

    async def _pause(self, seconds: float, stop: asyncio.Event) -> None:
        """Sleep (through the injected `sleep`) for `seconds`, or until `stop` is set, whichever is first."""
        sleeper = asyncio.ensure_future(self.sleep(seconds))
        stopper = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)

    # --- updates ----------
    async def handle_update(self, update: Update) -> None:
        """Only the configured chat is served. When it is a private chat (a positive id) the sender must
        also be that user; in a group chat (a negative id) any member's update is accepted."""
        cb = update.callback
        chats = {c for c in (update.chat_id, cb.chat_id if cb else None) if c is not None}
        kind = "callback" if cb is not None else "text" if update.text is not None else "other"
        if chats != {self.chat_id}:
            if chats:  # never the text or the data: only who and what kind
                foreign = max(chats - {self.chat_id})
                self._foreign_event(
                    "update from another chat ignored", foreign, {"chat_id": foreign, "kind": kind}
                )
            return
        sender = cb.from_id if cb is not None else update.from_id
        if self.chat_id > 0 and kind != "other" and sender != self.chat_id:
            self._foreign_event(
                "update from another sender ignored",
                sender if sender is not None else 0,
                {"chat_id": self.chat_id, "from_id": sender, "kind": kind},
            )
            return
        if cb is not None:
            await self._callback(cb)
        elif update.text is not None:
            for msg in await self.commands.handle(update.text):
                await self._send(msg)

    async def _callback(self, cb: CallbackQuery) -> None:
        parsed = self.signer.parse(cb.data)
        if parsed is None:
            self._event("warning", "invalid callback refused", {"chat_id": cb.chat_id, "reason": "bad_mac"})
            await self._answer(cb, CLAIM_ANSWERS["unknown"])
            return
        claim = self.issuer.claim(parsed, cb.chat_id, cb.message_id)
        if claim != "ok":
            if claim == "unknown":
                self._event("warning", "unknown callback nonce", {"chat_id": cb.chat_id, "kind": parsed.kind})
            await self._answer(cb, CLAIM_ANSWERS[claim])
            return
        if parsed.kind == "proposal":
            await self._proposal_tap(cb, parsed)
            return
        try:
            if parsed.kind == "pause":
                text = await self.commands.confirm_pause(parsed.action)
            else:
                text = self._journal_answer(parsed)
        except Exception as exc:
            self.issuer.release(parsed.nonce)  # nothing was saved: let the next tap try again
            log.error("telegram.callback_failed", kind=parsed.kind, **_error_text(exc))
            await self._answer(cb, "Error, try again")
            return
        await self._answer(cb, text)
        await self._remove_buttons(cb)
        if parsed.kind == "journal":
            await self._send(
                self.render.journal_answered(date.fromisoformat(parsed.ref), parsed.action == "y")
            )

    async def _proposal_tap(self, cb: CallbackQuery, parsed: ParsedCallback) -> None:
        proposal_id, decision = int(parsed.ref), DECISIONS[parsed.action]
        try:
            result = self.decide(proposal_id, decision, "telegram", f"telegram:{cb.from_id}")
        except KeyError:
            await self._answer(cb, "Unknown proposal")  # the nonce stays used: tapping again can't help
            return
        except Exception as exc:
            self.issuer.release(parsed.nonce)
            log.error("telegram.decide_failed", proposal_id=proposal_id, **_error_text(exc))
            await self._answer(cb, "Error, try again")
            return
        if result.already_decided:
            answer = f"Already {STATUS_LABELS.get(result.status, result.status)}"
        elif result.status == "failed":
            answer = "Approved, but the order failed"
        elif result.blocked is not None:
            answer = f"Not submitted, entry blocked: {result.blocked}"
        else:
            answer = "Approved" if decision == "approve" else "Rejected"
        await self._answer(cb, answer)
        view = self._proposal_view(proposal_id)
        message_id = cb.message_id
        if view is None or message_id is None:
            return
        text = self.render.proposal_closed(view, result.status, view.decided_via)
        try:
            await self.api.edit_message(self.chat_id, message_id, text, ())
        except Exception as exc:
            log.warning("telegram.edit_failed", proposal_id=proposal_id, **_error_text(exc))

    def _journal_answer(self, parsed: ParsedCallback) -> str:
        """Save the daily "Rules followed?" answer for the live run; returns the callback answer."""
        self._save_journal(date.fromisoformat(parsed.ref), parsed.action == "y")  # YYYYMMDD, signer-checked
        return "Saved"

    def _save_journal(self, session_date: date, followed: bool) -> None:
        now = self.clock.now()
        values = {"rules_followed": followed, "answered_via": SOURCE, "updated_at": now}
        stmt = insert(m.Journal).values(run_id=self.run_id, session_date=session_date, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["run_id", "session_date"], set_=values)
        with session_scope(self.factory) as s:
            s.execute(stmt)

    # --- proposal messages (ProposalMessenger) ----------
    async def send_proposal(self, proposal_id: int, *, resend: bool = False) -> bool:
        """Send the approval message of a pending proposal (False when skipped or not sent).

        A send refused by Telegram (4xx) delivered nothing: its nonce is discarded and the next call starts
        afresh. A network error or a 5xx may have delivered it: the unbound nonce is kept and the next call
        re-sends ONCE with the same nonce (a duplicate message is harmless: its buttons share the nonce, so
        only one decision can happen), binding it on success. If that re-send fails too, an `error` event
        with a source the relay forwards tells Stephen to decide on the web, and no further re-send is made.
        """
        view = self._proposal_view(proposal_id)
        if view is None or view.status != "pending":
            return False
        ref = str(proposal_id)
        retry_nonce: str | None = None
        if not resend:
            if self.issuer.exists_for("proposal", ref, bound=True):
                return False
            retry_nonce = self.issuer.unbound_for("proposal", ref, self.chat_id)
            if retry_nonce is not None and retry_nonce in self._lost_sends:
                return False
        if retry_nonce is None:
            # No TTL: the proposal's own expiry is enforced by ProposalService.decide ("Already expired").
            nonce, data = self.issuer.issue("proposal", ref, PROPOSAL_ACTIONS, self.chat_id, None)
        else:
            nonce, data = retry_nonce, self.issuer.data_for("proposal", ref, PROPOSAL_ACTIONS, retry_nonce)
        buttons: Buttons = ((Button(APPROVE_TEXT, data["a"]), Button(REJECT_TEXT, data["r"])),)
        msg = self.render.proposal(view, buttons)
        try:
            message_id = await self.api.send_message(self.chat_id, msg.text, msg.buttons, msg.silent)
        except Exception as exc:
            err = _error_text(exc)
            log.warning(
                "telegram.proposal_send_failed", proposal_id=proposal_id, retry=retry_nonce is not None, **err
            )
            if retry_nonce is not None:
                self._lost_sends.add(nonce)
                self._event(
                    "error",
                    f"proposal {proposal_id} approval message could not be sent to Telegram; "
                    "decide it on the web",
                    {"proposal_id": proposal_id, **err},
                    source=LOST_SEND_SOURCE,
                )
            else:
                self._event("warning", f"proposal {proposal_id} message not sent", err)
                if _refused(exc):
                    self.issuer.discard(nonce)  # nothing was delivered: the next call starts afresh
            return False
        self.issuer.bind(nonce, message_id)
        return True

    async def sync_closed(self) -> int:
        """Close every bound, unused proposal message whose proposal is no longer pending. An edit that
        fails transiently (network, 5xx, 429) reopens its nonce so a later call retries; after a 429 the
        edits wait for Telegram's retry_after."""
        now = self.clock.now()
        if self._edits_paused_until is not None and now < self._edits_paused_until:
            return 0
        self._edits_paused_until = None
        open_rows = self.issuer.open_messages("proposal", self.chat_id)
        if not open_rows:
            return 0
        refs = {int(ref) for _, ref, _ in open_rows}
        with self.factory() as s:
            closed_ids = set(
                s.scalars(
                    select(m.Proposal.id).where(
                        m.Proposal.id.in_(refs),
                        m.Proposal.run_id == self.run_id,
                        m.Proposal.status != "pending",
                    )
                )
            )
        edited = 0
        for nonce, ref, message_id in open_rows:
            proposal_id = int(ref)
            if proposal_id not in closed_ids:
                continue
            if not self.issuer.close_if_unused(nonce):
                continue  # a tap claimed it meanwhile
            view = self._proposal_view(proposal_id)
            if view is None:
                continue
            text = self.render.proposal_closed(view, view.status, view.decided_via)
            try:
                await self.api.edit_message(self.chat_id, message_id, text, ())
            except Exception as exc:
                log.warning("telegram.edit_failed", proposal_id=proposal_id, **_error_text(exc))
                retry_after = _retry_after(exc)
                is_429 = isinstance(exc, TelegramApiError) and exc.status == 429
                if is_429 or not _refused(exc):
                    self.issuer.release(nonce)  # transient: try again next time
                if is_429:  # rate limited: the other edits would fail too
                    if retry_after is not None:
                        self._edits_paused_until = self.clock.now() + timedelta(seconds=retry_after)
                    break
                continue
            edited += 1
        return edited

    def _proposal_view(self, proposal_id: int) -> ProposalView | None:
        with self.factory() as s:
            p = s.execute(
                select(m.Proposal).where(m.Proposal.id == proposal_id, m.Proposal.run_id == self.run_id)
            ).scalar_one_or_none()
            if p is None:
                return None
            spec: dict[str, Any] = p.order_spec if isinstance(p.order_spec, dict) else {}
            signal = s.get(m.Signal, p.signal_id)
            cancelled = s.get(m.Order, p.cancel_order_id) if p.cancel_order_id is not None else None
            symbol_id = spec.get("symbol_id")
            if symbol_id is None:
                symbol_id = (
                    cancelled.symbol_id if cancelled is not None else signal.symbol_id if signal else None
                )
            symbol = s.get(m.Symbol, symbol_id) if symbol_id is not None else None
            config = s.get(m.StrategyConfig, signal.strategy_config_id) if signal is not None else None
            return ProposalView(
                proposal_id=p.id,
                kind=p.kind,
                status=p.status,
                ticker=symbol.ticker if symbol is not None else "?",
                side=spec.get("side") or (cancelled.side if cancelled is not None else ""),
                order_type=spec.get("order_type") or (cancelled.order_type if cancelled is not None else ""),
                qty=p.qty,
                stop=_dec(spec.get("stop")),
                limit=_dec(spec.get("limit")),
                stop_loss=_dec(spec.get("stop_loss")),
                risk_usd=_risk_usd(p, spec),
                reason=str(spec.get("reason") or ""),
                strategy_key=config.strategy_key if config is not None else "",
                created_at=p.created_at,
                expires_at=p.expires_at,
                decided_via=p.decided_via,
                error=p.error,
            )

    # --- helpers ----------
    async def _answer(self, cb: CallbackQuery, text: str) -> None:
        try:
            await self.api.answer_callback(cb.id, _toast(text))
        except Exception as exc:
            log.warning("telegram.answer_failed", **_error_text(exc))

    async def _remove_buttons(self, cb: CallbackQuery) -> None:
        if cb.message_id is None:
            return
        try:
            await self.api.edit_buttons(self.chat_id, cb.message_id, ())
        except Exception as exc:
            log.warning("telegram.edit_failed", **_error_text(exc))

    async def _send(self, msg: OutboundMessage) -> None:
        try:
            await self.api.send_message(self.chat_id, msg.text, msg.buttons, msg.silent)
        except Exception as exc:
            log.warning("telegram.send_failed", kind=msg.kind, **_error_text(exc))

    def _foreign_event(self, message: str, foreign_id: int, data: dict[str, Any]) -> None:
        """A warning event about an ignored update, at most FOREIGN_EVENT_BURST per foreign id per
        FOREIGN_EVENT_WINDOW (a stranger can't flood event_log); the rest are only logged."""
        now = self.clock.now()
        cutoff = now - FOREIGN_EVENT_WINDOW
        for key in [k for k, times in self._foreign_events.items() if times[-1] <= cutoff]:
            del self._foreign_events[key]  # forget ids that went quiet
        recent = [t for t in self._foreign_events.get(foreign_id, []) if t > cutoff]
        if len(recent) >= FOREIGN_EVENT_BURST:
            log.warning("telegram.foreign_update_ignored", **data)
            self._foreign_events[foreign_id] = recent
            return
        self._foreign_events[foreign_id] = [*recent, now]
        self._event("warning", message, data)

    def _event(self, level: str, message: str, data: dict[str, Any], *, source: str = SOURCE) -> None:
        try:
            with session_scope(self.factory) as s:
                log_event(s, self.clock, level, source, message, data, run_id=self.run_id)
        except Exception as exc:
            log.error("telegram.event_log_failed", message=message, **_error_text(exc))
