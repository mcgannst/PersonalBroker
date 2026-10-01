"""QUOTEBAR: the 09:30-09:35 opening bar from a live quote, and the maths around it (pure: no I/O, no clock).

Why: Stephen's Questrade market-data package serves intraday candles only ~10 minutes late (a bar that ended
less than ~10 minutes ago is refused with HTTP 401 code 1022), while quotes are real time (delay 0). So at
09:35:05 the opening bar is read from one batched quotes pass instead (probe, Tue 2026-09-29):

- open  = the quote's openPrice (equal to the first regular 5-minute candle's open);
- high / low = highPrice / lowPrice: regular-session extremes (premarket excluded), which at 09:35:05 are the
  opening bar's (plus at most the five seconds after 09:35:00; occasionally a print the candle feed lacks);
- close = the last regular-hours trade (lastTradePriceTrHrs, else lastTradePrice), 5 s after 09:35:00;
- volume = the quote's consolidated volume, ~1.3-1.5x the candle feed's (SPY 36.85M vs 24.75M). The strategy's
  baseline (open_bar_stats.avg_open_vol_14d) is built from candles, so the quote volume is converted to candle
  scale with a per-symbol factor measured after the previous close (candle volume / quote volume), else the
  universe median of those factors, else DEFAULT_VOLUME_FACTOR (1 / 1.4).

FIX-DAY1 (Wed 2026-09-30): the quote's volume is the DAY's, pre-market included (CLDX 339,533 at 09:35 against
an official 35,628), and the quotes read at 09:35:05 had moved past the bar (NVTS high 12.3899 against 12.30).
So the bar is read at 09:35:00.0 by a timed capture, its volume is that capture's minus the volume at the open
(a capture at 09:29:58), and a quote whose last trade is more than QUOTE_LATE_AFTER past 09:35:00 is not a
bar.
The factor is measured on regular-session volume (`regular_factor`).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from trader.adapters.questrade.models import QtQuote
from trader.market.clock import et_date
from trader.market.types import Candle

OpeningBarSource = Literal["quotes", "candles"]
FactorSource = Literal["symbol", "median", "default"]

DEFAULT_VOLUME_FACTOR = Decimal("0.7143")  # 1 / 1.4: the middle of the probe's 1.3-1.5x
FACTOR_MIN = Decimal("0.05")  # a measured factor outside [FACTOR_MIN, FACTOR_MAX] is not trusted
FACTOR_MAX = Decimal("1.1")  # real factors are ~0.67-0.77; a looser cap would let one bad factor inflate rvol
FACTOR_PLACES = Decimal("0.000001")
# Quotes are the bar only right after it closes: later, the quote's high/low are no longer the bar's.
QUOTE_BAR_MAX_LAG = timedelta(minutes=5)
# Missing reasons (OpeningBars.missing).
NO_QUOTE = "no_quote"  # Questrade returned no quote for the symbol
NO_TRADE = "no_trade"  # no regular-session trade yet (no open, no volume, or the last trade before 09:30)
QUOTE_INCOMPLETE = "quote_incomplete"  # an open but no session high or low
QUOTE_DELAYED = "quote_delayed"  # a delayed quote: its prices are not the bar's
VOLUME_TOLERANCE = Decimal("0.10")  # the shadow check's "volume within" band
# FIX-DAY1 (Wed 2026-09-30) ----------------------------------------------------------------------------------
# A quote whose last trade is more than this after the bar's end may hold post-bar prints in its high/low
# (NVTS: read 6.3 s late, high 12.3899 against the official 12.30): it is not the bar.
QUOTE_LATE_AFTER = timedelta(seconds=2)
QUOTE_LATE = "quote_late"
# The two timed captures: `open` (the volume at the open) starts this long BEFORE 09:30:00, so the opening
# print, which belongs to the 09:30 bar, is not in it; `bar` starts at 09:35:00.0. A capture that could not
# start within CAPTURE_WINDOW of its time is not run (its quotes would not be the moment's).
OPEN_CAPTURE_LEAD = timedelta(seconds=2)
CAPTURE_WINDOW = timedelta(seconds=5)
CAPTURE_OPEN = "open"
CAPTURE_BAR = "bar"
VOLUME_BASIS_DELTA = "delta"  # opening_bar_quotes.volume_basis: (bar capture - open capture) x factor
NO_OPEN_SNAPSHOT = "no_open_snapshot"  # no usable volume at the open: the symbol falls back to candles


def open_snapshot_volume(q: QtQuote | None, open_: datetime, session_date: date) -> int | None:
    """The volume at the open from the open capture's quote (pre-market volume, quote scale). 0 when the
    symbol has not traded today (Questrade may still show the previous session's volume). None (not usable)
    without a quote, for a delayed or delay-unknown quote, and when the quote already holds a regular-session
    trade (its volume would include the opening print)."""
    if q is None or q.delay is None or q.delay > 0:
        return None
    if q.last_trade_time is None:
        return 0
    if q.last_trade_time >= open_:
        return None
    if et_date(q.last_trade_time) != session_date:
        return 0
    return max(q.volume, 0)


def opening_delta(bar_volume: int, open_volume: int | None) -> int | None:
    """The volume traded since the open (quote scale); None without a volume at the open, or when the day
    volume shrank (inconsistent quotes)."""
    if open_volume is None or bar_volume < open_volume:
        return None
    return bar_volume - open_volume


def regular_factor(
    rth_candle_volume: int,
    quote_day_volume: int,
    *,
    premarket_quote: int | None = None,
    premarket_candle: int | None = None,
) -> Decimal | None:
    """The candle/quote factor on regular-session volume (6 places; None when not measurable).

    With the open capture's pre-market volume (quote scale): RTH candles / (quote day - pre-market). Without
    it, the pre-market candles (candle scale) cannot be subtracted from a quote-scale volume; with one ratio f
    for the whole day (PM candles = f x PM quote, RTH candles = f x RTH quote) the same factor is
    (RTH candles + PM candles) / quote day. Without either: RTH candles / quote day (the QUOTEBAR ratio)."""
    if premarket_quote is not None:
        return measured_factor(rth_candle_volume, quote_day_volume - premarket_quote)
    if premarket_candle is not None and rth_candle_volume > 0:
        return measured_factor(rth_candle_volume + premarket_candle, quote_day_volume)
    return measured_factor(rth_candle_volume, quote_day_volume)


def quote_bar(q: QtQuote, start: datetime, end: datetime) -> Candle | str:
    """The opening bar [start, end) from quote `q`, or the missing reason. `volume` is the RAW quote volume
    (the caller turns it into the bar's volume). High and low are widened to contain open and close (a quote's
    high/low can trail the last trade by a moment). A quote whose last trade is more than QUOTE_LATE_AFTER
    after `end` is QUOTE_LATE (FIX-DAY1)."""
    if q.delay is not None and q.delay > 0:
        return QUOTE_DELAYED
    if q.last_trade_time is not None and q.last_trade_time > end + QUOTE_LATE_AFTER:
        return QUOTE_LATE
    close = q.last_regular if q.last_regular is not None and q.last_regular > 0 else q.last
    if (
        q.open is None
        or q.open <= 0
        or q.volume <= 0
        or close is None
        or close <= 0
        or q.last_trade_time is None
        or q.last_trade_time < start
    ):
        return NO_TRADE
    if q.high is None or q.low is None or q.high <= 0 or q.low <= 0:
        return QUOTE_INCOMPLETE
    high = max(q.high, q.open, close)
    low = min(q.low, q.open, close)
    return Candle(start, end, q.open, high, low, close, q.volume, q.vwap)


def usable_factor(factor: Decimal | None) -> bool:
    return factor is not None and FACTOR_MIN <= factor <= FACTOR_MAX


def median_factor(values: Iterable[Decimal]) -> Decimal | None:
    xs = sorted(values)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def scale_volume(raw: int, factor: Decimal) -> int:
    """A quote volume on candle scale, rounded half up."""
    return int((Decimal(raw) * factor).quantize(Decimal(1), ROUND_HALF_UP))


def measured_factor(candle_volume: int, quote_volume: int) -> Decimal | None:
    """candle / quote volume, 6 places; None when either is not positive."""
    if candle_volume <= 0 or quote_volume <= 0:
        return None
    return (Decimal(candle_volume) / Decimal(quote_volume)).quantize(FACTOR_PLACES, ROUND_HALF_UP)


@dataclass(frozen=True)
class VolumeScale:
    """The factors measured after one earlier session's close (`measured_on`), by symbols.id. Unusable
    factors are ignored, also for the median."""

    factors: Mapping[int, Decimal]
    measured_on: date | None

    @property
    def median(self) -> Decimal | None:
        return median_factor(f for f in self.factors.values() if usable_factor(f))

    def factor_for(self, symbol_id: int) -> tuple[Decimal, FactorSource]:
        own = self.factors.get(symbol_id)
        if usable_factor(own):
            assert own is not None
            return own, "symbol"
        median = self.median
        if median is not None:
            return median, "median"
        return DEFAULT_VOLUME_FACTOR, "default"


@dataclass(frozen=True)
class BarComparison:
    """A quote-built bar against the official candle. `prices_exact`: open, high and low equal (the close is
    read five seconds after 09:35:00 by design, so it is reported apart). `volume_error`: (quote - official) /
    official, 4 places; None when the official volume is 0."""

    prices_exact: bool
    close_exact: bool
    high_diff: Decimal
    volume_error: Decimal | None
    volume_within: bool


def compare_bars(quote: Candle, official: Candle, tolerance: Decimal = VOLUME_TOLERANCE) -> BarComparison:
    error: Decimal | None = None
    if official.volume > 0:
        error = ((Decimal(quote.volume) - official.volume) / official.volume).quantize(
            Decimal("0.0001"), ROUND_HALF_UP
        )
    return BarComparison(
        prices_exact=(quote.open, quote.high, quote.low) == (official.open, official.high, official.low),
        close_exact=quote.close == official.close,
        high_diff=quote.high - official.high,
        volume_error=error,
        volume_within=error is not None and abs(error) <= tolerance,
    )
