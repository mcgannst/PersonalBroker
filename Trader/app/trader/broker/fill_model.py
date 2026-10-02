"""Quote-based fill model for live simulation (SPEC §7.2, BR-20).

Freshness (FIX-DAY1, Wed 2026-09-30): a Questrade quote carries no quote timestamp, so the app stamps when it
fetched it (QtQuote.fetched_at). A live two-sided book (bid and ask > 0, not crossed; delay 0 and not halted
are checked first) fetched within stale_quote_seconds is usable however old the last trade is; an old last
trade then triggers no stop (FILLFIX: nor does the book). Without a live book, or without a fetch time
(replay, older callers), the last trade's age decides, as before (P1-T7).
QuoteFillModel implements the FillModel protocol; candle fills for replay are a separate model (P5-T2).
A crossed quote (bid > ask) is unusable, like a one-sided one: NoFill("crossed_quote") (P2-B1 fix round).

FILLFIX (Fri 2026-10-02): stop and stop-limit orders trigger on the LAST TRADE only, as Questrade's do
(a buy-stop when last >= stop, a sell-stop when last <= stop), never on the ask or bid alone. So one quote
can never fill an entry and its protective stop (its last can't be both >= the entry stop and <= the lower
protective stop), which the ask/bid triggers allowed on a wide opening spread (BXDC: bid 17.41 / ask 17.83 /
last 17.72 filled the 17.73 entry and the 17.6678 stop). A stale last trade (see above) triggers nothing: the
next print does. Once triggered the fill is at the ask (buy) or bid (sell) plus slippage, as before. A
triggered entry with a planned stop_loss fills only while ask - bid <= max_spread_stop_fraction x
(stop - stop_loss); a wider spread is NoFill("spread_wide") and the order keeps working. Protective stops and
exits are never held for spread.
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
SPREAD_WIDE = "spread_wide"  # a triggered entry held: the spread is too wide for its stop distance (FILLFIX)


@dataclass(frozen=True, slots=True)
class FillParams:
    slippage_min: Decimal = Decimal("0.01")
    slippage_bps: Decimal = Decimal("5")
    stale_quote_seconds: float = 10.0
    commission: Decimal = ZERO
    ecn_per_share: Decimal = Decimal("0.0035")
    direct_route: bool = False
    sec_fee_rate: Decimal = Decimal("0.0000206")
    max_spread_stop_fraction: Decimal = Decimal("0.5")

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
            max_spread_stop_fraction=s.fill_max_spread_stop_fraction,
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
        "fetched_at": q.fetched_at.isoformat() if q.fetched_at else None,
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
        # every fee is 4 dp like its numeric(14,4) column, so in-memory P&L equals what the ledger stores
        commission = self._p.commission.quantize(Q4, ROUND_HALF_UP)
        return Fees(commission=commission, ecn=ecn, sec=sec)

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
        bid, ask, last = _positive(quote.bid), _positive(quote.ask), _positive(quote.last)
        limit = self._p.stale_quote_seconds
        trade_age = (now - quote.last_trade_time).total_seconds() if quote.last_trade_time else None
        fetch_age = (now - quote.fetched_at).total_seconds() if quote.fetched_at else None
        live_book = bid is not None and ask is not None and bid <= ask
        if live_book and fetch_age is not None:
            # FIX-DAY1: a live two-sided book fetched within the limit is current, however long ago the last
            # trade printed (CLDX Wed 09-30 sat 30 min unfilled on an old last trade with a live book).
            if fetch_age > limit:
                return NoFill("stale_quote", f"the quote was fetched {fetch_age:.1f}s ago")
            if trade_age is None or trade_age > limit:
                last = None  # an old print says nothing about now: it triggers no stop
        else:
            # No live book (or no fetch time: replay, older callers): the last trade's age decides, as before.
            if trade_age is None:
                return NoFill("stale_quote", "the quote has no time")
            if trade_age > limit:
                return NoFill("stale_quote", f"the quote is {trade_age:.1f}s old")
        if bid is not None and ask is not None and bid > ask:  # a broken book: no price in it is real
            return NoFill("crossed_quote", f"bid {bid} > ask {ask}")
        priced: Priced | NoFill
        if order.side == "buy":
            if ask is None:
                return NoFill("no_ask")
            priced = self._buy(order, ask, last)
            if not isinstance(priced, NoFill):
                held = self._spread_hold(order, bid, ask)
                if held is not None:
                    return held
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

    def _spread_hold(self, o: OrderSpec, bid: Decimal | None, ask: Decimal) -> NoFill | None:
        """FILLFIX: a triggered stop entry with a planned stop_loss is held while ask - bid exceeds
        max_spread_stop_fraction x (stop - stop_loss), or while there is no bid to measure the spread. None:
        fill. Exits and entries without a stop price or stop_loss are never held."""
        if o.purpose != "entry" or o.stop is None or o.stop_loss is None:
            return None
        distance = o.stop - o.stop_loss
        if distance <= 0:
            return None
        if bid is None:
            return NoFill(SPREAD_WIDE, "no bid to measure the spread")
        spread = ask - bid
        allowed = (self._p.max_spread_stop_fraction * distance).quantize(Q4, ROUND_HALF_UP)
        if spread > allowed:
            fraction = self._p.max_spread_stop_fraction
            return NoFill(SPREAD_WIDE, f"spread {spread} > {fraction} x stop distance {distance} ({allowed})")
        return None

    def _buy(self, o: OrderSpec, ask: Decimal, last: Decimal | None) -> Priced | NoFill:
        if o.order_type == "market":
            s = self.slip(ask)
            return ask + s, s, "market"
        if o.order_type == "limit":
            assert o.limit is not None
            return (o.limit, ZERO, "limit") if ask <= o.limit else NoFill("not_triggered")
        assert o.stop is not None
        if last is None or last < o.stop:  # FILLFIX: the last trade triggers, never the ask alone
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
        if last is None or last > o.stop:  # FILLFIX: the last trade triggers, never the bid alone
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
