"""QUOTEBAR: the pure parts of the 9:35 opening bar from live quotes (no database, no network).

quote -> bar mapping, the candle-scale volume factor (measured, median, default) and the shadow comparison
maths."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.market.quote_bars import (
    DEFAULT_VOLUME_FACTOR,
    NO_TRADE,
    QUOTE_DELAYED,
    QUOTE_INCOMPLETE,
    VolumeScale,
    compare_bars,
    median_factor,
    quote_bar,
    scale_volume,
    usable_factor,
)
from trader.market.types import Candle

START = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)  # 09:30 ET
END = START + timedelta(minutes=5)


def q(**kw: object) -> QtQuote:
    values: dict[str, object] = {
        "symbol_id": 7,
        "symbol": "SMMT",
        "bid": Decimal("18.90"),
        "ask": Decimal("18.95"),
        "last": Decimal("18.93"),
        "last_regular": Decimal("18.93"),
        "volume": 1_400_000,
        # FIX-DAY1: was END + 4 s (read at 09:35:05); a last trade > 2 s after 09:35:00 is now quote_late
        "last_trade_time": END + timedelta(seconds=1),
        "delay": 0,
        "is_halted": False,
        "vwap": Decimal("18.70"),
        "open": Decimal("18.40"),
        "high": Decimal("19.09"),
        "low": Decimal("18.31"),
    }
    values.update(kw)
    return QtQuote(**values)  # type: ignore[arg-type]


def bar(o: str, h: str, low: str, c: str, v: int) -> Candle:
    return Candle(START, END, Decimal(o), Decimal(h), Decimal(low), Decimal(c), v, None)


# --- quote -> bar -------------------------------------------------------------------------------------------
def test_a_quote_maps_to_the_opening_bar() -> None:
    got = quote_bar(q(), START, END)
    assert got == Candle(
        START,
        END,
        Decimal("18.40"),
        Decimal("19.09"),
        Decimal("18.31"),
        Decimal("18.93"),
        1_400_000,  # the raw quote volume: the caller scales it
        Decimal("18.70"),
    )


def test_close_prefers_the_regular_hours_last_trade() -> None:
    got = quote_bar(q(last=Decimal("18.99"), last_regular=Decimal("18.93")), START, END)
    assert isinstance(got, Candle) and got.close == Decimal("18.93")
    got = quote_bar(q(last=Decimal("18.99"), last_regular=None), START, END)
    assert isinstance(got, Candle) and got.close == Decimal("18.99")


def test_high_and_low_always_contain_open_and_close() -> None:
    # the quote's high/low can lag a trade by a moment: the bar never has close > high or close < low
    got = quote_bar(q(high=Decimal("18.90"), last_regular=Decimal("18.95")), START, END)
    assert isinstance(got, Candle) and got.high == Decimal("18.95")
    got = quote_bar(q(low=Decimal("18.45"), open=Decimal("18.40")), START, END)
    assert isinstance(got, Candle) and got.low == Decimal("18.40")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"open": None}, NO_TRADE),  # no opening trade yet
        ({"open": Decimal("0")}, NO_TRADE),
        ({"volume": 0}, NO_TRADE),
        ({"last_trade_time": None}, NO_TRADE),
        ({"last_trade_time": START - timedelta(minutes=20)}, NO_TRADE),  # a pre-market print only
        ({"last": None, "last_regular": None}, NO_TRADE),
        ({"high": None}, QUOTE_INCOMPLETE),
        ({"low": None}, QUOTE_INCOMPLETE),
        ({"delay": 15}, QUOTE_DELAYED),
    ],
)
def test_a_quote_without_a_regular_session_trade_is_missing(changes: dict[str, object], reason: str) -> None:
    assert quote_bar(q(**changes), START, END) == reason


# --- volume scale -------------------------------------------------------------------------------------------
def test_scale_volume_rounds_half_up() -> None:
    assert scale_volume(1_000, Decimal("0.6716")) == 672  # 671.6
    assert scale_volume(1_000, Decimal("0.6715")) == 672  # 671.5 rounds up
    assert scale_volume(0, Decimal("0.7")) == 0


@pytest.mark.parametrize(
    ("factor", "usable"),
    [
        (Decimal("0.67"), True),
        (Decimal("1.1"), True),
        (Decimal("0.05"), True),
        (Decimal("0.04"), False),
        (Decimal("1.11"), False),
        (Decimal("2"), False),
        (Decimal("0"), False),
        (None, False),
    ],
)
def test_usable_factor_bounds(factor: Decimal | None, usable: bool) -> None:
    assert usable_factor(factor) is usable


def test_median_factor() -> None:
    assert median_factor([]) is None
    assert median_factor([Decimal("0.7")]) == Decimal("0.7")
    assert median_factor([Decimal("0.6"), Decimal("0.9"), Decimal("0.7")]) == Decimal("0.7")
    assert median_factor([Decimal("0.6"), Decimal("0.7"), Decimal("0.8"), Decimal("0.9")]) == Decimal("0.75")


def test_the_measured_factor_wins_then_the_median_then_the_default() -> None:
    scale = VolumeScale({1: Decimal("0.60"), 2: Decimal("0.70"), 3: Decimal("0.80")}, date(2026, 10, 5))
    assert scale.factor_for(1) == (Decimal("0.60"), "symbol")
    assert scale.factor_for(99) == (Decimal("0.70"), "median")
    empty = VolumeScale({}, None)
    assert empty.factor_for(1) == (DEFAULT_VOLUME_FACTOR, "default")
    assert DEFAULT_VOLUME_FACTOR == Decimal("0.7143")  # 1 / 1.4


def test_unusable_measured_factors_are_ignored() -> None:
    scale = VolumeScale({1: Decimal("5"), 2: Decimal("0.70")}, date(2026, 10, 5))
    assert scale.factor_for(1) == (Decimal("0.70"), "median")  # 5 is not a usable factor, nor in the median


# --- shadow comparison --------------------------------------------------------------------------------------
def test_compare_bars_exact_prices_and_volume_within_ten_percent() -> None:
    quote = bar("18.40", "19.00", "18.31", "18.93", 1_050)
    official = bar("18.40", "19.00", "18.31", "18.90", 1_000)
    c = compare_bars(quote, official)
    assert c.prices_exact is True  # open/high/low: the close is 5 s later by design
    assert c.close_exact is False
    assert c.volume_error == Decimal("0.0500")
    assert c.volume_within is True
    assert c.high_diff == Decimal("0")


def test_compare_bars_price_and_volume_misses() -> None:
    quote = bar("18.40", "19.09", "18.31", "18.93", 1_200)
    official = bar("18.40", "19.00", "18.31", "18.93", 1_000)
    c = compare_bars(quote, official)
    assert c.prices_exact is False and c.close_exact is True
    assert c.high_diff == Decimal("0.09")
    assert c.volume_error == Decimal("0.2000") and c.volume_within is False
    low = compare_bars(bar("18.40", "19.00", "18.31", "18.93", 899), official)
    assert low.volume_error == Decimal("-0.1010") and low.volume_within is False
    edge = compare_bars(bar("18.40", "19.00", "18.31", "18.93", 1_100), official)
    assert edge.volume_within is True  # exactly 10% counts


def test_compare_bars_with_zero_official_volume() -> None:
    c = compare_bars(bar("1", "1", "1", "1", 5), bar("1", "1", "1", "1", 0))
    assert c.volume_error is None and c.volume_within is False
