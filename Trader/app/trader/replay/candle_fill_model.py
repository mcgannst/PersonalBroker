"""The candle fill model (SPEC §7.4; P5-T3): fills from 1-minute bars with slippage, a half-spread estimate
and gap-through handling, implementing the P2 `FillModel` protocol. A `QtQuote` raises TypeError (the mirror
of `QuoteFillModel`). The broker, not the model, checks the bar's time against the order (P5-T4).

Rules (never flattering the strategy), with slip(p) = max(slippage_min, slippage_bps x p) and
hs(p) = half_spread_bps x p, each 4 dp half-up:
- market: buy open + slip + hs, sell open - slip - hs (both of the open);
- stop: a buy triggers when high >= stop and fills at ref + slip + hs with ref = max(stop, open); a sell
  triggers when low <= stop and fills at ref - slip - hs with ref = min(stop, open). An open already through
  the stop (a gap) fills at the open, worse than the stop (trigger `stop_gap`);
- stop-limit: as the stop, filled only when that price is within the limit, else it keeps working;
- limit: a buy triggers when low + hs <= limit, a sell when high - hs >= limit (hs of the limit price), and
  both fill at exactly the limit with no slippage, never at a better open (as the quote model, SPEC §7.2).
`FillDecision.slippage` is `slip` only (comparable with live fills, measured against the real ask or bid);
`hs` is recorded in the snapshot. The open is deliberately not checked against the bar's range: the
same-bar worst-case pass (P5-T4) re-applies a bar with its open replaced by the entry's fill price.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams, QuoteFillModel
from trader.broker.types import Q4, ZERO, Fees, FillDecision, NoFill, OrderSpec, Side
from trader.market.types import Candle

CANDLE_SNAPSHOT_SOURCE = "candle_1m"

Priced = tuple[Decimal, Decimal, Decimal, str]  # price, slippage per share, half spread, trigger


def candle_snapshot(bar: Candle, half_spread: Decimal, now: datetime) -> dict[str, Any]:
    return {
        "source": CANDLE_SNAPSHOT_SOURCE,
        "start": bar.start.isoformat(),
        "end": bar.end.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "half_spread": str(half_spread),
        "evaluated_at": now.isoformat(),
    }


class CandleFillModel:
    def __init__(self, params: FillParams, half_spread_bps: Decimal) -> None:
        self.params = params
        self.half_spread_bps = half_spread_bps
        self._quote = QuoteFillModel(params)  # the one definition of slippage and fees

    def slip(self, price: Decimal) -> Decimal:
        """max(slippage_min, slippage_bps x price), 4 dp half-up."""
        return self._quote.slip(price)

    def half_spread(self, price: Decimal) -> Decimal:
        """replay.half_spread_bps x price, 4 dp half-up."""
        return (self.half_spread_bps / Decimal(10000) * price).quantize(Q4, ROUND_HALF_UP)

    def fees(self, side: Side, qty: int, price: Decimal) -> Fees:
        """Identical to `QuoteFillModel.fees` for the same params."""
        return self._quote.fees(side, qty, price)

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None:
        out = self.assess(order, market, now)
        return out if isinstance(out, FillDecision) else None

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill:
        if not isinstance(market, Candle):
            raise TypeError(
                "CandleFillModel fills from 1-minute candles; quote fills belong to QuoteFillModel"
            )
        bar = market
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(p <= 0 for p in prices) or bar.low > bar.high or bar.volume < 0:
            return NoFill("bad_bar", f"O {bar.open} H {bar.high} L {bar.low} C {bar.close} V {bar.volume}")
        if bar.volume == 0:
            return NoFill("no_volume")
        priced = self._buy(order, bar) if order.side == "buy" else self._sell(order, bar)
        if isinstance(priced, NoFill):
            return priced
        price, slippage, hs, trigger = priced
        price = price.quantize(Q4, ROUND_HALF_UP)
        if price <= 0:  # a sub-penny open minus slippage: nothing real to sell into (fills.price > 0)
            return NoFill("no_bid", f"fill price {price} is not positive")
        return FillDecision(
            price=price,
            qty=order.qty,
            slippage=slippage,
            fees=self.fees(order.side, order.qty, price),
            quote_snapshot=candle_snapshot(bar, hs, now),
            trigger=trigger,
        )

    def _buy(self, o: OrderSpec, bar: Candle) -> Priced | NoFill:
        if o.order_type == "market":
            s, hs = self.slip(bar.open), self.half_spread(bar.open)
            return bar.open + s + hs, s, hs, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            hs = self.half_spread(o.limit)
            return (o.limit, ZERO, hs, "limit") if bar.low + hs <= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if bar.high < o.stop:
            return NoFill("not_triggered")
        ref = max(o.stop, bar.open)
        s, hs = self.slip(ref), self.half_spread(ref)
        price = ref + s + hs
        if o.order_type == "stop":
            return price, s, hs, "stop_gap" if bar.open > o.stop else "stop"
        assert o.limit is not None
        if price > o.limit:
            return NoFill("above_limit", f"fill price {price} > limit {o.limit}")
        return price, s, hs, "stop_limit"

    def _sell(self, o: OrderSpec, bar: Candle) -> Priced | NoFill:
        if o.order_type == "market":
            s, hs = self.slip(bar.open), self.half_spread(bar.open)
            return bar.open - s - hs, s, hs, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            hs = self.half_spread(o.limit)
            return (o.limit, ZERO, hs, "limit") if bar.high - hs >= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if bar.low > o.stop:
            return NoFill("not_triggered")
        ref = min(o.stop, bar.open)
        s, hs = self.slip(ref), self.half_spread(ref)
        price = ref - s - hs
        if o.order_type == "stop":
            return price, s, hs, "stop_gap" if bar.open < o.stop else "stop"
        assert o.limit is not None
        if price < o.limit:
            return NoFill("below_limit", f"fill price {price} < limit {o.limit}")
        return price, s, hs, "stop_limit"
