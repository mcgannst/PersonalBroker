"""Decision log contracts (P6-T9): stages, outcomes, the per-filter check, the record the recorder writes,
the day summary, the pass result, the scan-data protocol, the recorder's dependencies and the read views.

The decision log (`trader.decision_log`, migration 0007) is a derived journal: the recorder (P6-T10) builds
it from the rows the engine already writes and writes nothing but `decision_log`. No decision-path module
imports this package (D2: logging only). Numbers in checks are exact decimal strings; times are UTC.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, get_args

from sqlalchemy.orm import Session, sessionmaker

from trader.market.calendar import SessionCalendar
from trader.market.clock import Clock
from trader.market.types import Candle, OpenBarStats, UniverseMember
from trader.settings_store import RuntimeSettings

DecisionStage = Literal[
    "universe",
    "premarket",
    "scan",
    "signal",
    "risk",
    "proposal",
    "approval",
    "order",
    "fill",
    "exit",
    "overlay",
    "kill_switch",
    "day",
]
# Rows of a day are ordered by stage in this order, then by `ts`, then by source id (-> `seq`).
STAGE_ORDER: tuple[DecisionStage, ...] = get_args(DecisionStage)

DecisionOutcome = Literal[
    "info",
    "listed",
    "classified",
    "passed",
    "rejected",
    "proposed",
    "approved",
    "auto_approved",
    "declined",
    "expired",
    "blocked",
    "submitted",
    "filled",
    "cancelled",
    "exited",
    "tripped",
    "reset",
    "error",
]
OUTCOMES: tuple[DecisionOutcome, ...] = get_args(DecisionOutcome)

CheckOp = Literal[">=", "<=", "between", "==", "!=", "present", "absent"]
RecordSkip = Literal["unchanged", "final", "disabled", "not_session"]
ScanDetail = Literal["all", "ranked"]

SOURCE = "decisions"  # event_log source of the recorder's warning/info events (never relayed)
MAX_REASON_CHARS = 500
MAX_DATA_BYTES = 8192
LOCK_PREFIX = "trader.decisions"  # pg_advisory_xact_lock(hashtext('trader.decisions:<run>:<date>'))


@dataclass(frozen=True, slots=True)
class Check:
    """One filter rule of a scan candidate: its value, the comparison, the threshold and the verdict.
    `value` and `threshold` are exact decimal strings (or None); `passed` is None only when an input is
    missing."""

    name: str
    value: str | None
    op: CheckOp
    threshold: str | None
    passed: bool | None


@dataclass(frozen=True)
class DecisionRecord:
    """One decision, before the recorder adds `run_id`, `seq`, `recorded_at` and `final`."""

    session_date: date
    stage: DecisionStage
    outcome: DecisionOutcome
    ts: datetime  # when the decision happened (from the source row), UTC
    strategy_key: str | None = None
    symbol_id: int | None = None
    ticker: str | None = None
    rule: str | None = None
    reason: str | None = None
    ref: Mapping[str, int] = field(default_factory=dict)  # source row ids, e.g. {"candidate_id": 5}
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DaySummary:
    run_id: int
    session_date: date
    final: bool
    universe_size: int | None
    universe_source: str | None
    premarket_listed: int
    premarket_classified: int
    scanned: int
    rvol_passed: int
    ranked: int
    passed: int
    rejects_by_rule: tuple[tuple[str, int], ...]  # descending count
    signals: int
    risk_rejections: tuple[tuple[str, int], ...]
    proposals: int
    approvals: Mapping[str, int]  # keys manual, auto, declined, expired, blocked
    median_decision_seconds: float | None
    fills: int
    avg_fill_diff_per_share: Decimal | None  # positive = worse than planned
    trades: int
    wins: int
    losses: int
    pnl: Decimal
    pnl_r: Decimal | None
    exits_by_reason: tuple[tuple[str, int], ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class RecordResult:
    run_id: int
    session_date: date
    skipped: RecordSkip | None  # None: the day was (re)built
    stages: Mapping[str, int]  # rows written per stage (empty when skipped)
    final: bool


class ScanData(Protocol):
    """The scan inputs the recorder reads for non-candidates. Never a network call."""

    async def universe(self, session_date: date) -> list[UniverseMember]: ...

    async def open_bar_stats(self, session_date: date) -> dict[int, OpenBarStats]: ...

    async def stored_opening_bars(self, session_date: date, symbol_ids: list[int]) -> dict[int, Candle]: ...


@dataclass(frozen=True)
class RecorderDeps:
    factory: sessionmaker[Session]
    clock: Clock
    calendar: SessionCalendar
    settings: Callable[[], RuntimeSettings]
    scan_data: ScanData | None


@dataclass(frozen=True)
class DecisionRowView:
    """One `decision_log` row."""

    id: int
    run_id: int
    session_date: date
    seq: int
    stage: DecisionStage
    strategy_key: str | None
    symbol_id: int | None
    ticker: str | None
    outcome: DecisionOutcome
    rule: str | None
    reason: str | None
    ts: datetime
    ref: Mapping[str, int]
    data: Mapping[str, Any]
    recorded_at: datetime
    final: bool


@dataclass(frozen=True)
class DayView:
    run_id: int
    run_mode: str  # live | replay
    session_date: date
    final: bool
    recorded_at: datetime | None
    summary: DaySummary | None
    summary_text: str | None
    rows: tuple[DecisionRowView, ...]
    total: int  # rows matching the filters, before limit/offset


@dataclass(frozen=True)
class DayItem:
    run_id: int
    session_date: date
    final: bool
    summary_text: str | None
    proposals: int
    trades: int
