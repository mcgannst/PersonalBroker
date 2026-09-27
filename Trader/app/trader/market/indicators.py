"""Pure indicator maths for the ORB strategy (SPEC §5.2). No I/O, no clock.

Every rounded result uses ``ROUND_HALF_UP``. Malformed input raises ``ValueError``
rather than producing a plausible but wrong number.
"""

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from trader.market.calendar import SessionCalendar
from trader.market.types import Candle

FOUR = Decimal("0.0001")
TWO = Decimal("0.01")


def _check_range(c: Candle) -> None:
    if c.high < c.low:
        raise ValueError(f"malformed candle at {c.start.isoformat()}: high {c.high} < low {c.low}")


def regular_hours(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> list[Candle]:
    """Return the bars that start at or after the session open and before its close.

    Honours early closes. Input order is kept. Raises ``ValueError`` if ``session``
    is not a trading session (weekend or holiday).
    """
    open_, close = cal.session_open(session), cal.session_close(session)
    return [c for c in candles if open_ <= c.start < close]


def opening_bar(candles: Sequence[Candle], cal: SessionCalendar, session: date) -> Candle | None:
    """Return the bar whose start equals the session open exactly.

    ``None`` means no bar starts at the open (there is no fallback to a later bar).
    Raises ``ValueError`` if ``session`` is not a trading session.
    """
    open_ = cal.session_open(session)
    return next((c for c in candles if c.start == open_), None)


def atr(daily: Sequence[Candle], period: int = 14) -> Decimal | None:
    """Wilder's Average True Range, rounded ``ROUND_HALF_UP`` to 4 dp.

    ``daily`` must be oldest first with strictly ascending ``start`` values, else
    ``ValueError`` (this also rejects duplicate bars). The first value is the simple
    mean of the first ``period`` true ranges, then Wilder smoothing is applied.
    Needs at least ``period + 1`` candles: ``None`` means too few candles.
    Raises ``ValueError("period must be >= 1")`` for a non-positive period.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    for prev, cur in pairwise(daily):
        if cur.start <= prev.start:
            raise ValueError(
                f"daily candles must be oldest first with strictly ascending start: "
                f"{cur.start.isoformat()} follows {prev.start.isoformat()}"
            )
    if len(daily) < period + 1:
        return None
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in pairwise(daily)
    ]
    value = sum(trs[:period], Decimal(0)) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value.quantize(FOUR, ROUND_HALF_UP)


def average_volume(bars: Sequence[Candle]) -> Decimal | None:
    """Mean volume of ``bars``, rounded ``ROUND_HALF_UP`` to 2 dp.

    ``None`` means there were no bars.
    """
    if not bars:
        return None
    return (Decimal(sum(b.volume for b in bars)) / len(bars)).quantize(TWO, ROUND_HALF_UP)


def rvol(volume: int, average: Decimal | None) -> Decimal | None:
    """Relative volume ``volume / average``, rounded ``ROUND_HALF_UP`` to 4 dp.

    ``None`` means there is no usable average (``average`` is ``None`` or zero).
    """
    if average is None or average == 0:
        return None
    return (Decimal(volume) / average).quantize(FOUR, ROUND_HALF_UP)


def is_doji(c: Candle, max_body_pct: Decimal = Decimal("0.10")) -> bool:
    """True when ``|close - open| / (high - low) <= max_body_pct`` (the boundary counts).

    A zero-range bar is a doji. Never returns ``None``. Raises ``ValueError`` for a
    malformed bar where ``high < low``.
    """
    _check_range(c)
    rng = c.high - c.low
    if rng == 0:
        return True
    return abs(c.close - c.open) / rng <= max_body_pct


def is_bearish(c: Candle) -> bool:
    """True when ``close < open``. Never returns ``None``.

    Raises ``ValueError`` for a malformed bar where ``high < low``.
    """
    _check_range(c)
    return c.close < c.open
