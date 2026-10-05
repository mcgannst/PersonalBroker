"""CHASECAP (Mon 2026-10-05, run 302): orb_sip's entry_limit_stop_fraction turns the buy-stop entry into a
stop-limit, so a breakout that has already run past the trigger is not chased.

Monday's entries were placed about 30 s after the opening bar closed and filled on the next quote, well above
their triggers: LIFE's 35.41 trigger filled at 35.8179 (0.41 above, against a 0.28 stop distance), ZETA, IOT,
PAA and AXGN 0.8 to 1.0 stop distances above. The default (None) keeps the plain buy-stop.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from tests.strategies.fakes import CAL, FakeCatalysts, FakeData, bar, make_ctx
from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import SPREAD_WIDE, FillParams, QuoteFillModel
from trader.broker.types import FillDecision, NoFill, OrderSpec
from trader.strategies.base import EnterLong
from trader.strategies.orb_sip import ORB_EVENT, OrbSip, OrbSipParams

NOW = datetime(2026, 10, 5, 14, 6, 6, tzinfo=UTC)
MODEL = QuoteFillModel(FillParams())


async def _entries(params: OrbSipParams) -> tuple[Any, list[EnterLong]]:
    data = FakeData()
    data.add(1, "AAA", bar("21.00", "21.50", "20.90", "21.40", 9000))  # atr 1.00: entry 21.51, stop 21.41
    strategy = OrbSip(params)
    ctx = make_ctx(data, params, FakeCatalysts())
    event = next(e for e in strategy.schedule(CAL) if e.key == ORB_EVENT)
    intents = await strategy.on_event(ctx, event)
    return ctx, [i for i in intents if isinstance(i, EnterLong)]


# --- the strategy -------------------------------------------------------------------------------------------
async def test_default_keeps_the_plain_buy_stop() -> None:
    assert OrbSipParams().entry_limit_stop_fraction is None
    ctx, entries = await _entries(OrbSipParams(require_catalyst=False))
    (e,) = entries
    assert (e.order_type, e.stop, e.limit, e.stop_loss) == (
        "stop",
        Decimal("21.5100"),
        None,
        Decimal("21.4100"),
    )
    assert "entry_limit" not in e.evidence
    assert "entry_limit" not in ctx.candidates[0].data


@pytest.mark.parametrize(
    ("fraction", "limit"),
    [("0.5", "21.5600"), ("0.25", "21.5350"), ("0", "21.5100"), ("1", "21.6100")],
)
async def test_fraction_makes_a_stop_limit_entry(fraction: str, limit: str) -> None:
    params = OrbSipParams(require_catalyst=False, entry_limit_stop_fraction=Decimal(fraction))
    ctx, entries = await _entries(params)
    (e,) = entries
    assert e.order_type == "stop_limit"
    assert (e.stop, e.limit, e.stop_loss) == (Decimal("21.5100"), Decimal(limit), Decimal("21.4100"))
    assert e.evidence["entry_limit"] == limit  # in the signal evidence and the candidate row
    assert ctx.candidates[0].data["entry_limit"] == limit


@pytest.mark.parametrize("bad", ["-0.1", "5.01", "NaN"])
def test_fraction_bounds(bad: str) -> None:
    with pytest.raises(ValidationError):
        OrbSipParams(entry_limit_stop_fraction=Decimal(bad))


def test_stored_configs_without_the_field_still_load() -> None:
    """Config id 4 revision 3 (run 302) has no entry_limit_stop_fraction: it loads with the default."""
    stored = {"top_n": 20, "max_positions": 10, "extend_past_top_n": True, "max_rank": 100}
    assert OrbSipParams.model_validate(stored).entry_limit_stop_fraction is None


# --- the live fill model with that order ------------------------------------------------------------------
def qq(bid: str, ask: str, last: str) -> QtQuote:
    return QtQuote(
        symbol_id=1,
        symbol="LIFE",
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=Decimal(last),
        last_regular=None,
        volume=1000,
        last_trade_time=NOW - timedelta(seconds=1),
        delay=0,
        is_halted=False,
        vwap=None,
        fetched_at=NOW - timedelta(seconds=0.2),
    )


def capped(stop: str, limit: str, stop_loss: str) -> OrderSpec:
    return OrderSpec(
        1, "buy", "stop_limit", 2, stop=Decimal(stop), limit=Decimal(limit), stop_loss=Decimal(stop_loss)
    )


# LIFE, Mon 10-05: trigger 35.41, stop 35.1297 (distance 0.2803); at 0.5 the limit is 35.5502.
LIFE = capped("35.41", "35.5502", "35.1297")


def test_life_regression_a_gapped_breakout_is_not_chased() -> None:
    """The quote that filled LIFE at 35.8179 (bid 35.68, ask 35.80, last 35.74) no longer fills."""
    out = MODEL.assess(LIFE, qq("35.68", "35.80", "35.74"), NOW)
    assert isinstance(out, NoFill) and out.reason == "above_limit"


def test_fills_while_the_ask_plus_slippage_is_within_the_limit() -> None:
    out = MODEL.assess(LIFE, qq("35.40", "35.50", "35.42"), NOW)  # slippage 0.0178 (5 bps)
    assert isinstance(out, FillDecision)
    assert out.trigger == "stop_limit"
    assert out.price == Decimal("35.5178")
    assert out.price <= Decimal("35.5502")


def test_the_order_keeps_working_and_fills_when_the_price_comes_back() -> None:
    """Above the limit now, back within it later: the same order fills then (still on a last >= stop)."""
    assert isinstance(MODEL.assess(LIFE, qq("35.68", "35.80", "35.74"), NOW), NoFill)
    later = MODEL.assess(LIFE, qq("35.41", "35.45", "35.43"), NOW)
    assert isinstance(later, FillDecision) and later.price == Decimal("35.4677")


def test_not_triggered_below_the_stop_even_with_the_ask_inside_the_limit() -> None:
    assert MODEL.assess(LIFE, qq("35.30", "35.45", "35.35"), NOW) == NoFill("not_triggered")


def test_the_spread_guard_still_applies_to_a_stop_limit_entry() -> None:
    out = MODEL.assess(LIFE, qq("35.30", "35.50", "35.42"), NOW)  # spread 0.20 > 0.5 x 0.2803
    assert isinstance(out, NoFill) and out.reason == SPREAD_WIDE
