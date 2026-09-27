"""The notification relay: new pending proposals, fills, error events and the overlay decision recorded
in the database go to Telegram, exactly once per row, surviving restarts (BR-32; SPEC §4.4, §6.2).

The engine and the cron jobs never call Telegram (SPEC §1: processes talk only through PostgreSQL). The
worker calls `pump()` every step; each pump reads the rows added since the stream's cursor
(`notify_cursors`), sends one message per row through the `Notifier` with a `dedupe_key` naming the row,
and then advances the cursor (send-then-advance: a crash in between re-reads the row, and the notifier's
dedupe key drops the second send, so nothing is sent twice).

A missing cursor starts at the table's current maximum id, so a first start sends no history.

Fix round 1 (P3 gauntlet):
- Out-of-order commits. Ids come from a sequence when a row is inserted, but become visible when its
  transaction commits, so a row can appear BELOW a cursor that already moved past it (the engine's
  transaction holds id N while a cron job commits N+1 and the relay pumps in between). Every pump
  therefore also re-scans a trailing window below the cursor: the relevant rows with
  id > cursor - RESCAN_IDS and ts >= now - RESCAN_SECONDS (2 minutes). The time bound is the one that
  matters: it covers any writer transaction that stays open up to 2 minutes (ours last milliseconds to
  seconds) however many ids other writers take meanwhile, and it keeps the re-sends to the last 2 minutes
  of rows. The id bound only keeps the query on the primary-key index. Rows already sent are no-ops:
  this instance remembers what it handled, rows whose dedupe key is already in `notifications` are
  dropped before rendering, and anything else is dropped by the notifier's dedupe key.
- A row that can't be rendered or sent is skipped on its own (a try per row): the exception type goes
  to structlog, one `error` event (source `notify.relay`, never relayed itself, to avoid loops) is
  written per row, and the cursor still moves past it.
- Catch-up cap. After a long outage a pump sends at most `telegram.relay_catchup_max` rows per stream
  (the newest ones) plus one summary line for the rest, but only for a real backlog: on the first pump
  after a (re)start, or when the oldest waiting row is older than BACKLOG_AGE. A burst of fresh rows in
  normal running (a flatten closing many positions) is sent in full. `relay_catchup_max = 0` means no
  cap. Rows skipped by the cap are never re-sent by the trailing re-scan: the highest skipped id is kept
  as the stream's re-scan floor (a `<stream>.floor` row in `notify_cursors`).

Fix round 2 (outage delivery, P3-T13 finding):
- Retry pass. A message the notifier recorded `failed` (surely not delivered: Telegram unreachable, a 5xx,
  a 429, a refusal) is sent again through the notifier with the SAME dedupe key, so it still goes out at
  most once: the notifier re-claims only `failed` rows with sends left (MAX_SEND_ATTEMPTS in all) and never
  re-sends `sent`, `unknown` or `sending`. Candidates: `failed` rows created within RETRY_WINDOW (30
  minutes), nothing of which went out. The text is the stored one (no buttons: the relay's own messages
  have none; a job message's buttons, e.g. the daily summary's journal buttons, are not rebuilt, and the
  journal page still takes the answer), silent for the relay's auto-mode proposals.
- Backoff. A row is due RETRY_BACKOFF (30 s) after its last send, and twice that after its second, while
  Telegram is not known to be back; once a send in the same pump succeeded, 30 s is enough. The last send
  is the latest `telegram` failure event naming the row (the notifier writes one per failed send), else
  the row's `created_at`.
- Original order. Each pump first walks the waiting messages and the pending proposals together, oldest
  first (`created_at`; a proposal before a message of the same time, as a normal pump sends them). A
  message whose failure was transient (not a 4xx refusal other than 429) and that is not due yet, or whose
  re-send failed again, means Telegram is still away: everything after it in the walk waits, and so do the
  cursor-driven streams (their cursors stay put, so nothing is lost). So after an outage the messages go
  out in the order they were first tried, and while it lasts only one probe per backoff hits Telegram. A
  proposal older than the waiting messages is still offered every pump (it needs a decision; the bot
  starts it afresh after a surely-unsent failure). While the streams wait, the trailing re-scan window is
  widened to reach back to the last pump that ran them.
- The trailing re-scan no longer treats a `failed` key as done: it drops only keys whose status is `sent`,
  `unknown` or `sending`, and leaves a `failed` key to the retry pass (which applies the backoff and the
  order) rather than sending it straight away.
"""

import dataclasses
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import structlog
from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from trader.adapters.telegram.types import ProposalMessenger
from trader.db import models as m
from trader.db.session import session_scope
from trader.events import log_event
from trader.logging_mirror import MIRROR_SOURCE_PREFIX
from trader.market.clock import Clock
from trader.notify.messages import RESET_MESSAGE
from trader.notify.notifier import MAX_SEND_ATTEMPTS
from trader.notify.types import (
    AlertView,
    FillView,
    MessageKind,
    Notifier,
    OutboundMessage,
    OverlayView,
    Renderer,
)
from trader.notify.views import proposal_view
from trader.settings_store import RuntimeSettings

STREAMS = ("proposals", "fills", "events")

OVERLAY_SOURCE = "strategy.spy_overlay"
ALERT_LEVELS = ("error", "critical")
RELAY_ERROR_SOURCE = "notify.relay"
# The notifier's own failure events and the relay's own row failures: relaying them could loop. Sources
# starting with MIRROR_SOURCE_PREFIX (`log.`, the error-log mirror's rows) are never relayed either: the
# events that should alert are already written with log_event (P5-T10).
NEVER_RELAYED = ("telegram", RELAY_ERROR_SOURCE)
# A kill-switch reset (KillSwitches.reset, level warning) is relayed as a `kill_switch` confirmation (P5-T10).
KILLSWITCH_SOURCE = "killswitch"
RESET_LIKE = "kill switch % reset"  # SQL twin of messages.RESET_MESSAGE
SUMMARY_SOURCE = "relay"
SKIPPED_NOUN = {"proposals": "auto-mode proposals", "fills": "fills", "events": "alerts"}
DEDUPE_PREFIX = {"proposals": "proposal", "fills": "fill", "events": "event"}

RESCAN_SECONDS = 120  # the trailing window re-scanned below the cursor for late commits
RESCAN_IDS = 1000  # ... bounded by id too, so the query stays on the primary-key index
BACKLOG_AGE = timedelta(minutes=5)  # waiting rows older than this are a backlog (the cap applies)
FLOOR_SUFFIX = ".floor"

RETRY_WINDOW = timedelta(minutes=30)  # failed messages older than this are not retried
RETRY_BACKOFF = timedelta(seconds=30)  # at least this long after a message's last send
RETRY_BATCH = 200  # at most this many waiting messages are read per pump
NOTIFIER_SOURCE = "telegram"  # the notifier's failure events: data.notification_id names the row
_REFUSAL = re.compile(r"4\d\d\b")  # a stored error that starts with a 4xx: Telegram refused the message

log = structlog.get_logger("notify.relay")


@dataclass(frozen=True, slots=True)
class RelayReport:
    proposals: int
    fills: int
    events: int
    closed: int
    skipped: int
    retried: int = 0  # failed messages delivered by the retry pass
    held: bool = False  # Telegram presumed away: the streams waited this pump


@dataclass(frozen=True, slots=True)
class _Waiting:
    """A `failed` notification the retry pass may send again."""

    id: int
    created_at: datetime
    kind: str
    text: str
    dedupe_key: str
    attempts: int
    last_try: datetime
    transient: bool  # unreachable, 5xx or 429 (not a refusal): its failure says Telegram is away


def _transient_error(error: str | None) -> bool:
    """The notifier stores `<status> <description>` for an HTTP error and the bare description for a
    network error: anything but a 4xx refusal (429 excepted) is transient."""
    return error is None or error.startswith("429") or _REFUSAL.match(error) is None


def _backoff(attempts: int, up: bool) -> timedelta:
    """How long after its last send a waiting message is due: 30 s, 60 s after its second send while
    Telegram is not known to be back (so a longer outage still leaves it a send), 30 s once it is."""
    return RETRY_BACKOFF if up else RETRY_BACKOFF * 2 ** max(attempts - 1, 0)


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


@dataclass(slots=True)
class _Batch:
    """One stream's work for this pump."""

    top: int | None  # the highest id read above the cursor (relevant or not); the cursor ends here
    messages: list[tuple[int, OutboundMessage]] = field(default_factory=list)  # new rows, in id order
    rescan: list[tuple[int, OutboundMessage]] = field(default_factory=list)  # late rows below the cursor
    skipped: int = 0  # relevant rows past the catch-up cap, not sent
    last_skipped: int | None = None
    failed: list[tuple[int, str]] = field(default_factory=list)  # (row id, exception type): unrenderable
    handled: set[int] = field(default_factory=set)  # every row id this pump dealt with


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
        self._pumped: set[str] = set()  # streams this instance has pumped (the first is a catch-up)
        self._handled: dict[str, set[int]] = {s: set() for s in STREAMS}  # rows sent/tried, for re-scans
        self._tried: dict[int, datetime] = {}  # notification id -> this instance's last re-send of it
        self._streams_at: datetime | None = (
            None  # when the streams last ran (widens the re-scan after a hold)
        )

    async def pump(self) -> RelayReport:
        """Relay everything new once. Each step runs in its own try block, so one failing step never stops
        the others; nothing here raises."""
        proposals = fills = events = closed = skipped = retried = 0
        held = False
        try:
            proposals, retried, held = await self._backlog()
        except Exception as exc:
            _log_failure("backlog", exc)
        if not held:
            try:
                sent, missed = await self._relay_stream("proposals")
                proposals += sent
                skipped += missed
            except Exception as exc:
                _log_failure("auto_proposals", exc)
            try:
                fills, missed = await self._relay_stream("fills")
                skipped += missed
            except Exception as exc:
                _log_failure("fills", exc)
            try:
                events, missed = await self._relay_stream("events")
                skipped += missed
            except Exception as exc:
                _log_failure("events", exc)
            self._streams_at = self.clock.now()
        try:
            closed = await self.messenger.sync_closed()
        except Exception as exc:
            _log_failure("sync_closed", exc)
        return RelayReport(proposals, fills, events, closed, skipped, retried, held)

    # --- step 1: pending proposals (to the bot, which adds the buttons) and failed messages, in order ----
    async def _backlog(self) -> tuple[int, int, bool]:
        """Offer every pending proposal to the bot and re-send the due failed messages, oldest first.
        Returns (proposals sent, messages re-sent, held): held when Telegram is presumed away, so what
        comes after in the walk, and the streams, wait for a later pump."""
        now = self.clock.now()
        with session_scope(self.factory) as s:
            pending = s.execute(
                select(m.Proposal.id, m.Proposal.created_at)
                .where(m.Proposal.run_id == self.run_id, m.Proposal.status == "pending")
                .order_by(m.Proposal.id)
            ).all()
            waiting = self._waiting(s, now)
        walk: list[tuple[datetime, int, int, _Waiting | None]] = [
            (created_at, 0, proposal_id, None) for proposal_id, created_at in pending
        ]
        walk += [(w.created_at, 1, w.id, w) for w in waiting]
        walk.sort(key=lambda item: item[:3])
        sent = retried = 0
        up = held = False
        for _, _, item_id, w in walk:
            if held:
                break
            if w is None:
                try:
                    if await self.messenger.send_proposal(item_id):
                        sent += 1
                        up = True
                except Exception as exc:  # one bad proposal never blocks the others
                    _log_failure("send_proposal", exc, proposal_id=item_id)
                continue
            if now < w.last_try + _backoff(w.attempts, up):
                held = w.transient  # not due: Telegram is presumed still away
                continue
            status = await self._resend(w)
            if status == "sent":
                retried += 1
                up = True
            elif w.transient or status != "failed":
                held = True  # it failed again (or may have): Telegram is still away
        return sent, retried, held

    def _waiting(self, s: Session, now: datetime) -> list[_Waiting]:
        """The `failed` messages the retry pass may send again, oldest first, with their last send."""
        n = m.Notification
        rows = list(
            s.scalars(
                select(n)
                .where(
                    n.status == "failed",
                    n.attempts < MAX_SEND_ATTEMPTS,
                    n.message_ids.is_(None),
                    n.dedupe_key.is_not(None),
                    n.created_at >= now - RETRY_WINDOW,
                )
                .order_by(n.created_at, n.id)
                .limit(RETRY_BATCH)
            )
        )
        self._tried = {k: v for k, v in self._tried.items() if k in {r.id for r in rows}}
        if not rows:
            return []
        e = m.EventLog
        row_key = e.data["notification_id"].astext
        tried: dict[str, datetime] = {
            key: ts
            for key, ts in s.execute(
                select(row_key, func.max(e.ts))
                .where(
                    e.source == NOTIFIER_SOURCE,
                    e.ts >= now - RETRY_WINDOW - RETRY_BACKOFF,
                    row_key.in_([str(r.id) for r in rows]),
                )
                .group_by(row_key)
            ).tuples()
        }
        out: list[_Waiting] = []
        for r in rows:
            last = max(
                t for t in (r.created_at, tried.get(str(r.id)), self._tried.get(r.id)) if t is not None
            )
            out.append(
                _Waiting(
                    r.id,
                    r.created_at,
                    r.kind,
                    r.text,
                    cast(str, r.dedupe_key),
                    r.attempts,
                    last,
                    _transient_error(r.error),
                )
            )
        return out

    async def _resend(self, w: _Waiting) -> str | None:
        """Send a waiting message again with its own dedupe key; returns the row's status afterwards."""
        msg = OutboundMessage(
            kind=cast(MessageKind, w.kind),
            text=w.text,
            dedupe_key=w.dedupe_key,
            silent=w.dedupe_key.startswith(DEDUPE_PREFIX["proposals"] + ":"),
        )
        self._tried[w.id] = self.clock.now()
        try:
            await self.notifier.send(msg)
        except Exception as exc:  # the notifier never raises by contract
            _log_failure("retry", exc, notification_id=w.id)
        with session_scope(self.factory) as s:
            status = s.scalar(select(m.Notification.status).where(m.Notification.id == w.id))
        if status == "sent":
            log.info("relay.retry_delivered", notification_id=w.id, dedupe_key=w.dedupe_key)
            self._tried.pop(w.id, None)
        return status

    # --- the cursor-driven streams ------------------------------------------------------------------------
    async def _relay_stream(self, stream: str) -> tuple[int, int]:
        """Scan one stream, send its messages (send, then advance), move the cursor past everything read,
        then send the late rows found below the cursor. Returns (new-row messages sent, rows skipped by
        the catch-up cap)."""
        with session_scope(self.factory) as s:
            batch = self._scan(s, stream)
        for row_id, error_type in batch.failed:
            self._record_row_failure(stream, row_id, error_type)
        if batch.skipped and batch.last_skipped is not None:
            summary = dataclasses.replace(
                self._summary(stream, batch.skipped),
                dedupe_key=f"relay:{stream}:skipped-through:{batch.last_skipped}",
            )
            await self._send(stream, summary, batch.last_skipped)
            self._advance(stream, batch.last_skipped)
            self._advance(stream + FLOOR_SUFFIX, batch.last_skipped)
        sent = 0
        for row_id, msg in batch.messages:
            if await self._send(stream, msg, row_id):
                sent += 1
            self._advance(stream, row_id)
        if batch.top is not None:
            self._advance(stream, batch.top)
        for row_id, msg in batch.rescan:
            if await self._send(stream, msg, row_id):  # a no-op in the notifier if it was sent before
                log.debug("relay.rescan_offered", stream=stream, row_id=row_id)
        self._pumped.add(stream)
        self._handled[stream] |= batch.handled
        return sent, batch.skipped

    def _scan(self, s: Session, stream: str) -> _Batch:
        id_col, ts_col = _STREAM_IDS[stream], _STREAM_TS[stream]
        relevant = self._relevant(stream)
        cap = self.settings().telegram_relay_catchup_max
        now = self.clock.now()
        start = self._cursor(s, stream)
        floor = self._floor(s, stream, start)

        # rows above the cursor
        top = s.scalar(select(func.max(id_col)).where(id_col > start))
        batch = _Batch(top)
        if top is not None:
            in_window = [id_col > start, id_col <= top, *relevant]
            total = s.scalar(select(func.count(id_col)).where(*in_window)) or 0
            ids: list[int]
            if cap > 0 and total > cap and self._is_backlog(s, stream, ts_col, in_window, now):
                ids = sorted(s.scalars(select(id_col).where(*in_window).order_by(id_col.desc()).limit(cap)))
                batch.skipped = total - len(ids)
                older = [id_col < ids[0]] if ids else []
                batch.last_skipped = s.scalar(select(func.max(id_col)).where(*in_window, *older))
            else:
                ids = list(s.scalars(select(id_col).where(*in_window).order_by(id_col)))
            batch.handled.update(ids)
            batch.messages = self._render_all(s, stream, ids, batch)

        # late commits below the cursor (trailing window), minus what was handled or already sent
        low = max(start - RESCAN_IDS, floor)
        since = now - timedelta(seconds=RESCAN_SECONDS)
        if self._streams_at is not None:  # the streams were held: reach back to their last run
            since = min(since, self._streams_at - timedelta(seconds=RESCAN_SECONDS))
        late = [
            i
            for i in s.scalars(
                select(id_col)
                .where(id_col > low, id_col <= start, ts_col >= since, *relevant)
                .order_by(id_col)
            )
            if i not in self._handled[stream]
        ]
        if late:
            keys = {f"{DEDUPE_PREFIX[stream]}:{i}": i for i in late}
            known = set(
                s.scalars(select(m.Notification.dedupe_key).where(m.Notification.dedupe_key.in_(keys)))
            )
            # A key that is sent, maybe sent or being sent is done. A `failed` key is not done, but it
            # belongs to the retry pass (which ran before the streams, with its backoff and order), so it
            # is not sent from here either: only a key the notifier has never seen is.
            late = [i for k, i in keys.items() if k not in known]
            batch.handled.update(late)
            batch.rescan = self._render_all(s, stream, late, batch)
        self._handled[stream] = {i for i in self._handled[stream] if i > low}
        return batch

    def _is_backlog(
        self,
        s: Session,
        stream: str,
        ts_col: InstrumentedAttribute[Any],
        in_window: Sequence[ColumnElement[bool]],
        now: Any,
    ) -> bool:
        """A real backlog: the first pump of this instance (a restart), or rows waiting longer than
        BACKLOG_AGE (the worker was stuck or the database was away)."""
        if stream not in self._pumped:
            return True
        oldest = s.scalar(select(func.min(ts_col)).where(*in_window))
        return oldest is not None and oldest < now - BACKLOG_AGE

    def _relevant(self, stream: str) -> list[ColumnElement[bool]]:
        if stream == "proposals":
            # Proposals approved by auto mode (BR-30/BR-32): a silent message without buttons. Pending
            # ones belong to the messenger, and `auto_flatten_on_expiry` results show up as fills.
            return [m.Proposal.run_id == self.run_id, m.Proposal.decided_by == "auto"]
        if stream == "fills":
            return [m.Fill.run_id == self.run_id]
        e = m.EventLog
        return [
            or_(e.run_id.is_(None), e.run_id == self.run_id),
            e.source.not_in(NEVER_RELAYED),
            ~e.source.startswith(MIRROR_SOURCE_PREFIX, autoescape=True),  # mirrored log lines (P5-T14)
            or_(
                e.level.in_(ALERT_LEVELS),
                and_(e.source == OVERLAY_SOURCE, e.data.has_key("decision")),
                and_(e.source == KILLSWITCH_SOURCE, e.message.like(RESET_LIKE)),  # reset confirmation
            ),
        ]

    def _render_all(
        self, s: Session, stream: str, ids: Sequence[int], batch: _Batch
    ) -> list[tuple[int, OutboundMessage]]:
        """Render each row in its own savepoint and try: a row that fails is recorded in `batch.failed`
        and skipped, and never stops the rows after it."""
        out: list[tuple[int, OutboundMessage]] = []
        for row_id in ids:
            try:
                with s.begin_nested():
                    msg = self._render(s, stream, row_id)
            except Exception as exc:
                batch.failed.append((row_id, type(exc).__name__))
                continue
            if msg is not None:
                out.append((row_id, dataclasses.replace(msg, dedupe_key=f"{DEDUPE_PREFIX[stream]}:{row_id}")))
        return out

    def _render(self, s: Session, stream: str, row_id: int) -> OutboundMessage | None:
        if stream == "proposals":
            p = s.get_one(m.Proposal, row_id)
            return dataclasses.replace(self.render.proposal(proposal_view(s, p), ()), silent=True)
        if stream == "fills":
            return self.render.fill(_fill_view(s, s.get_one(m.Fill, row_id)))
        row = s.get_one(m.EventLog, row_id)
        data = row.data if isinstance(row.data, dict) else {}
        if row.source == OVERLAY_SOURCE and "decision" in data:
            return self.render.overlay(_overlay_view(row, data))
        is_reset = row.source == KILLSWITCH_SOURCE and RESET_MESSAGE.fullmatch(row.message) is not None
        if row.level in ALERT_LEVELS or is_reset:
            return self.render.alert(
                AlertView(
                    kind=alert_kind(row.source, row.level, row.message),
                    level=row.level,
                    source=row.source,
                    message=row.message,
                    ts=row.ts,
                    data=data,
                )
            )
        return None  # matched the SQL filter only through a non-object `data`: nothing to relay

    def _record_row_failure(self, stream: str, row_id: int, error_type: str) -> None:
        """One `error` event per unrenderable row (source notify.relay, never relayed), however many pumps
        or relays meet it. Best effort: never raises."""
        key = f"{DEDUPE_PREFIX[stream]}:{row_id}"
        log.error("relay.row_failed", stream=stream, row_id=row_id, error_type=error_type)
        try:
            with session_scope(self.factory) as s:
                exists = s.scalar(
                    select(m.EventLog.id)
                    .where(m.EventLog.source == RELAY_ERROR_SOURCE, m.EventLog.data["row"].astext == key)
                    .limit(1)
                )
                if exists is None:
                    log_event(
                        s,
                        self.clock,
                        "error",
                        RELAY_ERROR_SOURCE,
                        f"could not relay {stream} row {row_id} ({error_type}); skipped",
                        {"stream": stream, "row": key, "row_id": row_id, "error_type": error_type},
                        run_id=self.run_id,
                    )
        except Exception as exc:
            _log_failure("record_row_failure", exc, stream=stream, row_id=row_id)

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
        logged (the exception type only) and the row is not retried by this instance (the cursor still
        advances)."""
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

    def _floor(self, s: Session, stream: str, start: int) -> int:
        """The re-scan floor: rows at or below it are never re-scanned (history before the cursor was
        created, and rows skipped by the catch-up cap). Created at the cursor when missing."""
        name = stream + FLOOR_SUFFIX
        row = s.get(m.NotifyCursor, name)
        if row is not None:
            return row.last_id
        s.execute(
            insert(m.NotifyCursor)
            .values(stream=name, last_id=start, updated_at=self.clock.now())
            .on_conflict_do_nothing(index_elements=["stream"])
        )
        return s.get_one(m.NotifyCursor, name, populate_existing=True).last_id

    def _advance(self, stream: str, last_id: int) -> None:
        """Move a cursor forward to `last_id` (never back, so two relays can't rewind each other)."""
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
_STREAM_TS: dict[str, InstrumentedAttribute[Any]] = {
    "proposals": m.Proposal.created_at,
    "fills": m.Fill.ts,
    "events": m.EventLog.ts,
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
