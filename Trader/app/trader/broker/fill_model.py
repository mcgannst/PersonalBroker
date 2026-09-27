"""Quote-based fill model for live simulation (SPEC §7.2, BR-20).

Assumption: staleness uses QtQuote.last_trade_time, because a Questrade quote carries no separate quote
timestamp (P1-T7). This may over-flag quiet stocks as stale; it is re-checked live in Phase 6 (S2 recheck).
QuoteFillModel implements the FillModel protocol; candle fills for replay are a separate model (P5-T2).
A crossed quote (bid > ask) is unusable, like a one-sided one: NoFill("crossed_quote") (P2-B1 fix round).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import Q4, ZERO, Fees, FillDecision, NoFill, OrderSpec, Side, dec_str
from trader.market.types import Candle
from trader.settings_store import RuntimeSettings

Priced = tuple[Decimal, Decimal, str]  # price, slippage per share, trigger


@dataclass(frozen=True, slots=True)
class FillParams:
    slippage_min: Decimal = Decimal("0.01")
    slippage_bps: Decimal = Decimal("5")
    stale_quote_seconds: float = 10.0
    commission: Decimal = ZERO
    ecn_per_share: Decimal = Decimal("0.0035")
    direct_route: bool = False
    sec_fee_rate: Decimal = Decimal("0.0000206")

    @classmethod
    def from_settings(cls, s: RuntimeSettings) -> "FillParams":
        return cls(
            slippage_min=s.slippage_min,
            slippage_bps=s.slippage_bps,
            stale_quote_seconds=s.stale_quote_seconds,
            commission=s.fees_commission,
            ecn_per_share=s.fees_ecn_per_share,
            direct_route=s.fees_direct_route,
            sec_fee_rate=s.fees_sec_rate,
        )


def _positive(v: Decimal | None) -> Decimal | None:
    return v if v is not None and v > 0 else None


def quote_snapshot(q: QtQuote, now: datetime) -> dict[str, Any]:
    return {
        "symbol_id": q.symbol_id,
        "symbol": q.symbol,
        "bid": dec_str(q.bid),
        "ask": dec_str(q.ask),
        "last": dec_str(q.last),
        "time": q.last_trade_time.isoformat() if q.last_trade_time else None,
        "delay": q.delay,
        "halted": q.is_halted,
        "evaluated_at": now.isoformat(),
    }


class QuoteFillModel:
    def __init__(self, params: FillParams) -> None:
        self._p = params

    def slip(self, price: Decimal) -> Decimal:
        return max(self._p.slippage_min, self._p.slippage_bps / Decimal(10000) * price).quantize(
            Q4, ROUND_HALF_UP
        )

    def fees(self, side: Side, qty: int, price: Decimal) -> Fees:
        ecn = (self._p.ecn_per_share * qty).quantize(Q4, ROUND_HALF_UP) if self._p.direct_route else ZERO
        sec = (self._p.sec_fee_rate * price * qty).quantize(Q4, ROUND_HALF_UP) if side == "sell" else ZERO
        return Fees(commission=self._p.commission, ecn=ecn, sec=sec)

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None:
        out = self.assess(order, market, now)
        return out if isinstance(out, FillDecision) else None

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill:
        if not isinstance(market, QtQuote):
            raise TypeError(
                "QuoteFillModel fills from quotes; candle fills belong to the replay model (P5-T2)"
            )
        quote = market
        if quote.symbol_id != order.symbol_id:
            raise ValueError(
                f"a quote for symbol {quote.symbol_id} can't fill an order for symbol {order.symbol_id}"
            )
        if quote.is_halted:
            return NoFill("halted")
        if quote.delay is None or quote.delay > 0:  # None: Questrade omitted it, so never assume real-time
            return NoFill("delayed_quote", f"delay={quote.delay}")
        if quote.last_trade_time is None:
            return NoFill("stale_quote", "the quote has no time")
        age = (now - quote.last_trade_time).total_seconds()
        if age > self._p.stale_quote_seconds:
            return NoFill("stale_quote", f"the quote is {age:.1f}s old")
        bid, ask, last = _positive(quote.bid), _positive(quote.ask), _positive(quote.last)
        if bid is not None and ask is not None and bid > ask:  # a broken book: no price in it is real
            return NoFill("crossed_quote", f"bid {bid} > ask {ask}")
        priced: Priced | NoFill
        if order.side == "buy":
            if ask is None:
                return NoFill("no_ask")
            priced = self._buy(order, ask, last)
        else:
            if bid is None:
                return NoFill("no_bid")
            priced = self._sell(order, bid, last)
        if isinstance(priced, NoFill):
            return priced
        price, slippage, trigger = priced
        price = price.quantize(Q4, ROUND_HALF_UP)
        if price <= 0:  # a sub-penny bid minus slippage: nothing real to sell into (fills.price > 0)
            return NoFill("no_bid", f"fill price {price} is not positive")
        return FillDecision(
            price=price,
            qty=order.qty,
            slippage=slippage,
            fees=self.fees(order.side, order.qty, price),
            quote_snapshot=quote_snapshot(quote, now),
            trigger=trigger,
        )

    def _buy(self, o: OrderSpec, ask: Decimal, last: Decimal | None) -> Priced | NoFill:
        if o.order_type == "market":
            s = self.slip(ask)
            return ask + s, s, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            return (o.limit, ZERO, "limit") if ask <= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if not ((last is not None and last >= o.stop) or ask >= o.stop):
            return NoFill("not_triggered")
        if o.order_type == "stop":
            ref = max(o.stop, ask)
            s = self.slip(ref)
            return ref + s, s, "stop"
        assert o.limit is not None
        s = self.slip(ask)
        if ask + s > o.limit:
            return NoFill("above_limit", f"ask {ask} + slippage {s} > limit {o.limit}")
        return ask + s, s, "stop_limit"

    def _sell(self, o: OrderSpec, bid: Decimal, last: Decimal | None) -> Priced | NoFill:
        if o.order_type == "market":
            s = self.slip(bid)
            return bid - s, s, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            return (o.limit, ZERO, "limit") if bid >= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if not ((last is not None and last <= o.stop) or bid <= o.stop):
            return NoFill("not_triggered")
        if o.order_type == "stop":
            ref = min(o.stop, bid)
            s = self.slip(ref)
            return ref - s, s, "stop"
        assert o.limit is not None
        s = self.slip(bid)
        if bid - s < o.limit:
            return NoFill("below_limit", f"bid {bid} - slippage {s} < limit {o.limit}")
        return bid - s, s, "stop_limit"
