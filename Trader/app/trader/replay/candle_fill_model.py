"""The candle fill model (SPEC §7.4; P5-T3): fills from 1-minute bars with slippage, a half-spread estimate
and gap-through handling, implementing the P2 `FillModel` protocol. A `QtQuote` raises TypeError (the mirror
of `QuoteFillModel`). The broker, not the model, checks the bar's time against the order (P5-T4)."""

from datetime import datetime
from decimal import Decimal

from trader.adapters.questrade.models import QtQuote
from trader.broker.fill_model import FillParams
from trader.broker.types import Fees, FillDecision, NoFill, OrderSpec, Side
from trader.market.types import Candle

CANDLE_SNAPSHOT_SOURCE = "candle_1m"


class CandleFillModel:
    def __init__(self, params: FillParams, half_spread_bps: Decimal) -> None:
        self.params = params
        self.half_spread_bps = half_spread_bps

    def slip(self, price: Decimal) -> Decimal:
        """max(slippage_min, slippage_bps x price), 4 dp half-up."""
        raise NotImplementedError("P5-T3")

    def half_spread(self, price: Decimal) -> Decimal:
        """replay.half_spread_bps x price, 4 dp half-up."""
        raise NotImplementedError("P5-T3")

    def fees(self, side: Side, qty: int, price: Decimal) -> Fees:
        """Identical to `QuoteFillModel.fees` for the same params."""
        raise NotImplementedError("P5-T3")

    def evaluate(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | None:
        raise NotImplementedError("P5-T3")

    def assess(self, order: OrderSpec, market: QtQuote | Candle, now: datetime) -> FillDecision | NoFill:
        raise NotImplementedError("P5-T3")
