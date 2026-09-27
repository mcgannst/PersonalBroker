"""spy_overlay 1.0.0 (kind = overlay): hold into the close or exit at 15:30 ET (SPEC §5.3, BR-12)."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader.adapters.questrade.models import QtQuote
from trader.broker.types import Fill, dec_str
from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date
from trader.settings_store import TICKER_PATTERN
from trader.strategies.base import Exit, Intent, ScheduledEvent, SessionOffset, StrategyContext

DECISION_EVENT = "overlay_decision"
Q6 = Decimal("0.000001")


class SpyOverlayParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_at: str = "close-30m"
    benchmark: str = Field("SPY", pattern=TICKER_PATTERN)
    signal: Literal["rest_of_day"] = "rest_of_day"  # SPY return from the prior close to now
    # The runtime stale_quote_seconds setting is not visible to strategies, so the overlay has its own bound.
    max_quote_age_seconds: int = Field(120, ge=1, le=3600)

    @field_validator("decision_at")
    @classmethod
    def _before_close(cls, v: str) -> str:
        off = SessionOffset.parse(v)
        if off.anchor != "close" or off.seconds >= 0:
            raise ValueError(f"decision_at {v!r} must be a negative offset from the close (e.g. 'close-30m')")
        return v


def _price(q: QtQuote | None) -> Decimal | None:
    """The last trade, or the last regular-hours trade; halted, missing or non-positive prices are None."""
    if q is None or q.is_halted:
        return None
    for p in (q.last, q.last_regular):
        if p is not None and p > 0:
            return p
    return None


class SpyOverlay:
    key = "spy_overlay"
    version = "1.0.0"
    kind: Literal["entry", "overlay"] = "overlay"
    params_model = SpyOverlayParams

    def __init__(self, params: SpyOverlayParams | None = None) -> None:
        self.params = params or SpyOverlayParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent(DECISION_EVENT, SessionOffset.parse(self.params.decision_at))]

    def _stale_reason(self, ctx: StrategyContext, q: QtQuote) -> str | None:
        if q.delay is None or q.delay > 0:  # None: Questrade omitted it, so never assume real-time
            return "delayed"
        if q.last_trade_time is None:
            return "no_trade_time"
        if et_date(q.last_trade_time) != ctx.session_date:
            return "not_this_session"
        age = (ctx.clock.now() - q.last_trade_time).total_seconds()
        if age > self.params.max_quote_age_seconds:
            return "stale"
        return None

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        if event.key != DECISION_EVENT:
            return []
        bench = self.params.benchmark
        position_ids = [p.id for p in ctx.positions]
        sid = (await ctx.data.symbol_ids([bench])).get(bench)
        if sid is None:
            ctx.note(
                "overlay: benchmark unknown, holding",
                level="error",
                decision="hold",
                benchmark=bench,
                position_ids=position_ids,
            )
            return []
        prior = await ctx.data.prior_close(sid, ctx.session_date)
        q = (await ctx.data.quotes([sid])).get(sid)
        price = _price(q)
        quote_info: dict[str, Any] = {
            "benchmark": bench,
            "quote_time": None if q is None or q.last_trade_time is None else q.last_trade_time.isoformat(),
            "delay": None if q is None else q.delay,
        }
        if prior is None or prior <= 0 or q is None or price is None:
            ctx.note(
                "overlay: no benchmark data, holding",
                level="warning",
                decision="hold",
                reason="missing",
                prior_close=dec_str(prior),
                price=dec_str(price),
                position_ids=position_ids,
                **quote_info,
            )
            return []
        stale = self._stale_reason(ctx, q)
        if stale is not None:
            ctx.note(
                "overlay: benchmark quote is not current, holding",
                level="warning",
                decision="hold",
                reason=stale,
                prior_close=str(prior),
                price=str(price),
                position_ids=position_ids,
                **quote_info,
            )
            return []
        ret = (price - prior) / prior
        decision = "exit" if ret <= 0 else "hold"  # the sign of the unrounded return decides
        exiting = {
            o.position_id
            for o in ctx.working_orders
            if o.purpose == "exit" and o.position_id is not None  # a protective stop is not an exit
        }
        ctx.note(
            "overlay: decision",
            decision=decision,
            spy_return=str(ret.quantize(Q6, ROUND_HALF_UP)),  # rounded for display only
            prior_close=str(prior),
            price=str(price),
            position_ids=position_ids,
            already_exiting=sorted(pid for pid in position_ids if pid in exiting),
            **quote_info,
        )
        if decision == "hold":
            return []
        return [Exit(pid, "market", None, "overlay_negative") for pid in position_ids if pid not in exiting]

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        return []
