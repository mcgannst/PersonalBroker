"""spy_overlay 1.0.0 (kind = overlay): hold into the close or exit at 15:30 ET (SPEC §5.3, BR-12)."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from trader.broker.types import Fill
from trader.market.calendar import SessionCalendar
from trader.settings_store import TICKER_PATTERN
from trader.strategies.base import Exit, Intent, ScheduledEvent, SessionOffset, StrategyContext

DECISION_EVENT = "overlay_decision"
Q6 = Decimal("0.000001")


class SpyOverlayParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_at: str = "close-30m"
    benchmark: str = Field("SPY", pattern=TICKER_PATTERN)
    signal: Literal["rest_of_day"] = "rest_of_day"  # SPY return from the prior close to now

    @field_validator("decision_at")
    @classmethod
    def _offset(cls, v: str) -> str:
        SessionOffset.parse(v)
        return v


class SpyOverlay:
    key = "spy_overlay"
    version = "1.0.0"
    kind: Literal["entry", "overlay"] = "overlay"
    params_model = SpyOverlayParams

    def __init__(self, params: SpyOverlayParams | None = None) -> None:
        self.params = params or SpyOverlayParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent(DECISION_EVENT, SessionOffset.parse(self.params.decision_at))]

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
        price = None if q is None or q.is_halted else (q.last or q.last_regular)
        if prior is None or prior <= 0 or price is None:
            ctx.note(
                "overlay: no benchmark data, holding",
                level="warning",
                decision="hold",
                prior_close=None if prior is None else str(prior),
                price=None if price is None else str(price),
                position_ids=position_ids,
            )
            return []
        ret = ((price - prior) / prior).quantize(Q6, ROUND_HALF_UP)
        decision = "exit" if ret <= 0 else "hold"
        ctx.note(
            "overlay: decision",
            decision=decision,
            spy_return=str(ret),
            prior_close=str(prior),
            price=str(price),
            position_ids=position_ids,
        )
        if decision == "hold":
            return []
        return [Exit(pid, "market", None, "overlay_negative") for pid in position_ids]

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        return []
