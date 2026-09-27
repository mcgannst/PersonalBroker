"""Phase 3 notification contracts: outbound messages, the views they are rendered from, and the Notifier
and Renderer protocols (SPEC §4.4).

Views are frozen value objects built from database rows; the Renderer turns them into messages with pure
functions (P3-T4). Prices and money are Decimal, times are UTC datetimes (rendered in Mountain Time).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from trader.market.sessions import SessionPhase

MessageKind = Literal[
    "proposal",
    "fill",
    "stop_hit",
    "flatten",
    "overlay",
    "kill_switch",
    "job_failure",
    "token_failure",
    "escalation",
    "alert",
    "daily_summary",
    "weekly_report",
    "premarket_brief",
    "preopen",
    "checkin",
    "reply",
]


@dataclass(frozen=True, slots=True)
class Button:
    """One inline-keyboard button. `callback_data` is at most 64 bytes (Telegram's limit)."""

    text: str
    callback_data: str


# Inline keyboard: rows of buttons.
Buttons = tuple[tuple[Button, ...], ...]


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """A message to send. `text` is Telegram HTML (every dynamic string escaped). A message whose
    `dedupe_key` was already sent is not sent again. `silent` sends without a notification sound."""

    kind: MessageKind
    text: str
    buttons: Buttons = ()
    dedupe_key: str | None = None
    silent: bool = False


@dataclass(frozen=True, slots=True)
class ProposalView:
    proposal_id: int
    kind: str  # entry | stop | exit | cancel (ProposalKind)
    status: str  # proposals.status
    ticker: str
    side: str  # buy | sell
    order_type: str  # market | limit | stop | stop_limit
    qty: int
    stop: Decimal | None
    limit: Decimal | None
    stop_loss: Decimal | None
    risk_usd: Decimal | None
    reason: str
    strategy_key: str
    created_at: datetime
    expires_at: datetime
    decided_via: str | None  # telegram | web | auto
    error: str | None


@dataclass(frozen=True, slots=True)
class FillView:
    fill_id: int
    ticker: str
    side: str
    purpose: str  # entry | stop | exit (the order's purpose)
    qty: int
    price: Decimal
    ts: datetime
    reason: str  # the order's reason, e.g. "flatten_close"
    position_id: int | None
    stop_loss: Decimal | None
    pnl: Decimal | None  # set when the fill closed a position
    pnl_r: Decimal | None


@dataclass(frozen=True, slots=True)
class OverlayView:
    decision: str  # hold | exit
    spy_return: Decimal | None  # None when spy_overlay held for lack of data
    prior_close: Decimal | None
    price: Decimal | None
    position_ids: tuple[int, ...]
    ts: datetime
    note: str  # the event message


@dataclass(frozen=True, slots=True)
class AlertView:
    kind: MessageKind  # kill_switch | job_failure | token_failure | escalation | alert
    level: str  # event_log.level
    source: str  # event_log.source
    message: str
    ts: datetime
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PositionLine:
    position_id: int
    ticker: str
    qty: int
    entry: Decimal  # positions.avg_price
    last: Decimal | None  # None when no quote (shown as n/a)
    stop: Decimal | None  # the working stop order's price, else the position's stop_loss
    unrealized_pnl: Decimal | None  # None when `last` is None
    unprotected_seconds: int  # including the running interval while unprotected
    stop_working: bool  # False: `stop` is the position's stop_loss, shown "(no stop order)"


@dataclass(frozen=True, slots=True)
class StatusView:
    now: datetime
    phase: SessionPhase
    session_date: date
    next_event_key: str | None  # None: no unfired event left today
    next_event_at: datetime | None
    approval_mode: str  # manual | auto
    blocking_switches: tuple[str, ...]
    token_ok: bool
    token_age_hours: float | None
    token_error: str | None
    heartbeat_age_seconds: float | None  # None: no worker heartbeat row
    positions: tuple[PositionLine, ...]
    pending_count: int


@dataclass(frozen=True, slots=True)
class PnlView:
    session_date: date
    realized_today: Decimal
    unrealized: Decimal
    week_to_date: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal


@dataclass(frozen=True, slots=True)
class TradeLine:
    ticker: str
    qty: int
    entry: Decimal
    exit: Decimal
    pnl: Decimal
    pnl_r: Decimal | None
    exit_reason: str


@dataclass(frozen=True, slots=True)
class DailySummaryView:
    session_date: date
    trades: tuple[TradeLine, ...]
    realized_pnl: Decimal
    fees: Decimal
    equity: Decimal
    drawdown_pct: Decimal
    open_positions: tuple[PositionLine, ...]  # non-empty means STILL OPEN (BR-42)
    decisions: int
    avg_decision_seconds: float | None  # None when nothing was decided
    unprotected_seconds: int
    blocking_switches: tuple[str, ...]
    archive: Mapping[str, int]  # candle-archive counts, e.g. {"5m": 812, "1m": 8190}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    level: Literal["info", "warning", "error"]
    detail: str


@dataclass(frozen=True, slots=True)
class PreopenView:
    session_date: date
    approval_mode: str
    checks: tuple[Check, ...]


class Notifier(Protocol):
    async def send(self, msg: OutboundMessage) -> None:
        """Send a message. Never raises; a message whose dedupe_key was already sent is skipped."""
        ...


class Renderer(Protocol):
    """Every Telegram message format (P3-T4 implements it as MessageRenderer)."""

    def proposal(self, v: ProposalView, buttons: Buttons) -> OutboundMessage: ...

    def proposal_closed(self, v: ProposalView, final_status: str, via: str | None) -> str: ...

    def fill(self, v: FillView) -> OutboundMessage: ...

    def overlay(self, v: OverlayView) -> OutboundMessage: ...

    def alert(self, v: AlertView) -> OutboundMessage: ...

    def daily_summary(self, v: DailySummaryView, buttons: Buttons) -> OutboundMessage: ...

    def journal_answered(self, session_date: date, rules_followed: bool) -> OutboundMessage: ...

    def premarket_brief(self, session_date: date, brief: str) -> OutboundMessage: ...

    def preopen(self, v: PreopenView) -> OutboundMessage: ...

    def checkin(self, v: StatusView, at_label: str) -> OutboundMessage: ...

    def status(self, v: StatusView) -> OutboundMessage: ...

    def positions(self, lines: Sequence[PositionLine], now: datetime) -> OutboundMessage: ...

    def pnl(self, v: PnlView) -> OutboundMessage: ...

    def pause_confirm(self, buttons: Buttons) -> OutboundMessage: ...

    def help(self) -> OutboundMessage: ...

    def reply(self, text: str) -> OutboundMessage: ...

    def weekly_link(self, week_ending: date) -> OutboundMessage: ...
