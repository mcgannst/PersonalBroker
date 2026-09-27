"""Pure indicator maths for the ORB strategy (SPEC §5.2). No I/O, no clock."""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from trader.market.calendar import SessionCalendar
from trader.market.types import Candle

FOUR = Decimal("0.0001")
TWO = Decimal("0.01")


def regular_hours(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> list[Candle]:
    open_, close = cal.session_open(session), cal.session_close(session)
    return [c for c in candles if open_ <= c.start < close]


def opening_bar(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> Candle | None:
    open_ = cal.session_open(session)
    return next((c for c in candles if c.start == open_), None)


def atr(daily: Sequence[Candle], period: int = 14) -> Decimal | None:
    if len(daily) < period + 1:
        return None
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(daily, daily[1:], strict=False)
    ]
    value = sum(trs[:period], Decimal(0)) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value.quantize(FOUR, ROUND_HALF_UP)


def average_volume(bars: Sequence[Candle]) -> Decimal | None:
    if not bars:
        return None
    return (Decimal(sum(b.volume for b in bars)) / len(bars)).quantize(TWO, ROUND_HALF_UP)


def rvol(volume: int, average: Decimal | None) -> Decimal | None:
    if average is None or average == 0:
        return None
    return (Decimal(volume) / average).quantize(FOUR, ROUND_HALF_UP)


def is_doji(c: Candle, max_body_pct: Decimal = Decimal("0.10")) -> bool:
    rng = c.high - c.low
    if rng == 0:
        return True
    return abs(c.close - c.open) / rng <= max_body_pct


def is_bearish(c: Candle) -> bool:
    return c.close < c.open
