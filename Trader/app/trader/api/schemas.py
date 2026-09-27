"""The API's request and response models (SPEC §11), the single source for the TypeScript mirror
`web/src/api/types.ts` (T2 writes it from the plan; T18 checks the two agree).

Rules: pydantic v2, frozen; JSON field names are the Python names; `Decimal` serialises as a JSON string,
`datetime` as ISO 8601 in UTC (`...Z`), `date` as `YYYY-MM-DD`. A `| None` field is always present in the
JSON (null when unknown). Nothing here reads the database: the pure mappings `ProposalOut.from_view` and
`PositionOut.from_line` turn the Phase 3 views into responses.
"""

import datetime as dt
import warnings
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from trader.db import models as m
from trader.market.sessions import SessionPhase
from trader.notify.types import PositionLine, ProposalView


def _utc(value: datetime) -> datetime:
    """Aware datetimes in UTC (a naive one is taken as UTC), so the JSON always ends in `Z`."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_utc)]

TimelineStatus = Literal["done", "failed", "missed", "running", "skipped", "next", "upcoming"]
FieldKind = Literal["decimal", "integer", "number", "boolean", "string", "enum", "string_list", "enum_list"]
Topic = Literal[
    "proposals",
    "orders",
    "fills",
    "positions",
    "trades",
    "candidates",
    "killswitch",
    "events",
    "journal",
    "jobs",
    "settings",
    "strategies",
    "system",
]
ManualJob = Literal["nightly", "premarket", "preopen", "postclose", "token-refresh"]

TotpCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
Password = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class ApiModel(BaseModel):
    """Base of every API model: frozen, unknown input fields ignored."""

    model_config = ConfigDict(frozen=True)


class Items[T](ApiModel):
    items: list[T]


# --- errors and misc ------------------------------------------------------------------------------------


class FieldError(ApiModel):
    loc: list[str | int]
    msg: str


class ErrorBody(ApiModel):
    code: str
    message: str
    fields: list[FieldError] | None = None
    request_id: str | None = None


class ErrorOut(ApiModel):
    error: ErrorBody


class OkOut(ApiModel):
    ok: bool = True
    message: str | None = None


# --- auth -----------------------------------------------------------------------------------------------


class LoginIn(ApiModel):
    username: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    password: Password
    totp: TotpCode | None = None


class UserOut(ApiModel):
    username: str
    totp_enabled: bool


class SessionOut(ApiModel):
    user: UserOut
    csrf_token: str
    expires_at: UtcDateTime


class PasswordChangeIn(ApiModel):
    current_password: Password
    new_password: Annotated[str, StringConstraints(min_length=8, max_length=200)]  # auth.MIN_PASSWORD_CHARS
    totp: str | None = None


class TotpSetupIn(ApiModel):
    password: Password


class TotpSetupOut(ApiModel):
    secret: str
    otpauth_uri: str


class TotpConfirmIn(ApiModel):
    code: TotpCode


class TotpDisableIn(ApiModel):
    password: Password
    code: TotpCode


# --- health and meta ------------------------------------------------------------------------------------


class HealthOut(ApiModel):
    status: Literal["ok", "degraded", "down"]
    time: UtcDateTime
    version: str
    db_ok: bool
    db_latency_ms: int | None = None
    token_ok: bool
    token_age_hours: float | None = None
    worker_ok: bool
    worker_phase: str | None = None
    worker_age_seconds: float | None = None


class MetaOut(ApiModel):
    server_time: UtcDateTime
    app_env: str
    version: str
    tz_display: str
    tz_offset_minutes: int
    tz_iana_version: str | None = None
    public_base_url: str


# --- status parts ---------------------------------------------------------------------------------------


class TokenOut(ApiModel):
    ok: bool
    seeded: bool
    age_hours: float | None = None
    expires_at: UtcDateTime | None = None
    last_refresh_at: UtcDateTime | None = None
    error: str | None = None


class WorkerOut(ApiModel):
    ok: bool
    phase: str | None = None
    beat_at: UtcDateTime | None = None
    age_seconds: float | None = None
    session_date: date | None = None
    pid: int | None = None
    host: str | None = None
    detail: dict[str, Any] | None = None


class EventOut(ApiModel):
    id: int
    ts: UtcDateTime
    level: str
    source: str
    message: str
    data: dict[str, Any] | None = None


class KillSwitchOut(ApiModel):
    switch: str
    label: str
    tripped: bool
    tripped_at: UtcDateTime | None = None
    value: Decimal | None = None
    threshold: Decimal | None = None
    automatic: bool
    needs_web_reset: bool
    clears: str


class KillSwitchEventOut(ApiModel):
    id: int
    switch: str
    session_date: date
    tripped_at: UtcDateTime
    value: Decimal | None = None
    threshold: Decimal | None = None
    reset_at: UtcDateTime | None = None
    reset_reason: str | None = None
    reset_by: str | None = None


# --- trading --------------------------------------------------------------------------------------------


class ProposalOut(ApiModel):
    id: int
    kind: str  # entry | stop | exit | cancel
    status: str  # a ProposalStatus
    ticker: str
    side: str
    order_type: str
    qty: int
    stop: Decimal | None = None
    limit: Decimal | None = None
    stop_loss: Decimal | None = None
    risk_usd: Decimal | None = None
    reason: str
    strategy_key: str
    created_at: UtcDateTime
    expires_at: UtcDateTime
    decided_at: UtcDateTime | None = None
    decided_via: str | None = None
    decided_by: str | None = None
    decision_latency_ms: int | None = None
    error: str | None = None
    order_id: int | None = None
    position_id: int | None = None

    @classmethod
    def from_view(cls, v: ProposalView, p: m.Proposal) -> Self:
        """The response for one proposal: the shared view (the same numbers Telegram shows) plus the row's
        decision details. Pure: reads nothing."""
        return cls(
            id=v.proposal_id,
            kind=v.kind,
            status=v.status,
            ticker=v.ticker,
            side=v.side,
            order_type=v.order_type,
            qty=v.qty,
            stop=v.stop,
            limit=v.limit,
            stop_loss=v.stop_loss,
            risk_usd=v.risk_usd,
            reason=v.reason,
            strategy_key=v.strategy_key,
            created_at=v.created_at,
            expires_at=v.expires_at,
            decided_at=v.decided_at if v.decided_at is not None else p.decided_at,
            decided_via=v.decided_via,
            decided_by=p.decided_by,
            decision_latency_ms=p.decision_latency_ms,
            error=v.error,
            order_id=p.order_id,
            position_id=p.position_id,
        )


class PositionOut(ApiModel):
    id: int
    symbol_id: int
    ticker: str
    strategy_key: str
    status: Literal["open", "closed"]
    qty: int
    entry: Decimal
    last: Decimal | None = None
    stop: Decimal | None = None
    stop_working: bool
    unrealized_pnl: Decimal | None = None
    unprotected_seconds: int
    opened_at: UtcDateTime
    closed_at: UtcDateTime | None = None

    @classmethod
    def from_line(cls, line: PositionLine, *, symbol_id: int, strategy_key: str, opened_at: datetime) -> Self:
        """An open position from the shared `/positions` line (the same numbers Telegram shows)."""
        return cls(
            id=line.position_id,
            symbol_id=symbol_id,
            ticker=line.ticker,
            strategy_key=strategy_key,
            status="open",
            qty=line.qty,
            entry=line.entry,
            last=line.last,
            stop=line.stop,
            stop_working=line.stop_working,
            unrealized_pnl=line.unrealized_pnl,
            unprotected_seconds=line.unprotected_seconds,
            opened_at=opened_at,
            closed_at=None,
        )


class OrderOut(ApiModel):
    id: int
    proposal_id: int | None = None
    position_id: int | None = None
    symbol_id: int
    ticker: str
    side: str
    order_type: str
    purpose: str
    qty: int
    stop_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_loss: Decimal | None = None
    tif: str
    status: str
    reason: str
    session_date: date
    submitted_at: UtcDateTime
    closed_at: UtcDateTime | None = None
    cancel_reason: str | None = None


class FillOut(ApiModel):
    id: int
    order_id: int
    ticker: str
    side: str
    purpose: str
    ts: UtcDateTime
    qty: int
    price: Decimal
    fees: dict[str, Any]
    quote_snapshot: dict[str, Any]
    slippage: Decimal


class TradeOut(ApiModel):
    id: int
    position_id: int
    symbol_id: int
    ticker: str
    strategy_key: str
    session_date: date
    entry_price: Decimal
    exit_price: Decimal
    qty: int
    pnl: Decimal
    pnl_r: Decimal | None = None
    planned_risk: Decimal | None = None
    exit_reason: str
    slippage_total: Decimal
    fees_total: Decimal
    opened_at: UtcDateTime
    closed_at: UtcDateTime


class SignalOut(ApiModel):
    id: int
    strategy_key: str
    config_revision: int
    config_version: str
    event_key: str
    ts: UtcDateTime
    intent: dict[str, Any]
    evidence: dict[str, Any]


class CandleOut(ApiModel):
    start: UtcDateTime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class PositionDetailOut(ApiModel):
    position: PositionOut
    trade: TradeOut | None = None
    signal: SignalOut | None = None
    proposals: list[ProposalOut]
    orders: list[OrderOut]
    fills: list[FillOut]
    candles: list[CandleOut]
    chart_error: str | None = None


class CandidateOut(ApiModel):
    id: int
    session_date: date
    strategy_key: str
    symbol_id: int
    ticker: str
    rvol: Decimal | None = None
    rank: int | None = None
    passed: bool
    reject_reason: str | None = None
    candle: dict[str, Any] | None = None
    data: dict[str, Any] | None = None


class HeadlineOut(ApiModel):
    ts: UtcDateTime | None = None
    title: str
    source: str | None = None
    url: str | None = None


class CatalystOut(ApiModel):
    symbol_id: int
    ticker: str
    session_date: date
    catalyst_type: str
    direction: str
    quality: int | None = None
    confirmed: bool | None = None
    reason: str | None = None
    gap_pct: Decimal | None = None
    earnings_date: date | None = None
    headlines: list[HeadlineOut]
    model: str | None = None
    classified_at: UtcDateTime | None = None


class CandidatesOut(ApiModel):
    session_date: date
    brief: str | None = None
    catalysts: list[CatalystOut]
    ranking: list[CandidateOut]


# --- dashboard ------------------------------------------------------------------------------------------


class SessionInfoOut(ApiModel):
    date: dt.date
    phase: SessionPhase
    is_session: bool
    open_at: UtcDateTime | None = None
    close_at: UtcDateTime | None = None


class TimelineItemOut(ApiModel):
    key: str
    label: str
    kind: Literal["job", "event"]
    at: UtcDateTime
    status: TimelineStatus
    detail: str | None = None


class PnlOut(ApiModel):
    session_date: date
    realized_today: Decimal
    unrealized: Decimal | None = None
    unrealized_partial: bool
    week_to_date: Decimal
    equity: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal


class DashboardOut(ApiModel):
    server_time: UtcDateTime
    run_id: int
    session: SessionInfoOut
    approval_mode: Literal["manual", "auto"]
    telegram_configured: bool
    timeline: list[TimelineItemOut]
    pending: list[ProposalOut]
    positions: list[PositionOut]
    pnl: PnlOut
    killswitches: list[KillSwitchOut]
    events: list[EventOut]
    token: TokenOut
    worker: WorkerOut
    candidates_top: list[CandidateOut]
    candidates_count: int


# --- decisions ------------------------------------------------------------------------------------------


class DecisionOut(ApiModel):
    proposal: ProposalOut
    already_decided: bool
    blocked: str | None = None
    message: str


class KillSwitchesOut(ApiModel):
    switches: list[KillSwitchOut]
    history: list[KillSwitchEventOut]


class ResetIn(ApiModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


# --- performance and journal ----------------------------------------------------------------------------


class HistogramBinOut(ApiModel):
    lo: Decimal
    hi: Decimal
    count: int


class MetricsOut(ApiModel):
    run_id: int
    from_date: date | None = None
    to_date: date | None = None
    trades: int
    wins: int
    win_rate: Decimal | None = None
    avg_win_r: Decimal | None = None
    avg_loss_r: Decimal | None = None
    expectancy_r: Decimal | None = None
    profit_factor: Decimal | None = None
    avg_slippage: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    adherence_pct: Decimal | None = None
    total_pnl: Decimal
    r_histogram: list[HistogramBinOut]


class EquityPointOut(ApiModel):
    ts: UtcDateTime
    equity: Decimal
    cash: Decimal
    settled_cash: Decimal
    peak_equity: Decimal
    drawdown_pct: Decimal


class EquityOut(ApiModel):
    run_id: int
    points: list[EquityPointOut]


class JournalDayOut(ApiModel):
    session_date: date
    rules_followed: bool | None = None
    notes: str | None = None
    answered_via: str | None = None
    updated_at: UtcDateTime | None = None
    trades: int
    realized_pnl: Decimal | None = None


class JournalIn(ApiModel):
    """Fields not sent are left unchanged: the route reads `model_fields_set`."""

    rules_followed: bool | None = None
    notes: Annotated[str, StringConstraints(max_length=5000)] | None = None


# --- settings and strategies ----------------------------------------------------------------------------


class FieldOut(ApiModel):
    name: str
    kind: FieldKind
    title: str
    description: str | None = None
    default: Any
    minimum: str | None = None
    maximum: str | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False
    enum: list[str] | None = None
    item_enum: list[str] | None = None
    pattern: str | None = None
    nullable: bool = False


class SettingOut(ApiModel):
    key: str
    value: Any
    default: Any
    is_default: bool
    group: str
    field: FieldOut
    updated_at: UtcDateTime | None = None
    updated_by: str | None = None


class SettingsOut(ApiModel):
    items: list[SettingOut]


class SettingIn(ApiModel):
    value: Any


# `schema` is the plan's JSON field name (the plug-in's JSON Schema); it shadows pydantic's deprecated
# BaseModel.schema() classmethod, which nothing here uses, so that one warning is silenced.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class StrategyOut(ApiModel):
        key: str
        version: str
        kind: Literal["entry", "overlay"]
        enabled: bool
        revision: int
        params: dict[str, Any]
        schema: dict[str, Any]  # type: ignore[assignment]
        fields: list[FieldOut]
        updated_at: UtcDateTime
        updated_by: str | None = None
        owns_open_positions: bool


class StrategyIn(ApiModel):
    params: dict[str, Any] | None = None
    enabled: bool | None = None


# --- system and jobs ------------------------------------------------------------------------------------


class JobRunOut(ApiModel):
    id: int
    job: str
    session_date: date
    started_at: UtcDateTime
    finished_at: UtcDateTime | None = None
    status: str
    error: str | None = None
    detail: dict[str, Any] | None = None
    duration_seconds: float | None = None


class JobRunIn(ApiModel):
    date: dt.date | None = None
    force: bool = False


class JobLaunchOut(ApiModel):
    job: str
    session_date: date | None = None  # None: the command's own default date
    launched: bool
    message: str


class NotificationOut(ApiModel):
    id: int
    kind: str
    status: str
    created_at: UtcDateTime
    sent_at: UtcDateTime | None = None
    attempts: int
    error: str | None = None


class SystemOut(ApiModel):
    server_time: UtcDateTime
    version: str
    app_env: str
    alembic_revision: str | None = None
    tz_iana_version: str | None = None
    telegram_configured: bool
    token: TokenOut
    worker: WorkerOut
    rate_limit: dict[str, int] | None = None
    last_runs: list[JobRunOut]
    errors: list[EventOut]
    notifications_failed: list[NotificationOut]
    manual_jobs: list[ManualJob]


class CredentialIn(ApiModel):
    refresh_token: Annotated[SecretStr, Field(min_length=1, max_length=400)]


class TelegramTestOut(ApiModel):
    sent: bool
    message: str


# --- watchlist ------------------------------------------------------------------------------------------


class WatchlistOut(ApiModel):
    session_date: date
    tickers: list[str]
    filename: str | None = None
    uploaded_at: UtcDateTime
    uploaded_by: str


class RejectedRowOut(ApiModel):
    row: int
    value: str
    reason: str


class WatchlistUploadOut(ApiModel):
    watchlist: WatchlistOut
    rejected: list[RejectedRowOut]
    launched: JobLaunchOut | None = None


# --- stream (SSE event payloads) ------------------------------------------------------------------------


class StreamHello(ApiModel):
    server_time: UtcDateTime


class StreamInvalidate(ApiModel):
    topics: list[Topic]


class StreamEvents(ApiModel):
    items: list[EventOut]
