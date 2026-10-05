"""The toy plug-in of acceptance item 6 (OPTSIM task plan §3.10): a complete option strategy in one file,
installed in tests through a patched entry point (`toy_call = "tests.options.toy_plugin:ToyCallBuyer"`).

On its event `toy_buy` (open+5m) it buys one call of the first expiry at least `min_dte` days out, at the
strike nearest the money, with a market order, unless it already holds or is buying one. It has a one-table
panel and one action (`note`, a text the owner can leave)."""

from pydantic import BaseModel, ConfigDict, Field

from trader.market.calendar import SessionCalendar
from trader.option_strategies.base import (
    KeyValue,
    OpenStructure,
    OptionEvent,
    OptionIntent,
    OptionStrategyContext,
    PanelAction,
    PanelActionRequest,
    PanelActionResult,
    PanelColumn,
    PanelRow,
    PanelTable,
    ScheduledEvent,
    SessionOffset,
    StrategyPanel,
)
from trader.options.types import (
    ContractKey,
    LegSpec,
    LifecycleEvent,
    OptionFillEvent,
    OwnerPromptRequest,
    PromptView,
    contract_label,
)

TOY_KEY = "toy_call"
TOY_EVENT = "toy_buy"
NOTE_SCOPE = "note"


class ToyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    underlying: str = Field("F", pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")
    min_dte: int = Field(30, ge=1, le=365)
    at: str = "open+5m"


class ToyCallBuyer:
    key = TOY_KEY
    version = "0.1.0"
    params_model = ToyParams
    manual_events: tuple[str, ...] = (TOY_EVENT,)

    def __init__(self, params: ToyParams | None = None) -> None:
        self.params = params or ToyParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent(TOY_EVENT, SessionOffset.parse(self.params.at))]

    async def watch_underlyings(self, ctx: OptionStrategyContext) -> set[str]:
        return {self.params.underlying}

    async def on_event(self, ctx: OptionStrategyContext, event: OptionEvent) -> list[OptionIntent]:
        if event.key != TOY_EVENT:
            return []
        if ctx.structures or ctx.orders:
            ctx.note("toy_call already holds or is buying a call")
            return []
        ticker = self.params.underlying
        expiry = next((e for e in await ctx.market.expiries(ticker) if e.dte >= self.params.min_dte), None)
        quote = await ctx.market.underlying_quote(ticker)
        if expiry is None or quote is None or quote.last is None:
            ctx.note(f"toy_call found no expiry or price for {ticker}", "warning")
            return []
        last = quote.last
        calls = [s for s in await ctx.market.strikes(ticker, expiry.expiry) if s.call_id is not None]
        if not calls:
            ctx.note(f"toy_call found no call for {ticker} {expiry.expiry}", "warning")
            return []
        strike = min(calls, key=lambda s: (abs(s.strike - last), s.strike)).strike
        leg = LegSpec("option", "buy", 1, ticker, ContractKey(ticker, expiry.expiry, strike, "call"))
        return [
            OpenStructure(
                legs=(leg,),
                qty=1,
                order_type="market",
                net_limit=None,
                tif="day",
                reason=TOY_EVENT,
                evidence={"underlying_last": str(last), "dte": expiry.dte},
            )
        ]

    async def on_fill(self, ctx: OptionStrategyContext, fill: OptionFillEvent) -> list[OptionIntent]:
        return []

    async def on_lifecycle(self, ctx: OptionStrategyContext, event: LifecycleEvent) -> list[OptionIntent]:
        return []

    async def on_answer(self, ctx: OptionStrategyContext, prompt: PromptView) -> list[OptionIntent]:
        return []

    async def prompts(self, ctx: OptionStrategyContext) -> list[OwnerPromptRequest]:
        return []

    async def panel(self, ctx: OptionStrategyContext) -> StrategyPanel:
        note = (ctx.state.get(NOTE_SCOPE) or {}).get("text", "")
        rows = tuple(
            PanelRow(
                id=str(s.id),
                cells={
                    "contract": contract_label(p.contract) if p.contract is not None else s.underlying,
                    "qty": p.qty,
                    "cost": str(p.avg_price),
                },
                actions=("note",),
                detail=(KeyValue("Opened", s.opened_at.isoformat()),),
            )
            for s in ctx.structures
            for p in s.positions
            if p.qty != 0
        )
        return StrategyPanel(
            summary=(
                KeyValue("Calls held", str(len(rows)), "ok" if rows else None),
                KeyValue("Note", note or "none"),
            ),
            tables=(
                PanelTable(
                    key="holdings",
                    title="Holdings",
                    columns=(
                        PanelColumn("contract", "Contract", "text"),
                        PanelColumn("qty", "Quantity", "number"),
                        PanelColumn("cost", "Cost", "money"),
                    ),
                    rows=rows,
                    empty_text="No call held",
                ),
            ),
            actions=(PanelAction("note", "Leave a note", "text"),),
        )

    async def on_action(self, ctx: OptionStrategyContext, req: PanelActionRequest) -> PanelActionResult:
        if req.action != "note":
            return PanelActionResult(False, "Unknown action")
        if not isinstance(req.value, str) or not req.value.strip():
            return PanelActionResult(False, "A note needs some text")
        ctx.state.put(NOTE_SCOPE, {"text": req.value.strip(), "row_id": req.row_id})
        return PanelActionResult(True, "Note saved")
