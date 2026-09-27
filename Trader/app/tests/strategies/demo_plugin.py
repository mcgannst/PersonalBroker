"""A minimal plug-in the registry tests load through a patched entry point."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from trader.broker.types import Fill
from trader.market.calendar import SessionCalendar
from trader.strategies.base import Intent, ScheduledEvent, SessionOffset, StrategyContext


class DemoParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    threshold: int = Field(5, ge=1, le=10)
    at: str = "open+15m"


class DemoStrategy:
    key = "demo"
    version = "0.1.0"
    kind: Literal["entry", "overlay"] = "entry"
    params_model = DemoParams

    def __init__(self, params: DemoParams | None = None) -> None:
        self.params = params or DemoParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent("demo_event", SessionOffset.parse(self.params.at))]

    async def on_event(self, ctx: StrategyContext, event: ScheduledEvent) -> list[Intent]:
        return []

    async def on_fill(self, ctx: StrategyContext, fill: Fill) -> list[Intent]:
        return []


class Mislabeled(DemoStrategy):
    key = "not_demo"
