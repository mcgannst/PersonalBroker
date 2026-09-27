"""The notification relay: new pending proposals, fills, error events and the overlay decision recorded
in the database go to Telegram, exactly once per row, surviving restarts (BR-32; SPEC §4.4, §6.2).

The engine and the cron jobs never call Telegram (SPEC §1: processes talk only through PostgreSQL). The
worker calls `pump()` every step; each pump reads the rows added since the stream's cursor
(`notify_cursors`), sends one message per row through the `Notifier` with a `dedupe_key` naming the row,
and then advances the cursor (send-then-advance: a crash in between re-reads the row, and the notifier's
dedupe key drops the second send, so nothing is sent twice).

A missing cursor starts at the table's current maximum id, so a first start sends no history. After a
long outage a pump sends at most `telegram.relay_catchup_max` rows per stream (the newest ones) plus one
summary line for the rest.
"""

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from trader.adapters.telegram.types import ProposalMessenger
from trader.db import models as m
from trader.db.session import session_scope
from trader.market.clock import Clock
from trader.notify.types import (
    AlertView,
    FillView,
    MessageKind,
    Notifier,
    OutboundMessage,
    OverlayView,
    ProposalView,
    Renderer,
)
from trader.settings_store import RuntimeSettings

STREAMS = ("proposals", "fills", "events")

OVERLAY_SOURCE = "strategy.spy_overlay"
ALERT_LEVELS = ("error", "critical")
NEVER_RELAYED = ("telegram",)  # the notifier's own failure events: relaying them could loop
SUMMARY_SOURCE = "relay"
SKIPPED_NOUN = {"proposals": "auto-mode proposals", "fills": "fills", "events": "alerts"}

log = structlog.get_logger("notify.relay")


@dataclass(frozen=True, slots=True)
class RelayReport:
    proposals: int
    fills: int
    events: int
    closed: int
    skipped: int


def alert_kind(source: str, level: str, message: str) -> MessageKind:
    """Which alert message an event_log row becomes (pure)."""
    if source == "killswitch":
        return "kill_switch"
    if source.startswith("job."):
        return "job_failure"
    if source == "questrade.token":
        return "token_failure"
    if source == "proposals":
        return "escalation"
    if source == "engine" and level == "critical":
        return "escalation"
    return "alert"


@dataclass(frozen=True, slots=True)
class _Batch:
    """One stream's rows to relay in this pump."""

    top: int | None  # the highest id read (relevant or not); the cursor ends here
    messages: list[tuple[int, OutboundMessage]]  # (row id, message), in id order
    skipped: int  # relevant rows past the catch-up cap, not sent
    last_skipped: int | None


class NotificationRelay:
    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Clock,
        notifier: Notifier,
        render: Renderer,
        messenger: ProposalMessenger,
        run_id: int,
        *,
        settings: Callable[[], RuntimeSettings],
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.notifier = notifier
        self.render = render
        self.messenger = messenger
        self.run_id = run_id
        self.settings = settings

    async def pump(self) -> RelayReport:
        """Relay everything new once. Each step runs in its own try block, so one failing step never stops
        the others; nothing here raises."""
        proposals = fills = events = closed = skipped = 0
        try:
            proposals += await self._pending_proposals()
        except Exception as exc:
            _log_failure("pending_proposals", exc)
        try:
            sent, missed = await self._relay_stream("proposals", self._scan_auto_proposals)
            proposals += sent
            skipped += missed
        except Exception as exc:
            _log_failure("auto_proposals", exc)
        try:
            fills, missed = await self._relay_stream("fills", self._scan_fills)
            skipped += missed
        except Exception as exc:
            _log_failure("fills", exc)
        try:
            events, missed = await self._relay_stream("events", self._scan_events)
            skipped += missed
        except Exception as exc:
            _log_failure("events", exc)
        try:
            closed = await self.messenger.sync_closed()
        except Exception as exc:
            _log_failure("sync_closed", exc)
        return RelayReport(proposals, fills, events, closed, skipped)

    # --- step 1: pending proposals go to the bot, which adds the buttons ---------------------------------
    async def _pending_proposals(self) -> int:
        with session_scope(self.factory) as s:
            ids = list(
                s.scalars(
                    select(m.Proposal.id)
                    .where(m.Proposal.run_id == self.run_id, m.Proposal.status == "pending")
                    .order_by(m.Proposal.id)
                )
            )
        sent = 0
        for proposal_id in ids:
            try:
                if await self.messenger.send_proposal(proposal_id):
                    sent += 1
            except Exception as exc:  # one bad proposal never blocks the others
                _log_failure("send_proposal", exc, proposal_id=proposal_id)
        return sent

    # --- the cursor-driven streams ------------------------------------------------------------------------
    async def _relay_stream(
        self, stream: str, scan: Callable[[Session, int, int], _Batch]
    ) -> tuple[int, int]:
        """Scan one stream, send its messages (send, then advance), and move the cursor past everything
        read. Returns (messages sent, rows skipped by the catch-up cap)."""
        cap = self.settings().telegram_relay_catchup_max
        with session_scope(self.factory) as s:
            start = self._cursor(s, stream)
            batch = scan(s, start, cap)
        if batch.top is None:
            return 0, 0
        if batch.skipped and batch.last_skipped is not None:
            summary = dataclasses.replace(
                self._summary(stream, batch.skipped),
                dedupe_key=f"relay:{stream}:skipped-through:{batch.last_skipped}",
            )
            await self._send(stream, summary, batch.last_skipped)
            self._advance(stream, batch.last_skipped)
        sent = 0
        for row_id, msg in batch.messages:
            if await self._send(stream, msg, row_id):
                sent += 1
            self._advance(stream, row_id)
        self._advance(stream, batch.top)
        return sent, batch.skipped

    def _window(
        self,
        s: Session,
        id_col: InstrumentedAttribute[int],
        start: int,
        cap: int,
        relevant: Sequence[ColumnElement[bool]],
    ) -> tuple[int | None, list[int], int, int | None]:
        """The rows after `start`: (top id read, the newest `cap` relevant ids in id order, the number of
        older relevant ids skipped, the highest skipped id)."""
        top = s.scalar(select(func.max(id_col)).where(id_col > start))
        if top is None:
            return None, [], 0, None
        in_window = [id_col > start, id_col <= top, *relevant]
        total = s.scalar(select(func.count(id_col)).where(*in_window)) or 0
        ids = sorted(s.scalars(select(id_col).where(*in_window).order_by(id_col.desc()).limit(cap)))
        skipped = total - len(ids)
        last_skipped: int | None = None
        if skipped:
            older = [id_col < ids[0]] if ids else []
            last_skipped = s.scalar(select(func.max(id_col)).where(*in_window, *older))
        return top, ids, skipped, last_skipped

    def _scan_auto_proposals(self, s: Session, start: int, cap: int) -> _Batch:
        """Proposals approved by auto mode (BR-30/BR-32): a silent message without buttons. Pending ones
        belong to the messenger, and `auto_flatten_on_expiry` results show up as fills."""
        relevant = [m.Proposal.run_id == self.run_id, m.Proposal.decided_by == "auto"]
        top, ids, skipped, last_skipped = self._window(s, m.Proposal.id, start, cap, relevant)
        messages: list[tuple[int, OutboundMessage]] = []
        for pid in ids:
            p = s.get_one(m.Proposal, pid)
            msg = self.render.proposal(_proposal_view(s, p), ())
            messages.append((pid, dataclasses.replace(msg, dedupe_key=f"proposal:{pid}", silent=True)))
        return _Batch(top, messages, skipped, last_skipped)

    def _scan_fills(self, s: Session, start: int, cap: int) -> _Batch:
        top, ids, skipped, last_skipped = self._window(
            s, m.Fill.id, start, cap, [m.Fill.run_id == self.run_id]
        )
        messages: list[tuple[int, OutboundMessage]] = []
        for fid in ids:
            msg = self.render.fill(_fill_view(s, s.get_one(m.Fill, fid)))
            messages.append((fid, dataclasses.replace(msg, dedupe_key=f"fill:{fid}")))
        return _Batch(top, messages, skipped, last_skipped)

    def _scan_events(self, s: Session, start: int, cap: int) -> _Batch:
        e = m.EventLog
        relevant = [
            or_(e.run_id.is_(None), e.run_id == self.run_id),
            e.source.not_in(NEVER_RELAYED),
            or_(e.level.in_(ALERT_LEVELS), and_(e.source == OVERLAY_SOURCE, e.data.has_key("decision"))),
        ]
        top, ids, skipped, last_skipped = self._window(s, e.id, start, cap, relevant)
        messages: list[tuple[int, OutboundMessage]] = []
        for eid in ids:
            row = s.get_one(m.EventLog, eid)
            data = row.data if isinstance(row.data, dict) else {}
            if row.source == OVERLAY_SOURCE and "decision" in data:
                msg = self.render.overlay(_overlay_view(row, data))
            elif row.level in ALERT_LEVELS:
                msg = self.render.alert(
                    AlertView(
                        kind=alert_kind(row.source, row.level, row.message),
                        level=row.level,
                        source=row.source,
                        message=row.message,
                        ts=row.ts,
                        data=data,
                    )
                )
            else:  # matched the SQL filter only through a non-object `data`: nothing to relay
                continue
            messages.append((eid, dataclasses.replace(msg, dedupe_key=f"event:{eid}")))
        return _Batch(top, messages, skipped, last_skipped)

    def _summary(self, stream: str, skipped: int) -> OutboundMessage:
        return self.render.alert(
            AlertView(
                kind="alert",
                level="warning",
                source=SUMMARY_SOURCE,
                message=f"{skipped} older {SKIPPED_NOUN[stream]} not sent; see the System page",
                ts=self.clock.now(),
                data={"stream": stream, "skipped": skipped},
            )
        )

    async def _send(self, stream: str, msg: OutboundMessage, row_id: int) -> bool:
        """Send one message. `Notifier.send` never raises by contract; a notifier that does anyway is
        logged and the row is not retried (the cursor still advances)."""
        try:
            await self.notifier.send(msg)
        except Exception as exc:
            _log_failure("send", exc, stream=stream, row_id=row_id)
            return False
        return True

    # --- cursors ------------------------------------------------------------------------------------------
    def _cursor(self, s: Session, stream: str) -> int:
        """The stream's cursor; created at the table's current maximum id when missing."""
        row = s.get(m.NotifyCursor, stream)
        if row is not None:
            return row.last_id
        id_col = _STREAM_IDS[stream]
        start = s.scalar(select(func.coalesce(func.max(id_col), 0))) or 0
        s.execute(
            insert(m.NotifyCursor)
            .values(stream=stream, last_id=start, updated_at=self.clock.now())
            .on_conflict_do_nothing(index_elements=["stream"])
        )
        return s.get_one(m.NotifyCursor, stream, populate_existing=True).last_id

    def _advance(self, stream: str, last_id: int) -> None:
        """Move the cursor forward to `last_id` (never back, so two relays can't rewind each other)."""
        stmt = insert(m.NotifyCursor).values(stream=stream, last_id=last_id, updated_at=self.clock.now())
        stmt = stmt.on_conflict_do_update(
            index_elements=["stream"],
            set_={
                "last_id": func.greatest(m.NotifyCursor.last_id, stmt.excluded.last_id),
                "updated_at": stmt.excluded.updated_at,
            },
        )
        with session_scope(self.factory) as s:
            s.execute(stmt)


_STREAM_IDS: dict[str, InstrumentedAttribute[int]] = {
    "proposals": m.Proposal.id,
    "fills": m.Fill.id,
    "events": m.EventLog.id,
}


def _log_failure(step: str, exc: BaseException, **fields: Any) -> None:
    # The exception type only: a Telegram client error's text or repr can carry the bot token in a URL.
    log.error("relay.step_failed", step=step, error_type=type(exc).__name__, **fields)


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _ids(v: Any) -> tuple[int, ...]:
    if not isinstance(v, list):
        return ()
    return tuple(int(x) for x in v if isinstance(x, int) or (isinstance(x, str) and x.isdigit()))


def _ticker(s: Session, symbol_id: int | None) -> str:
    sym = s.get(m.Symbol, symbol_id) if symbol_id is not None else None
    return sym.ticker if sym is not None else "?"


def _proposal_view(s: Session, p: m.Proposal) -> ProposalView:
    spec: dict[str, Any] = p.order_spec if isinstance(p.order_spec, dict) else {}
    sig = s.get(m.Signal, p.signal_id)
    cfg = s.get(m.StrategyConfig, sig.strategy_config_id) if sig is not None else None
    cancelled = s.get(m.Order, p.cancel_order_id) if p.cancel_order_id is not None else None
    symbol_id = spec.get("symbol_id")
    if symbol_id is None and cancelled is not None:
        symbol_id = cancelled.symbol_id
    if symbol_id is None and sig is not None:
        symbol_id = sig.symbol_id
    intent: dict[str, Any] = sig.intent if sig is not None and isinstance(sig.intent, dict) else {}
    sizing: dict[str, Any] = p.sizing if isinstance(p.sizing, dict) else {}
    return ProposalView(
        proposal_id=p.id,
        kind=p.kind,
        status=p.status,
        ticker=_ticker(s, symbol_id),
        side=str(spec.get("side") or (cancelled.side if cancelled is not None else "")),
        order_type=str(spec.get("order_type") or (cancelled.order_type if cancelled is not None else "")),
        qty=p.qty,
        stop=_dec(spec.get("stop")),
        limit=_dec(spec.get("limit")),
        stop_loss=_dec(spec.get("stop_loss")),
        risk_usd=_dec(sizing.get("risk_dollars")),
        reason=str(spec.get("reason") or intent.get("reason") or ""),
        strategy_key=cfg.strategy_key if cfg is not None else "",
        created_at=p.created_at,
        expires_at=p.expires_at,
        decided_via=p.decided_via,
        error=p.error,
    )


def _fill_view(s: Session, f: m.Fill) -> FillView:
    order = s.get_one(m.Order, f.order_id)
    position_id = order.position_id
    if position_id is None and order.purpose == "entry":
        position_id = s.scalar(select(m.Position.id).where(m.Position.entry_order_id == order.id))
    pos = s.get(m.Position, position_id) if position_id is not None else None
    trade = None
    if order.purpose != "entry" and position_id is not None:
        trade = s.scalar(select(m.Trade).where(m.Trade.position_id == position_id))
    return FillView(
        fill_id=f.id,
        ticker=_ticker(s, order.symbol_id),
        side=order.side,
        purpose=order.purpose,
        qty=f.qty,
        price=f.price,
        ts=f.ts,
        reason=order.reason,
        position_id=position_id,
        stop_loss=pos.stop_loss if pos is not None else order.stop_loss,
        pnl=trade.pnl if trade is not None else None,
        pnl_r=trade.pnl_r if trade is not None else None,
    )


def _overlay_view(row: m.EventLog, data: dict[str, Any]) -> OverlayView:
    return OverlayView(
        decision=str(data.get("decision")),
        spy_return=_dec(data.get("spy_return")),
        prior_close=_dec(data.get("prior_close")),
        price=_dec(data.get("price")),
        position_ids=_ids(data.get("position_ids")),
        ts=row.ts,
        note=row.message,
    )
