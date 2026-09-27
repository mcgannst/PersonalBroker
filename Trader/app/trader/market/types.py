from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Interval = Literal["OneMinute", "FiveMinutes", "FifteenMinutes", "OneHour", "OneDay"]
INTERVAL_CODES: dict[Interval, str] = {
    "OneMinute": "1m",
    "FiveMinutes": "5m",
    "FifteenMinutes": "15m",
    "OneHour": "1h",
    "OneDay": "1d",
}


@dataclass(frozen=True, slots=True)
class Candle:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None
