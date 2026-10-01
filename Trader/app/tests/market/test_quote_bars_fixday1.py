"""FIX-DAY1: the pure maths of the pre-market-free opening volume and the late-quote rule (no I/O).

- The quote's day volume includes pre-market trades (CLDX Wed 09-30: 339,533 at 09:35 against an official
  09:30-09:35 candle of 35,628), so the opening volume is the 09:35 capture's volume minus the volume at the
  open (the open capture), times the candle-scale factor.
- The factor is measured on regular-session volume: candle RTH / (quote day - pre-market quote volume) with
  the open capture, else (candle RTH + pre-market candles) / quote day (the same ratio, both on one scale).
- A quote whose last trade is more than 2 s after 09:35:00 may carry post-bar prints in its high/low
  (NVTS Wed: high 12.3899 read 6 s late against the official 12.30): it is not a bar ("quote_late").
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.market.quote_bars import (
    QUOTE_LATE,
    QUOTE_LATE_AFTER,
    open_snapshot_volume,
    opening_delta,
    quote_bar,
    regular_factor,
    scale_volume,
)
from trader.market.types import Candle

DAY = date(2026, 9, 30)
OPEN = datetime(2026, 9, 30, 13, 30, tzinfo=UTC)  # 09:30 ET
END = OPEN + timedelta(minutes=5)


def q(**kw: object) -> QtQuote:
    values: dict[str, object] = {
        "symbol_id": 7,
        "symbol": "NVTS",
        "bid": Decimal("12.20"),
        "ask": Decimal("12.21"),
        "last": Decimal("12.20"),
        "last_regular": Decimal("12.20"),
        "volume": 500_000,
        "last_trade_time": END + timedelta(seconds=1),
        "delay": 0,
        "is_halted": False,
        "vwap": None,
        "open": Decimal("11.80"),
        "high": Decimal("12.30"),
        "low": Decimal("11.75"),
    }
    values.update(kw)
    return QtQuote(**values)  # type: ignore[arg-type]


# --- the late-quote rule ------------------------------------------------------------------------------------
def test_a_quote_within_two_seconds_of_the_bar_end_is_a_bar() -> None:
    got = quote_bar(q(last_trade_time=END + QUOTE_LATE_AFTER), OPEN, END)
    assert isinstance(got, Candle) and got.high == Decimal("12.30")


def test_a_quote_more_than_two_seconds_late_is_not_a_bar() -> None:
    """NVTS Wed 09-30: read 6.3 s after 09:35:00, the high had moved on to 12.3899 (official 12.30)."""
    late = q(last_trade_time=END + timedelta(seconds=6.32), high=Decimal("12.3899"))
    assert quote_bar(late, OPEN, END) == QUOTE_LATE
    assert (
        quote_bar(q(last_trade_time=END + QUOTE_LATE_AFTER + timedelta(milliseconds=1)), OPEN, END)
        == QUOTE_LATE
    )


def test_a_quiet_quote_whose_last_trade_is_inside_the_bar_is_a_bar() -> None:
    assert isinstance(quote_bar(q(last_trade_time=OPEN + timedelta(minutes=2)), OPEN, END), Candle)


# --- the volume at the open ---------------------------------------------------------------------------------
def test_the_open_snapshot_volume_is_the_pre_market_volume() -> None:
    snap = q(volume=303_905, last_trade_time=OPEN - timedelta(seconds=3), open=None, high=None, low=None)
    assert open_snapshot_volume(snap, OPEN, DAY) == 303_905


def test_no_trade_today_means_zero_volume_at_the_open() -> None:
    """Before today's first trade Questrade may still show yesterday's volume: not today's pre-market."""
    yesterday = q(volume=2_000_000, last_trade_time=OPEN - timedelta(hours=17))
    assert open_snapshot_volume(yesterday, OPEN, DAY) == 0
    assert open_snapshot_volume(q(volume=5, last_trade_time=None), OPEN, DAY) == 0


def test_a_snapshot_that_already_holds_a_regular_trade_is_not_used() -> None:
    """Its volume would include the opening print, which belongs to the 09:30 bar."""
    assert open_snapshot_volume(q(last_trade_time=OPEN), OPEN, DAY) is None
    assert open_snapshot_volume(q(last_trade_time=OPEN + timedelta(seconds=1)), OPEN, DAY) is None


@pytest.mark.parametrize("delay", [None, 15])
def test_a_delayed_snapshot_is_not_used(delay: int | None) -> None:
    assert (
        open_snapshot_volume(q(delay=delay, last_trade_time=OPEN - timedelta(seconds=1)), OPEN, DAY) is None
    )


def test_no_snapshot_quote_is_none() -> None:
    assert open_snapshot_volume(None, OPEN, DAY) is None


# --- delta and scaling --------------------------------------------------------------------------------------
def test_the_opening_volume_is_the_delta_times_the_factor() -> None:
    """CLDX-like: 339,533 at 09:35, 303,905 at the open: 35,628 traded in the bar (quote scale)."""
    delta = opening_delta(339_533, 303_905)
    assert delta == 35_628
    assert scale_volume(delta, Decimal("0.700000")) == 24_940


def test_no_snapshot_or_a_shrinking_volume_has_no_delta() -> None:
    assert opening_delta(339_533, None) is None
    assert opening_delta(100, 101) is None
    assert opening_delta(100, 100) == 0


# --- the factor on regular-session volume -------------------------------------------------------------------
def test_factor_with_the_open_snapshot_subtracts_pre_market_quote_volume() -> None:
    # day quote 1,400,000 of which 400,000 pre-market; RTH candles 700,000 -> 0.7
    assert regular_factor(700_000, 1_400_000, premarket_quote=400_000) == Decimal("0.700000")


def test_factor_without_a_snapshot_adds_the_pre_market_candles_on_the_candle_side() -> None:
    """Both sides on one scale: (RTH candles + pre-market candles) / day quote volume. A true ratio of 0.7:
    pre-market 400,000 (quote) = 280,000 (candles) and RTH 1,000,000 (quote) = 700,000 (candles)."""
    assert regular_factor(700_000, 1_400_000, premarket_candle=280_000) == Decimal("0.700000")


def test_factor_snapshot_wins_over_candles() -> None:
    assert regular_factor(700_000, 1_400_000, premarket_quote=400_000, premarket_candle=1) == Decimal(
        "0.700000"
    )


def test_factor_without_pre_market_information_is_the_old_ratio() -> None:
    assert regular_factor(700_000, 1_000_000) == Decimal("0.700000")


def test_factor_is_none_when_nothing_regular_traded() -> None:
    assert regular_factor(0, 1_000) is None
    assert regular_factor(700, 1_000, premarket_quote=1_000) is None
    assert regular_factor(700, 1_000, premarket_quote=2_000) is None
