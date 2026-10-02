"""FIX-DAY1 (3): fill freshness judges the book, not only the last trade.

Wed 2026-09-30: the CLDX entry stop sat 09:35-10:05 ET as "unusable quote (stale_quote)" because its last
trade was more than stale_quote_seconds old, although bid and ask were live; it filled 30 minutes late. A
quote with a live book (bid and ask > 0, not crossed, delay 0, not halted) FETCHED within
stale_quote_seconds is usable whatever its last trade's age; without a book the last trade's age decides.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel, quote_snapshot
from trader.broker.types import FillDecision, NoFill, OrderSpec

NOW = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams(stale_quote_seconds=10.0))


def q(
    bid: str | None = "9.99",
    ask: str | None = "10.00",
    last: str | None = "9.90",
    *,
    trade_age: float | None = 600.0,
    fetch_age: float | None = 1.0,
    delay: int | None = 0,
    halted: bool = False,
) -> QtQuote:
    return QtQuote(
        symbol_id=1,
        symbol="CLDX",
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last=Decimal(last) if last is not None else None,
        last_regular=None,
        volume=100_000,
        last_trade_time=NOW - timedelta(seconds=trade_age) if trade_age is not None else None,
        delay=delay,
        is_halted=halted,
        vwap=None,
        fetched_at=NOW - timedelta(seconds=fetch_age) if fetch_age is not None else None,
    )


def stop_buy(stop: str = "10.00") -> OrderSpec:
    return OrderSpec(1, "buy", "stop", 10, stop=Decimal(stop))


def test_live_book_with_an_old_last_trade_is_usable_and_a_fresh_print_fills_the_stop_entry() -> None:
    # FILLFIX: the old print is usable (not stale_quote) but triggers nothing; the ask alone never triggers
    assert MODEL.assess(stop_buy("10.00"), q(trade_age=1800), NOW) == NoFill("not_triggered")
    out = MODEL.assess(stop_buy("10.00"), q(last="10.00", trade_age=2), NOW)
    assert isinstance(out, FillDecision)
    assert out.trigger == "stop" and out.price == Decimal("10.0100")


def test_live_book_with_no_last_trade_time_at_all_is_usable() -> None:
    assert MODEL.assess(stop_buy(), q(trade_age=None), NOW) == NoFill("not_triggered")


def test_a_stop_entry_still_waits_for_ask_at_or_above_the_stop() -> None:
    out = MODEL.assess(stop_buy("10.05"), q(ask="10.04", trade_age=1800), NOW)
    assert out == NoFill("not_triggered")


def test_an_old_last_trade_above_the_stop_does_not_trigger_on_its_own() -> None:
    """The last trade is 30 minutes old: it says nothing about now. Only the live ask triggers."""
    out = MODEL.assess(stop_buy("10.05"), q(ask="10.04", last="10.20", trade_age=1800), NOW)
    assert out == NoFill("not_triggered")


def test_a_book_fetched_too_long_ago_is_stale() -> None:
    out = MODEL.assess(stop_buy(), q(fetch_age=10.5, trade_age=1800), NOW)
    assert isinstance(out, NoFill) and out.reason == "stale_quote"


def test_fetched_exactly_stale_quote_seconds_ago_is_still_usable() -> None:
    assert isinstance(
        MODEL.assess(stop_buy(), q(fetch_age=10.0, last="10.00", trade_age=2), NOW), FillDecision
    )


@pytest.mark.parametrize(
    ("bid", "ask"),
    [(None, "10.00"), ("9.99", None), ("0", "10.00"), ("9.99", "0"), (None, None)],
)
def test_no_book_and_an_old_last_trade_is_stale(bid: str | None, ask: str | None) -> None:
    out = MODEL.assess(stop_buy(), q(bid=bid, ask=ask, trade_age=600), NOW)
    assert isinstance(out, NoFill) and out.reason == "stale_quote"


def test_no_book_but_a_fresh_last_trade_goes_on_to_the_side_checks() -> None:
    out = MODEL.assess(stop_buy(), q(bid="9.99", ask=None, trade_age=2), NOW)
    assert out == NoFill("no_ask")


def test_a_crossed_book_is_not_a_live_book() -> None:
    """Crossed: not usable as a book, so the old last trade decides (stale)."""
    out = MODEL.assess(stop_buy(), q(bid="10.10", ask="10.00", trade_age=600), NOW)
    assert isinstance(out, NoFill) and out.reason == "stale_quote"


def test_a_crossed_book_with_a_fresh_last_trade_is_crossed_quote() -> None:
    out = MODEL.assess(stop_buy(), q(bid="10.10", ask="10.00", trade_age=1), NOW)
    assert isinstance(out, NoFill) and out.reason == "crossed_quote"


def test_delayed_and_halted_never_fill_even_with_a_live_book() -> None:
    assert MODEL.assess(stop_buy(), q(delay=15), NOW).reason == "delayed_quote"  # type: ignore[union-attr]
    assert MODEL.assess(stop_buy(), q(delay=None), NOW).reason == "delayed_quote"  # type: ignore[union-attr]
    assert MODEL.assess(stop_buy(), q(halted=True), NOW) == NoFill("halted")


def test_without_a_fetch_time_the_last_trade_rule_applies_as_before() -> None:
    """Replay quotes and older callers carry no fetched_at: the old last-trade-only rule, unchanged."""
    assert MODEL.assess(stop_buy(), q(fetch_age=None, trade_age=600), NOW).reason == "stale_quote"  # type: ignore[union-attr]
    assert isinstance(
        MODEL.assess(stop_buy(), q(fetch_age=None, last="10.00", trade_age=2), NOW), FillDecision
    )


def test_the_protective_stop_triggers_on_a_fresh_print_not_on_the_bid() -> None:
    """FILLFIX: Questrade triggers stops on the last trade: a bid under the stop with an old print above it
    doesn't sell; a fresh print at or under the stop sells into the bid."""
    order = OrderSpec(1, "sell", "stop", 10, stop=Decimal("9.50"), purpose="stop", position_id=7)
    held = MODEL.assess(order, q(bid="9.45", ask="9.47", last="9.80", trade_age=900), NOW)
    assert held == NoFill("not_triggered")
    out = MODEL.assess(order, q(bid="9.45", ask="9.47", last="9.48", trade_age=1), NOW)
    assert isinstance(out, FillDecision) and out.trigger == "stop" and out.price == Decimal("9.4400")


def test_the_snapshot_records_the_fetch_time() -> None:
    snap = quote_snapshot(q(fetch_age=1.0), NOW)
    assert snap["fetched_at"] == (NOW - timedelta(seconds=1)).isoformat()
