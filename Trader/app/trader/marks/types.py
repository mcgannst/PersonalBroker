"""The mark tap's and publisher's shared types and constants (DB-T1 contracts, final; live dashboard plan S1a,
S10). Thresholds are module constants, never settings keys (soak safety: no settings_store change)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from trader.adapters.questrade.models import QtQuote

PUBLISH_INTERVAL_S: float = 2.0
MAX_OBSERVATIONS_PER_SYMBOL: int = 30  # the tap keeps at most this many undrained per symbol (S1a)
MAX_TAP_SYMBOLS: int = 1000  # the tap forgets the least recently observed symbols beyond this
MAX_CANDLE_BATCHES: int = 5
MARK_BARS_KEEP_DAYS: int = 10
PUBLISH_STATEMENT_TIMEOUT_MS: int = 5000  # SET LOCAL statement_timeout and lock_timeout of a publisher pass
MARKS_SOURCE: str = "marks"  # event_log source of the publisher's warning/info events


@dataclass(frozen=True, slots=True)
class ObservedQuote:
    qt_id: int  # Questrade symbol id, as the client returned it
    quote: QtQuote  # the very object the client returned
    observed_at: datetime  # the Clock time the call returned


@dataclass(frozen=True, slots=True)
class CandleBatch:
    """One `candles_many` call, counted after it returned or raised (S10 `candle_batches`)."""

    started_at: datetime
    symbols: int
    completed: int
    errors: int
    outstanding: int
    elapsed_s: float
    deadline_s: float | None
    http_429: int
    pause_s: float
    raised: str | None


@dataclass(frozen=True, slots=True)
class PublishStep:
    """The outcome of one publisher pass (`MarkPublisher.run_once`)."""

    at: datetime
    skipped: Literal["no_run", "no_new_quotes", "error"] | None
    marks_written: int
    bars_written: int


class LatestQuotes(Protocol):
    def drain(self) -> list[ObservedQuote]: ...


# (level, message, data, run_id) -> None: writes one event_log row and never raises.
EventWriter = Callable[[str, str, dict[str, Any], int | None], None]
