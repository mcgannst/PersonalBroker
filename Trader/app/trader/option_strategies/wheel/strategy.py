"""The wheel as an option strategy plug-in (OPTSIM task plan T11; feature plan §5).

It gathers the inputs of the wheel rules spec (WS) from the context and its own tables, calls the pure rule
modules, and turns each recommended action into intents (the host makes them orders), owner prompts, alerts
and journal rows. It never sizes collateral, fills or places anything itself.

- `opt_daily` (scheduled): every open wheel position is evaluated once per session, then alerts, then new
  puts for approved tickers in rank order.
- `screen` (manual, Saturday cron): the market-wide candidate screen.
- `postclose` (built in): the quarter-end benchmark review.
- Fills and lifecycle events move the WS §6 state machine in `wheel_positions`.

What the owner must decide is asked through prompts. A ticker-level prompt (new candidate, a caution to
acknowledge) follows from the ticker's row; a position-level prompt is kept in the plug-in state under
`pos:<id>`, with the last alert snapshot and a decision the owner made while the market was closed. An
unanswered prompt changes nothing: the rules keep returning the same action and no order is sent."""

import asyncio
import dataclasses
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.market.calendar import SessionCalendar
from trader.market.clock import et_date
from trader.option_strategies.base import (
    POSTCLOSE_EVENT,
    CloseStructure,
    OpenStructure,
    OptionEvent,
    OptionIntent,
    OptionStrategyContext,
    PanelActionRequest,
    PanelActionResult,
    RollStructure,
    ScheduledEvent,
    SellShares,
    SessionOffset,
    StrategyPanel,
)
from trader.option_strategies.wheel import alerts, calc, evaluate, panel, screen, screener, select, stops
from trader.option_strategies.wheel.config import WheelParams
from trader.option_strategies.wheel.inputs import (
    AccountInputs,
    CallPosition,
    OptionRow,
    OwnerInputs,
    PutPosition,
    SharesPosition,
)
from trader.option_strategies.wheel.store import (
    APPROVED,
    CANDIDATE,
    EVALUATE,
    REJECTED,
    PositionRow,
    TickerRow,
    WheelStore,
    jsonable,
)
from trader.options.types import (
    ContractKey,
    LegSpec,
    LifecycleEvent,
    OptionContract,
    OptionFillEvent,
    OptionQuote,
    OptPositionView,
    OwnerPromptRequest,
    PromptChoice,
    PromptView,
    Right,
    StructureView,
    UnderlyingFacts,
    dte,
    round_tick,
)

KEY = "wheel"
DAILY_EVENT = "opt_daily"
SCREEN_EVENT = "screen"
ACCOUNT_SCOPE = "account"  # state: {"new_positions_paused": bool, ...}
SYSTEM_ACTOR = "strategy:wheel"
PANEL_ACTOR = "owner:panel"
SCREEN_POOL_FACTOR = 3  # the market screen scores at most this many times the cap, then keeps the best
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DEFAULTS = WheelParams()
_SNAPSHOT_DATES = (
    "today",
    "expiry",
    "next_earnings_date",
    "next_ex_dividend_date",
    "fresh_cash_date",
    "last_review_date",
)
_SNAPSHOT_DECIMALS = ("price", "strike", "net_cost")


@dataclass
class _Outcome:
    """What one evaluation of one position decided."""

    action: str
    reason: str
    intents: list[OptionIntent] = field(default_factory=list)
    prompt: OwnerPromptRequest | None = None
    data: dict[str, Any] = field(default_factory=dict)
    snapshot: alerts.AlertSnapshot | None = None


# --- small pure helpers -------------------------------------------------------------------------------------


def pos_scope(position_id: int) -> str:
    return f"pos:{position_id}"


def _store(ctx: OptionStrategyContext) -> WheelStore:
    return WheelStore(ctx.factory, ctx.clock, ctx.run_id)


def _paused(ctx: OptionStrategyContext) -> bool:
    return bool((ctx.state.get(ACCOUNT_SCOPE) or {}).get("new_positions_paused"))


def _account(ctx: OptionStrategyContext) -> AccountInputs:
    """WS §3.4 for the one Options pool: the wheel's cash is the account's cash, and every reservation in
    the pool (manual ones included) counts as open put collateral."""
    return AccountInputs(ctx.account.cash, ctx.account.cash, ctx.account.reserved, _paused(ctx))


def _owner(t: TickerRow | None) -> OwnerInputs:
    if t is None:
        return OwnerInputs()
    return OwnerInputs(t.would_own, t.ownership_reason, t.thesis_broken, t.acknowledged)


def _row(contract: OptionContract, quote: OptionQuote | None, today: date) -> OptionRow:
    key = ContractKey(contract.underlying, contract.expiry, contract.strike, contract.right)
    days = dte(contract.expiry, today)
    if quote is None:
        return OptionRow(key, None, None, None, None, None, contract.is_monthly, days)
    return OptionRow(
        key, quote.bid, quote.ask, quote.delta, quote.iv, quote.open_interest, contract.is_monthly, days
    )


def _row_json(row: OptionRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "expiry": row.expiry.isoformat(),
        "strike": str(row.strike),
        "right": row.contract.right,
        "bid": None if row.bid is None else str(row.bid),
        "ask": None if row.ask is None else str(row.ask),
        "delta": None if row.delta is None else str(row.delta),
        "iv": None if row.iv is None else str(row.iv),
        "open_interest": row.open_interest,
        "dte": row.dte,
    }


def screen_json(result: screen.ScreenResult, has_facts: bool, today: date) -> dict[str, Any]:
    """A screen result as JSON: what `wheel_tickers.last_screen`, the order evidence and the panel hold."""
    return {
        "ticker": result.ticker,
        "verdict": result.verdict,
        "tests": [
            {"number": t.number, "name": t.name, "status": t.status, "reason": t.reason} for t in result.tests
        ],
        "passes": result.passes,
        "flags": list(result.flags),
        "disqualifiers": list(result.disqualifiers),
        "unacknowledged": list(result.unacknowledged),
        "expiry": None if result.expiry is None else result.expiry.expiry.isoformat(),
        "reference": _row_json(result.reference),
        "chosen": _row_json(result.chosen),
        "target_delta": None if result.target_delta is None else str(result.target_delta),
        "breakeven": None if result.breakeven is None else str(result.breakeven),
        "stale": result.stale,
        "has_facts": has_facts,
        "screened_on": today.isoformat(),
    }


def tests_text(shot: dict[str, Any] | None) -> str:
    """The verdict and the ten tests, one per line, for a prompt body."""
    if not shot:
        return "Not screened yet."
    lines = [
        f"Verdict: {shot['verdict']}" + (" (quotes are stale: verify live)" if shot.get("stale") else "")
    ]
    lines += [f"{t['number']}. {t['name']}: {t['status']} ({t['reason']})" for t in shot.get("tests", [])]
    if shot.get("flags"):
        lines.append("Flags: " + ", ".join(shot["flags"]))
    return "\n".join(lines)


def _cycle_suffix(cycle: int) -> str:
    return f":{cycle}" if cycle else ""


def candidate_prompt(t: TickerRow, cycle: int) -> OwnerPromptRequest:
    return OwnerPromptRequest(
        kind="candidate",
        scope_key=t.ticker,
        dedupe_key=f"wheel:cand:{t.ticker}{_cycle_suffix(cycle)}",
        title=f"Wheel candidate {t.ticker}: would you own it?",
        body=tests_text(t.last_screen) + "\n\nApprove with one sentence on why you would own it, or reject.",
        choices=(PromptChoice("a", "Approve"), PromptChoice("r", "Reject")),
        needs_text=True,
        data={"ticker": t.ticker},
    )


def ack_prompt(t: TickerRow, test: str, cycle: int) -> OwnerPromptRequest:
    reason = next((x["reason"] for x in (t.last_screen or {}).get("tests", []) if x["name"] == test), "")
    return OwnerPromptRequest(
        kind="ack_caution",
        scope_key=t.ticker,
        dedupe_key=f"wheel:ack:{t.ticker}:{test}{_cycle_suffix(cycle)}",
        title=f"{t.ticker}: acknowledge the {test} caution?",
        body=f"The {test} test is CAUTION ({reason}). No put is sold on {t.ticker} until it is acknowledged.",
        choices=(PromptChoice("a", "Acknowledge"), PromptChoice("k", "Skip")),
        needs_text=False,
        data={"ticker": t.ticker, "test": test},
    )


def _position_prompt(
    pos: PositionRow,
    kind: str,
    key: str,
    title: str,
    body: str,
    choices: tuple[tuple[str, str], ...],
    *,
    needs_text: bool = False,
    default: str | None = None,
) -> OwnerPromptRequest:
    return OwnerPromptRequest(
        kind=kind,
        scope_key=pos.ticker,
        dedupe_key=key,
        title=title,
        body=body,
        choices=tuple(PromptChoice(code, label) for code, label in choices),
        needs_text=needs_text,
        default_choice=default,
        data={"ticker": pos.ticker, "position_id": pos.id},
    )


def _request_json(req: OwnerPromptRequest) -> dict[str, Any]:
    return {
        "kind": req.kind,
        "scope_key": req.scope_key,
        "dedupe_key": req.dedupe_key,
        "title": req.title,
        "body": req.body,
        "choices": [[c.code, c.label] for c in req.choices],
        "needs_text": req.needs_text,
        "default_choice": req.default_choice,
        "data": dict(req.data),
    }


def _request_from(raw: dict[str, Any]) -> OwnerPromptRequest:
    return OwnerPromptRequest(
        kind=raw["kind"],
        scope_key=raw["scope_key"],
        dedupe_key=raw["dedupe_key"],
        title=raw["title"],
        body=raw["body"],
        choices=tuple(PromptChoice(code, label) for code, label in raw["choices"]),
        needs_text=raw["needs_text"],
        default_choice=raw["default_choice"],
        data=raw["data"],
    )


def _snapshot_from(raw: dict[str, Any] | None) -> alerts.AlertSnapshot | None:
    if not raw:
        return None
    values = dict(raw)
    for name in _SNAPSHOT_DATES:
        if values.get(name):
            values[name] = date.fromisoformat(values[name])
    for name in _SNAPSHOT_DECIMALS:
        if values.get(name) is not None:
            values[name] = Decimal(values[name])
    return alerts.AlertSnapshot(**values)


def _structure(ctx: OptionStrategyContext, structure_id: int | None) -> StructureView | None:
    return next((s for s in ctx.structures if s.id == structure_id), None)


def _option(structure: StructureView | None) -> OptPositionView | None:
    """The structure's open option position (a wheel structure holds one contract at a time)."""
    if structure is None:
        return None
    return next((p for p in structure.positions if p.qty != 0 and p.contract is not None), None)


def _et(moment: datetime | None) -> date | None:
    return None if moment is None else et_date(moment)


def _quarter(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3


# --- the plug-in --------------------------------------------------------------------------------------------


class WheelStrategy:
    key = KEY
    version = "1.0.0"
    params_model = WheelParams
    manual_events: tuple[str, ...] = (SCREEN_EVENT,)

    def __init__(self, params: WheelParams | None = None) -> None:
        self.params = params or WheelParams()

    def schedule(self, cal: SessionCalendar) -> list[ScheduledEvent]:
        return [ScheduledEvent(DAILY_EVENT, SessionOffset.parse(self.params.daily_event_offset))]

    async def watch_underlyings(self, ctx: OptionStrategyContext) -> set[str]:
        store = _store(ctx)
        tickers = {t.ticker for t in store.tickers() if t.status != REJECTED}
        tickers |= {p.ticker for p in store.open_positions()}
        return tickers | {self._benchmark(ctx)}

    def _benchmark(self, ctx: OptionStrategyContext) -> str:
        return self.params.stops_benchmark_ticker or ctx.settings.benchmark_ticker

    # --- inputs ---------------------------------------------------------------------------------------------

    async def _trend(self, ctx: OptionStrategyContext, ticker: str) -> dict[str, Decimal | None]:
        """The moving average and its earlier value from daily bars, only when the owner changed the
        look-backs (the stored facts use the defaults)."""
        period, back = self.params.screen_sma_period, self.params.screen_sma_slope_lookback_sessions
        if (period, back) == (_DEFAULTS.screen_sma_period, _DEFAULTS.screen_sma_slope_lookback_sessions):
            return {}
        start = ctx.session_date - timedelta(days=(period + back) * 2 + 14)
        try:
            bars = await ctx.market.daily_bars(ticker, start, ctx.session_date - timedelta(days=1))
        except LookupError:
            bars = []
        closes = [bar.close for bar in bars]
        if len(closes) < period + back:
            return {"sma50": None, "sma50_prior": None}
        return {
            "sma50": sum(closes[-period:], Decimal(0)) / period,
            "sma50_prior": sum(closes[-period - back : -back], Decimal(0)) / period,
        }

    async def _facts(
        self, ctx: OptionStrategyContext, ticker: str, symbol_id: int, override: str | None
    ) -> tuple[UnderlyingFacts, bool]:
        """The ticker's facts with the owner's security-type override and the live price; whether a facts
        row exists. Never None: without a row every test reports MISSING_DATA."""
        stored = await ctx.market.facts(ticker)
        facts = stored or UnderlyingFacts(symbol_id=symbol_id, as_of=ctx.session_date, ticker=ticker)
        changes: dict[str, Any] = dict(await self._trend(ctx, ticker))
        if override:
            changes["security_type"] = override
        try:
            quote = await ctx.market.underlying_quote(ticker)
        except LookupError:
            quote = None
        if quote is not None and quote.last is not None:
            changes["price"] = quote.last
        return dataclasses.replace(facts, **changes), stored is not None

    async def _rows(
        self,
        ctx: OptionStrategyContext,
        ticker: str,
        expiry: date,
        right: Right,
        min_strike: Decimal | None = None,
    ) -> list[OptionRow]:
        found = await ctx.market.quotes_for_expiry(ticker, expiry, right, min_strike)
        return [_row(c, q, ctx.session_date) for c, q in found if not c.adjusted]

    async def _screen(
        self,
        ctx: OptionStrategyContext,
        ticker: str,
        symbol_id: int,
        owner: OwnerInputs,
        override: str | None,
        account: AccountInputs,
    ) -> tuple[screen.ScreenResult, bool]:
        """WS §4 for one ticker now. Quotes read while the market is closed are flagged stale, never
        refused (WS §3.5)."""
        cfg, today = self.params, ctx.session_date
        facts, has_facts = await self._facts(ctx, ticker, symbol_id, override)
        try:
            expiries = await ctx.market.expiries(ticker)
        except LookupError:
            expiries = []
        picked = select.expiry(expiries, facts.next_earnings_date, today, cfg).expiry
        puts = {picked.expiry: await self._rows(ctx, ticker, picked.expiry, "put")} if picked else {}
        market_open = ctx.market.is_open(ctx.clock.now())
        return screen.run(facts, owner, account, expiries, puts, today, market_open, cfg), has_facts

    async def _screen_ticker(
        self, ctx: OptionStrategyContext, store: WheelStore, t: TickerRow, account: AccountInputs
    ) -> screen.ScreenResult:
        """Screen a listed ticker, store the verdict on its row and journal it."""
        result, has_facts = await self._screen(
            ctx, t.ticker, t.symbol_id, _owner(t), t.security_type_override, account
        )
        shot = screen_json(result, has_facts, ctx.session_date)
        store.save_screen(t.ticker, result.verdict, shot)
        store.add_event("screen", t.symbol_id, ctx.session_date, result.verdict, data=shot)
        return result

    # --- intents --------------------------------------------------------------------------------------------

    def _limit(
        self, ctx: OptionStrategyContext, row: OptionRow | None, sign: int
    ) -> tuple[bool, Decimal | None]:
        """Whether a one-leg order on `row` can be priced, and its net limit (`sign` +1 to sell it, -1 to
        buy it back): None when the broker walks the order from the midpoint itself, else the midpoint on
        the tick grid, which needs a two-sided quote."""
        if self.params.walk_orders:
            return True, None
        if row is None or row.bid is None or row.ask is None:
            return False, None
        return True, round_tick(sign * (row.bid + row.ask) / 2, ctx.settings.tick_size, "up")

    def _sell_to_open(
        self,
        ctx: OptionStrategyContext,
        row: OptionRow,
        qty: int,
        reason: str,
        evidence: dict[str, Any],
    ) -> list[OptionIntent]:
        """WS §5.3: a limit sell that starts at the midpoint and walks down to the bid, with the standing
        take-profit. Nothing when an opening order on the ticker is still working."""
        ticker = row.contract.underlying
        if any(o.intent == "open" and o.underlying == ticker for o in ctx.orders):
            ctx.note(f"{ticker}: an opening order is already working", underlying=ticker)
            return []
        priced, limit = self._limit(ctx, row, 1)
        if not priced:
            ctx.note(f"{ticker}: no two-sided quote to price the order", "warning", underlying=ticker)
            return []
        return [
            OpenStructure(
                legs=(LegSpec("option", "sell", 1, ticker, row.contract),),
                qty=qty,
                order_type="limit",
                net_limit=limit,
                tif="day",
                reason=reason,
                evidence=jsonable(evidence),
                walk=self.params.walk_orders,
                take_profit_pct=self.params.manage_profit_target_pct_of_premium,
            )
        ]

    def _close(
        self, ctx: OptionStrategyContext, structure: StructureView, row: OptionRow | None, reason: str
    ) -> list[OptionIntent]:
        """Buy the option back: a limit at the midpoint, walked up to the ask. Nothing when an order on the
        structure (this close, or the standing take-profit) is already working."""
        if any(o.structure_id == structure.id for o in ctx.orders):
            ctx.note(
                f"{structure.underlying}: an order on structure {structure.id} is already working",
                underlying=structure.underlying,
                structure_id=structure.id,
            )
            return []
        priced, limit = self._limit(ctx, row, -1)
        if not priced:
            ctx.note(
                f"{structure.underlying}: no two-sided quote to price the close",
                "warning",
                underlying=structure.underlying,
            )
            return []
        return [CloseStructure(structure.id, "limit", limit, reason, tif="day", walk=self.params.walk_orders)]

    def _sell_shares(self, ctx: OptionStrategyContext, pos: PositionRow, reason: str) -> list[OptionIntent]:
        structure = _structure(ctx, pos.shares_structure_id)
        if structure is None or any(o.structure_id == structure.id for o in ctx.orders):
            return []
        return [SellShares(structure.id, reason)]

    def _roll(
        self, ctx: OptionStrategyContext, structure: StructureView, candidate: OptionRow, credit: Decimal
    ) -> list[OptionIntent]:
        """One two-leg order: buy the open put, sell the candidate. The limit is one tick of net credit, so
        it fills at the market's net and never for a debit (WS §8.1)."""
        if any(o.structure_id == structure.id for o in ctx.orders):
            return []
        return [
            RollStructure(
                structure_id=structure.id,
                open_legs=(LegSpec("option", "sell", 1, structure.underlying, candidate.contract),),
                order_type="limit",
                net_limit=ctx.settings.tick_size,
                reason="wheel: ROLL_PUT",
                evidence=jsonable({"net_credit": credit, "candidate": _row_json(candidate)}),
                tif="day",
                walk=False,
                take_profit_pct=self.params.manage_profit_target_pct_of_premium,
            )
        ]

    # --- the daily evaluation of one position ---------------------------------------------------------------

    async def _next_monthly_puts(
        self, ctx: OptionStrategyContext, ticker: str, after: date
    ) -> list[OptionRow]:
        later = [e.expiry for e in await ctx.market.expiries(ticker) if e.expiry > after and e.is_monthly]
        return await self._rows(ctx, ticker, min(later), "put") if later else []

    async def _put(
        self,
        ctx: OptionStrategyContext,
        pos: PositionRow,
        facts: UnderlyingFacts,
        biz: evaluate.BusinessCheck,
        decision: str | None,
        snapshot: alerts.AlertSnapshot,
    ) -> _Outcome:
        cfg, today = self.params, ctx.session_date
        structure = _structure(ctx, pos.put_structure_id)
        held = _option(structure)
        if structure is None or held is None or held.contract is None:
            return _Outcome("HOLD", "the put is no longer open: waiting for its fill or expiry record")
        contract = held.contract
        row = _row(contract, (await ctx.market.quotes([contract.id])).get(contract.id), today)
        snapshot = dataclasses.replace(snapshot, strike=contract.strike, expiry=contract.expiry)
        if decision == "close":
            intents = self._close(ctx, structure, row, "wheel: owner chose to close")
            return _Outcome("CLOSE_PUT_NOW", "the owner chose to close", intents, snapshot=snapshot)
        rolling = decision == "roll"
        next_puts: list[OptionRow] = []
        if rolling or cfg.manage_itm_at_time_exit_preference == "ROLL_ONCE":
            next_puts = await self._next_monthly_puts(ctx, pos.ticker, contract.expiry)
        if rolling:
            # The owner's roll before earnings: the same pick as WS §8.1, without its earnings condition.
            best = next(
                (
                    r
                    for r in sorted(next_puts, key=lambda r: -r.strike)
                    if r.strike <= contract.strike
                    and r.bid is not None
                    and row.ask is not None
                    and r.bid - row.ask > 0
                ),
                None,
            )
            if best is None or best.bid is None or row.ask is None or not stops.can_roll(pos.roll_count, cfg):
                return _Outcome(
                    "HOLD", "the owner chose to roll, but no net-credit roll is allowed", snapshot=snapshot
                )
            credit = best.bid - row.ask
            intents = self._roll(ctx, structure, best, credit)
            return _Outcome(
                "ROLL_PUT", "the owner chose to roll", intents, data={"net_credit": credit}, snapshot=snapshot
            )
        put = PutPosition(
            pos.ticker,
            contract.strike,
            contract.expiry,
            pos.contracts,
            held.avg_price,
            pos.roll_count,
            pos.total_put_premium,
        )
        action = evaluate.put(put, facts, row, next_puts, biz, today, cfg)
        out = _Outcome(action.kind, action.reason, data=dict(action.detail), snapshot=snapshot)
        if action.kind in (
            "CLOSE_PUT_NOW",
            "CLOSE_PUT_PROFIT",
            "CLOSE_PUT_BEFORE_EARNINGS",
            "CLOSE_PUT_TIME",
        ):
            out.intents = self._close(ctx, structure, row, f"wheel: {action.kind}")
        elif action.kind == "ROLL_PUT" and action.candidate is not None:
            out.intents = self._roll(ctx, structure, action.candidate, action.detail["net_credit"])
        elif action.kind == "REVIEW_BEFORE_EARNINGS":
            earnings = action.detail.get("next_earnings_date")
            out.prompt = _position_prompt(
                pos,
                "review_before_earnings",
                f"wheel:earn:{pos.id}:{earnings}",
                f"{pos.ticker}: earnings on {earnings}, before the put expires",
                f"The {contract.strike} put expiring {contract.expiry} is not in profit "
                f"(sold for {held.avg_price}, ask {row.ask}). Close it, hold it, or roll it? "
                "Until you answer it is held.",
                (("c", "Close"), ("h", "Hold"), ("o", "Roll")),
                default="h",
            )
        elif action.kind == "PIN_RISK":
            out.prompt = _position_prompt(
                pos,
                "pin_risk",
                f"wheel:pin:{pos.id}",
                f"{pos.ticker}: pin risk on the {contract.strike} put today",
                f"The stock is at {facts.price}, within the pin band of the strike on expiry day. "
                "Buy the put back, or accept possible assignment? Until you answer it is accepted.",
                (("b", "Buy to close"), ("a", "Accept")),
                default="a",
            )
        return out

    async def _shares(
        self,
        ctx: OptionStrategyContext,
        store: WheelStore,
        pos: PositionRow,
        t: TickerRow | None,
        facts: UnderlyingFacts,
        biz: evaluate.BusinessCheck,
        decision: str | None,
        snapshot: alerts.AlertSnapshot,
    ) -> _Outcome:
        cfg, today = self.params, ctx.session_date
        if pos.assignment_strike is None or pos.net_cost is None:
            return _Outcome("HOLD", "the shares have no recorded net cost")
        answered = _et(pos.fresh_cash_at) if pos.fresh_cash_answer is not None else None
        reviewed = _et(pos.drawdown_review_at)
        snapshot = dataclasses.replace(
            snapshot, net_cost=pos.net_cost, fresh_cash_date=answered, last_review_date=reviewed
        )
        if decision == "sell":
            intents = self._sell_shares(ctx, pos, "wheel: owner chose to sell")
            return _Outcome("SELL_SHARES", "the owner chose to sell", intents, snapshot=snapshot)
        shares = SharesPosition(
            pos.ticker,
            pos.contracts,
            pos.assignment_strike,
            pos.net_cost,
            pos.fresh_cash_answer,
            answered,
            reviewed,
        )
        expiries = await ctx.market.expiries(pos.ticker)
        calls: list[OptionRow] = []
        if pos.fresh_cash_answer and biz.passes:  # the only case that reaches the call selection
            for e in expiries:
                if cfg.cc_dte_min <= e.dte <= cfg.cc_dte_max:
                    calls += await self._rows(ctx, pos.ticker, e.expiry, "call", pos.net_cost)
        action = evaluate.shares(shares, facts, calls, expiries, biz, today, cfg)
        out = _Outcome(action.kind, action.reason, data=dict(action.detail), snapshot=snapshot)
        if action.kind == "SELL_SHARES":
            out.intents = self._sell_shares(ctx, pos, "wheel: SELL_SHARES")
        elif action.kind == "SELL_CALL" and action.candidate is not None:
            evidence = {"net_cost": pos.net_cost, "call": _row_json(action.candidate), "why": action.reason}
            out.intents = self._sell_to_open(
                ctx, action.candidate, pos.contracts, "wheel: SELL_CALL", evidence
            )
        elif action.kind == "RUN_FRESH_CASH_TEST":
            shot: dict[str, Any] | None = None
            if t is not None:
                result, has_facts = await self._screen(
                    ctx, t.ticker, t.symbol_id, _owner(t), t.security_type_override, _account(ctx)
                )
                shot = screen_json(result, has_facts, today)
            out.prompt = _position_prompt(
                pos,
                "fresh_cash",
                f"wheel:fresh:{pos.id}:{today}",
                f"{pos.ticker}: fresh-cash test",
                f"If you held cash instead of these {pos.contracts * 100} shares, would you open a position "
                f"in {pos.ticker} today at {facts.price}? The purchase price is not part of the question. "
                "No call is sold until you answer; a No sells the shares.\n\n" + tests_text(shot),
                (("y", "Yes"), ("n", "No")),
            )
        elif action.kind == "DRAWDOWN_REVIEW":
            out.prompt = _position_prompt(
                pos,
                "drawdown_review",
                f"wheel:dd:{pos.id}:{today}",
                f"{pos.ticker}: drawdown review",
                f"The shares are at {facts.price}, at or below the review level under the net cost of "
                f"{pos.net_cost}. Write why the stock still passes, or sell.",
                (("w", "Write the reason"), ("s", "Sell")),
                needs_text=True,
            )
        return out

    async def _call(
        self,
        ctx: OptionStrategyContext,
        pos: PositionRow,
        facts: UnderlyingFacts,
        biz: evaluate.BusinessCheck,
        snapshot: alerts.AlertSnapshot,
    ) -> _Outcome:
        structure = _structure(ctx, pos.call_structure_id)
        held = _option(structure)
        if structure is None or held is None or held.contract is None or pos.net_cost is None:
            return _Outcome("HOLD", "the call is no longer open: waiting for its fill or expiry record")
        contract = held.contract
        row = _row(contract, (await ctx.market.quotes([contract.id])).get(contract.id), ctx.session_date)
        snapshot = dataclasses.replace(
            snapshot, strike=contract.strike, expiry=contract.expiry, net_cost=pos.net_cost
        )
        call = CallPosition(
            pos.ticker, contract.strike, contract.expiry, pos.contracts, held.avg_price, pos.net_cost
        )
        action = evaluate.call(call, facts, row, biz, ctx.session_date, self.params)
        out = _Outcome(action.kind, action.reason, data=dict(action.detail), snapshot=snapshot)
        if action.kind in ("CLOSE_CALL_AND_SELL_SHARES", "CLOSE_CALL_PROFIT", "CLOSE_OR_EXPIRE_CALL"):
            # After CLOSE_CALL_AND_SELL_SHARES the shares are sold from on_fill, once the call is closed.
            out.intents = self._close(ctx, structure, row, f"wheel: {action.kind}")
        return out

    def _raise_alerts(
        self,
        ctx: OptionStrategyContext,
        store: WheelStore,
        pos: PositionRow,
        doc: dict[str, Any],
        snapshot: alerts.AlertSnapshot,
    ) -> None:
        """WS §12 from yesterday's snapshot and today's. The worker already alerts on a touched strike of
        every short option when `options.strike_touch_alerts` is on, so the wheel leaves that one to it."""
        for alert in alerts.detect(_snapshot_from(doc.get("snapshot")), snapshot, self.params):
            if alert.kind == "STRIKE_TOUCHED" and ctx.settings.strike_touch_alerts:
                continue
            key = f"wheel:{alert.kind}:{pos.id}:{ctx.session_date}"
            ctx.alert(alert.kind, alert.message, key, underlying=pos.ticker)
            store.add_event(
                "alert",
                pos.symbol_id,
                ctx.session_date,
                alert.message,
                position_id=pos.id,
                data={"alert": alert.kind},
            )
        doc["snapshot"] = jsonable(dataclasses.asdict(snapshot))

    def _keep_prompt(
        self, ctx: OptionStrategyContext, doc: dict[str, Any], wanted: OwnerPromptRequest
    ) -> OwnerPromptRequest:
        """The position's prompt of this kind that is still open (or answered and not yet delivered) stays
        as it is, so its key (which may hold the date it became due) and its text do not change from day to
        day. A new one is asked only once the old one is done."""
        for raw in (doc.get("prompts") or {}).values():
            if raw["kind"] != wanted.kind:
                continue
            asked = ctx.prompt(raw["dedupe_key"])
            if asked is None or asked.status == "pending":
                return _request_from(raw)
            if asked.status == "answered" and asked.delivered_at is None:
                return _request_from(raw)
        return wanted

    async def _evaluate(
        self, ctx: OptionStrategyContext, store: WheelStore, pos: PositionRow, *, daily: bool
    ) -> list[OptionIntent]:
        """Evaluate one open position and apply the action. `daily` is the once-per-session evaluation
        (the `wheel_events` unique key); otherwise it is the follow-up of a fill, a lifecycle event or an
        answer, journalled as an `action` row."""
        today = ctx.session_date
        if daily and store.evaluated(pos.id, today):
            return []
        t = store.ticker(pos.ticker)
        owner = _owner(t)
        override = t.security_type_override if t is not None else None
        facts, _ = await self._facts(ctx, pos.ticker, pos.symbol_id, override)
        biz = evaluate.business_check(facts, owner, self.params)
        t2 = screen.profitability(facts)
        base = alerts.AlertSnapshot(
            ticker=pos.ticker,
            today=today,
            state="NONE",
            price=facts.price,
            next_earnings_date=facts.next_earnings_date,
            next_ex_dividend_date=facts.next_ex_dividend_date,
            profitability=t2.status,
            balance_sheet=screen.balance_sheet(facts, t2.status, self.params).status,
        )
        doc = ctx.state.get(pos_scope(pos.id)) or {}
        decision = doc.pop("decision", None)
        if pos.state == "PUT_OPEN":
            out = await self._put(ctx, pos, facts, biz, decision, dataclasses.replace(base, state="PUT_OPEN"))
        elif pos.state == "SHARES_HELD":
            out = await self._shares(
                ctx, store, pos, t, facts, biz, decision, dataclasses.replace(base, state="SHARES_HELD")
            )
        else:
            out = await self._call(ctx, pos, facts, biz, dataclasses.replace(base, state="CALL_OPEN"))
        prompt = self._keep_prompt(ctx, doc, out.prompt) if out.prompt is not None else None
        data = {
            **out.data,
            "state": pos.state,
            "intents": len(out.intents),
            "business_check": list(biz.failed),
            "prompt": None if prompt is None else prompt.dedupe_key,
        }
        written = store.add_event(
            EVALUATE if daily else "action",
            pos.symbol_id,
            today,
            out.reason,
            position_id=pos.id,
            action=out.action,
            data=data,
        )
        if not written:  # another run evaluated this position today
            return []
        if daily and out.snapshot is not None:
            self._raise_alerts(ctx, store, pos, doc, out.snapshot)
        doc["prompts"] = {} if prompt is None else {prompt.dedupe_key: _request_json(prompt)}
        ctx.state.put(pos_scope(pos.id), doc)
        ctx.note(
            f"{pos.ticker} {pos.state}: {out.action} ({out.reason})",
            underlying=pos.ticker,
            position_id=pos.id,
            action=out.action,
        )
        return out.intents

    # --- new puts -------------------------------------------------------------------------------------------

    async def _entries(
        self, ctx: OptionStrategyContext, store: WheelStore, only: str | None = None
    ) -> list[OptionIntent]:
        """Screen the approved tickers that have no open wheel position (a manual position on the ticker
        does not count) and open puts in rank order while the cash still fits."""
        cfg = self.params
        if _paused(ctx):
            ctx.note("new wheel positions are paused: no ticker screened for entry")
            return []
        account = _account(ctx)
        held = {p.ticker for p in store.open_positions()}
        allowed: list[screen.ScreenResult] = []
        for t in store.tickers(APPROVED):
            if (only is not None and t.ticker != only) or t.ticker in held:
                continue
            result = await self._screen_ticker(ctx, store, t, account)
            if result.entry_allowed:
                allowed.append(result)
            else:
                waiting = f", waiting for {', '.join(result.unacknowledged)}" if result.unacknowledged else ""
                ctx.note(f"{t.ticker}: no entry, {result.verdict}{waiting}", underlying=t.ticker)
        intents: list[OptionIntent] = []
        for result in screen.rank(allowed):
            if result.chosen is None:
                continue
            collateral = calc.collateral(result.chosen.strike, cfg.entry_contracts_per_ticker)
            blocked = stops.can_open(False, account, collateral, cfg)
            if blocked is not None:
                ctx.note(f"{result.ticker}: no entry, {blocked}", underlying=result.ticker)
                break
            opened = self._sell_to_open(
                ctx,
                result.chosen,
                cfg.entry_contracts_per_ticker,
                "wheel: open put",
                screen_json(result, True, ctx.session_date),
            )
            if opened:
                intents += opened
                account = dataclasses.replace(
                    account, open_put_collateral_usd=account.open_put_collateral_usd + collateral
                )
        return intents

    # --- events ---------------------------------------------------------------------------------------------

    async def on_event(self, ctx: OptionStrategyContext, event: OptionEvent) -> list[OptionIntent]:
        if event.key == DAILY_EVENT:
            return await self._daily(ctx)
        if event.key == SCREEN_EVENT:
            return await self._market_screen(ctx)
        if event.key == POSTCLOSE_EVENT:
            return await self._postclose(ctx)
        return []

    async def _daily(self, ctx: OptionStrategyContext) -> list[OptionIntent]:
        store = _store(ctx)
        intents: list[OptionIntent] = []
        for pos in store.open_positions():
            intents += await self._evaluate(ctx, store, pos, daily=True)
        intents += await self._entries(ctx, store)
        account = _account(ctx)
        for t in store.tickers(CANDIDATE):  # a candidate is offered to the owner once it has been screened
            if t.last_screen is None:
                await self._screen_ticker(ctx, store, t, account)
        return intents

    async def _market_screen(self, ctx: OptionStrategyContext) -> list[OptionIntent]:
        """D8: new tickers from the FinViz screen, scored with the ten tests, the best few added as
        candidates. Each is offered to the owner with a `candidate` prompt (see `prompts`)."""
        cfg, store = self.params, _store(ctx)
        known = {t.ticker for t in store.tickers()}
        found = await asyncio.to_thread(screener.market_candidates, screener.source, cfg, known)
        account = _account(ctx)
        scored: dict[str, tuple[screen.ScreenResult, bool, int]] = {}
        for ticker in found[: cfg.market_screen_max_candidates * SCREEN_POOL_FACTOR]:
            try:
                quote = await ctx.market.underlying_quote(ticker)
            except LookupError:
                quote = None
            if quote is None:
                ctx.note(f"market screen: {ticker} is unknown to the market data", underlying=ticker)
                continue
            result, has_facts = await self._screen(ctx, ticker, quote.symbol_id, OwnerInputs(), None, account)
            scored[ticker] = (result, has_facts, quote.symbol_id)
        ranked = screen.rank([s[0] for s in scored.values()])[: cfg.market_screen_max_candidates]
        for result in ranked:
            _, has_facts, symbol_id = scored[result.ticker]
            store.add_ticker(symbol_id, result.ticker, origin="screen", actor=SYSTEM_ACTOR)
            if has_facts:  # without facts the daily event screens it after the next facts refresh
                shot = screen_json(result, has_facts, ctx.session_date)
                store.save_screen(result.ticker, result.verdict, shot)
                store.add_event("screen", symbol_id, ctx.session_date, result.verdict, data=shot)
        ctx.note(
            f"market screen: {len(found)} new ticker(s), {len(ranked)} added as candidates",
            added=[r.ticker for r in ranked],
        )
        return []

    async def _postclose(self, ctx: OptionStrategyContext) -> list[OptionIntent]:
        """WS §10 at a quarter end: when the benchmark's quarter was flat or falling and the account still
        trailed it, new positions are paused until the owner clears the pause."""
        today = ctx.session_date
        try:
            if _quarter(ctx.calendar.next_session(today)) == _quarter(today):
                return []
            day = ctx.calendar.previous_session(date(today.year, 3 * ((today.month - 1) // 3) + 1, 1))
        except ValueError:  # outside the calendar's range
            return []
        base = ctx.equity_on(day)
        while base is None and day < today - timedelta(days=1):  # the run started inside the quarter
            day += timedelta(days=1)
            base = ctx.equity_on(day)
        end = ctx.equity_on(today) or ctx.account.equity
        benchmark = self._benchmark(ctx)
        try:
            bars = await ctx.market.daily_bars(benchmark, day, today)
        except LookupError:
            bars = []
        key = f"wheel:BENCHMARK_REVIEW_DUE:{today}"
        if not base or len(bars) < 2 or not bars[0].close:
            ctx.alert(
                "BENCHMARK_REVIEW_DUE",
                f"Quarter end: the wheel account could not be compared with {benchmark} (no data).",
                key,
            )
            return []
        account_return = end / base - 1
        benchmark_return = bars[-1].close / bars[0].close - 1
        pause = stops.benchmark_review(benchmark_return, account_return, self.params)
        if pause:
            ctx.state.put(
                ACCOUNT_SCOPE,
                {
                    "new_positions_paused": True,
                    "since": today.isoformat(),
                    "benchmark": benchmark,
                    "benchmark_return": str(benchmark_return),
                    "account_return": str(account_return),
                },
            )
        ctx.alert(
            "BENCHMARK_REVIEW_DUE",
            f"Quarter end: the account returned {account_return:.1%} and {benchmark} {benchmark_return:.1%} "
            f"since {day}." + (" New wheel positions are paused until you clear the pause." if pause else ""),
            key,
            paused=pause,
        )
        return []

    # --- fills ----------------------------------------------------------------------------------------------

    async def on_fill(self, ctx: OptionStrategyContext, fill: OptionFillEvent) -> list[OptionIntent]:
        store = _store(ctx)
        if fill.intent == "open":
            leg = next((x for x in fill.legs if x.instrument == "option" and x.contract_id is not None), None)
            if leg is None or leg.contract_id is None:
                return []
            contract = await ctx.market.contract(leg.contract_id)
            if contract.right == "put":
                await self._put_sold(ctx, store, fill, contract, leg.price, leg.quote)
            else:
                self._call_sold(ctx, store, fill, contract, leg.price)
            return []
        pos = store.by_structure(fill.structure_id)
        if pos is None:
            ctx.note(f"fill of order {fill.order_id}: no open wheel position on its structure", "warning")
            return []
        fees = pos.fees + fill.fees
        if fill.intent == "roll":
            opened = next((x for x in fill.legs if x.effect == "open" and x.contract_id is not None), None)
            entry = dict(pos.entry)
            if opened is not None and opened.contract_id is not None:
                contract = await ctx.market.contract(opened.contract_id)
                entry.update(strike=contract.strike, expiry=contract.expiry, premium=opened.price)
            store.update_position(
                pos.id,
                roll_count=pos.roll_count + 1,
                total_put_premium=pos.total_put_premium + fill.net_price,
                fees=fees,
                entry=entry,
            )
            self._journal(ctx, store, pos, "rolled", f"put rolled for a net credit of {fill.net_price}")
            return []
        if fill.structure_id == pos.put_structure_id:
            premium = pos.total_put_premium + fill.net_price  # the buy-back is a debit: a negative net
            result = premium * 100 * pos.contracts - fees
            store.close_position(pos.id, "put_closed", result, total_put_premium=premium, fees=fees)
            ctx.state.delete(pos_scope(pos.id))
            self._journal(
                ctx, store, pos, "put_closed", f"put bought back at {-fill.net_price}: result {result}"
            )
            return []
        if fill.structure_id == pos.call_structure_id:
            # A new call needs a new fresh-cash answer (WS §9.2), so the last one is cleared.
            pos = store.update_position(
                pos.id,
                state="SHARES_HELD",
                call_structure_id=None,
                total_call_premium=pos.total_call_premium + fill.net_price,
                fees=fees,
                fresh_cash_answer=None,
            )
            self._journal(ctx, store, pos, "call_closed", f"call bought back at {-fill.net_price}")
            return await self._evaluate(ctx, store, pos, daily=False)
        if fill.structure_id == pos.shares_structure_id:
            sale = next((x.price for x in fill.legs if x.instrument == "shares"), fill.net_price)
            self._cycle_over(ctx, store, pos, "shares_sold", sale, fees)
        return []

    async def _put_sold(
        self,
        ctx: OptionStrategyContext,
        store: WheelStore,
        fill: OptionFillEvent,
        contract: OptionContract,
        premium: Decimal,
        quote: Any,
    ) -> None:
        """PUT_OPEN, with the WS §5.4 record."""
        t = store.ticker(contract.underlying)
        chosen = ((t.last_screen or {}).get("chosen") or {}) if t is not None else {}
        if (
            chosen.get("strike") != str(contract.strike)
            or chosen.get("expiry") != contract.expiry.isoformat()
        ):
            chosen = {}
        facts = await ctx.market.facts(contract.underlying)
        entry = {
            "open_date": ctx.session_date,
            "ticker": contract.underlying,
            "expiry": contract.expiry,
            "strike": contract.strike,
            "contracts": fill.qty,
            "premium": premium,
            "breakeven": calc.breakeven(contract.strike, premium),
            "delta_at_entry": quote.get("delta") or chosen.get("delta"),
            "iv_at_entry": quote.get("iv") or chosen.get("iv"),
            "next_earnings_date": None if facts is None else facts.next_earnings_date,
            "ownership_reason": "" if t is None else t.ownership_reason,
            "usd_cad_rate": ctx.usd_cad_rate,
        }
        pos = store.open_put(
            symbol_id=contract.underlying_symbol_id,
            ticker=contract.underlying,
            structure_id=fill.structure_id,
            contracts=fill.qty,
            premium=premium,
            fees=fill.fees,
            entry=entry,
        )
        if t is not None and t.acknowledged:  # an acknowledgement covers one entry
            store.update_ticker(t.ticker, SYSTEM_ACTOR, acknowledged=frozenset())
        self._journal(ctx, store, pos, "put_sold", f"sold the {contract.strike} put for {premium}")

    def _call_sold(
        self,
        ctx: OptionStrategyContext,
        store: WheelStore,
        fill: OptionFillEvent,
        contract: OptionContract,
        premium: Decimal,
    ) -> None:
        pos = next((p for p in store.open_positions() if p.ticker == contract.underlying), None)
        if pos is None or pos.state != "SHARES_HELD":
            ctx.note(f"{contract.underlying}: a call filled with no shares held by the wheel", "warning")
            return
        store.update_position(
            pos.id,
            state="CALL_OPEN",
            call_structure_id=fill.structure_id,
            total_call_premium=pos.total_call_premium + premium,
            fees=pos.fees + fill.fees,
        )
        self._journal(ctx, store, pos, "call_sold", f"sold the {contract.strike} call for {premium}")

    def _journal(
        self, ctx: OptionStrategyContext, store: WheelStore, pos: PositionRow, what: str, reason: str
    ) -> None:
        """A fill or lifecycle event in the journal. No `action`: the panel's next action stays the last
        evaluation's."""
        store.add_event(
            "lifecycle", pos.symbol_id, ctx.session_date, reason, position_id=pos.id, data={"event": what}
        )
        ctx.note(f"{pos.ticker}: {reason}", underlying=pos.ticker, position_id=pos.id)

    def _cycle_over(
        self,
        ctx: OptionStrategyContext,
        store: WheelStore,
        pos: PositionRow,
        why: str,
        sale_price: Decimal,
        fees: Decimal,
    ) -> None:
        """The shares are gone (sold, or called away): state NONE with the full-cycle result (WS §11). The
        ticker goes back to `candidate`: the wheel never re-enters it without a new approval (WS §9.6)."""
        result = calc.full_cycle_result(
            total_put_premium=pos.total_put_premium,
            total_call_premium=pos.total_call_premium,
            dividends=pos.dividends,
            sale_price=sale_price,
            assignment_strike=pos.assignment_strike or sale_price,
            contracts=pos.contracts,
            fees=fees,
        )
        store.close_position(pos.id, why, result, fees=fees)
        ctx.state.delete(pos_scope(pos.id))
        store.update_ticker(pos.ticker, SYSTEM_ACTOR, status=CANDIDATE, acknowledged=frozenset())
        store.save_screen(pos.ticker, None, None)
        what = "shares sold" if why == "shares_sold" else "shares called away"
        self._journal(ctx, store, pos, why, f"{what} at {sale_price}: full-cycle result {result}")

    # --- lifecycle ------------------------------------------------------------------------------------------

    async def on_lifecycle(self, ctx: OptionStrategyContext, event: LifecycleEvent) -> list[OptionIntent]:
        store = _store(ctx)
        pos = store.by_structure(event.structure_id)
        if pos is None:
            return []  # the cycle is already over (for example the second event of a call-away)
        right = None if event.contract is None else event.contract.right
        is_put = event.structure_id == pos.put_structure_id or right == "put"
        if event.kind == "expired":
            if is_put:
                result = pos.total_put_premium * 100 * pos.contracts - pos.fees
                store.close_position(pos.id, "put_expired", result)
                ctx.state.delete(pos_scope(pos.id))
                self._journal(ctx, store, pos, "put_expired", f"put expired worthless: result {result}")
                return []
            return await self._call_gone(ctx, store, pos, "call expired worthless")
        if event.kind in ("assigned", "early_assignment") and is_put:
            strike = event.strike if event.strike is not None else Decimal(str(pos.entry.get("strike", "0")))
            net_cost = calc.net_cost(
                strike,
                pos.total_put_premium,
                pos.total_call_premium,
                include_call_premium=self.params.cc_include_call_premium_in_net_cost,
            )
            pos = store.update_position(
                pos.id,
                state="SHARES_HELD",
                shares_structure_id=event.new_structure_id,
                assignment_strike=strike,
                net_cost=net_cost,
                fresh_cash_answer=None,
            )
            self._journal(ctx, store, pos, "assigned", f"put assigned at {strike}: net cost {net_cost}")
            return await self._evaluate(ctx, store, pos, daily=False)  # the fresh-cash test is due today
        if event.kind in ("called_away", "early_assignment"):
            sale = event.strike if event.strike is not None else (pos.net_cost or Decimal(0))
            self._cycle_over(ctx, store, pos, "called_away", sale, pos.fees)
            return []
        if event.kind == "assigned":  # a call settled in cash: the shares are still held
            return await self._call_gone(ctx, store, pos, "call assigned and settled in cash")
        ctx.note(f"{pos.ticker}: lifecycle event {event.kind} changes nothing", underlying=pos.ticker)
        return []

    async def _call_gone(
        self, ctx: OptionStrategyContext, store: WheelStore, pos: PositionRow, reason: str
    ) -> list[OptionIntent]:
        pos = store.update_position(
            pos.id, state="SHARES_HELD", call_structure_id=None, fresh_cash_answer=None
        )
        self._journal(ctx, store, pos, "call_gone", reason)
        return await self._evaluate(ctx, store, pos, daily=False)

    # --- answers and prompts --------------------------------------------------------------------------------

    async def on_answer(self, ctx: OptionStrategyContext, prompt: PromptView) -> list[OptionIntent]:
        """Store the owner's answer, then act on it now when the market is open; otherwise the next daily
        event does."""
        store = _store(ctx)
        actor = f"prompt:{prompt.answered_via or 'owner'}"
        answer, ticker = prompt.answer, str(prompt.data.get("ticker", ""))
        market_open = ctx.market.is_open(ctx.clock.now())
        if prompt.kind in ("candidate", "ack_caution"):
            t = store.ticker(ticker)
            if t is None:
                return []
            if prompt.kind == "candidate" and answer == "a":
                store.update_ticker(
                    ticker, actor, status=APPROVED, would_own=True, ownership_reason=prompt.answer_text or ""
                )
            elif prompt.kind == "candidate" and answer == "r":
                store.update_ticker(ticker, actor, status=REJECTED, would_own=False)
            elif answer == "a":
                store.update_ticker(ticker, actor, acknowledged=t.acknowledged | {str(prompt.data["test"])})
            store.add_event(
                "answer",
                t.symbol_id,
                ctx.session_date,
                f"{prompt.kind}: {answer}",
                data={"prompt": prompt.id},
            )
            return await self._entries(ctx, store, only=ticker) if answer == "a" and market_open else []
        pos = store.position(int(prompt.data.get("position_id", 0)))
        if pos is None or pos.closed_at is not None:
            return []
        now = ctx.clock.now()
        decision: str | None = None
        if prompt.kind == "fresh_cash" and answer in ("y", "n"):
            pos = store.update_position(pos.id, fresh_cash_answer=answer == "y", fresh_cash_at=now)
        elif prompt.kind == "drawdown_review" and answer == "w":
            pos = store.update_position(
                pos.id, drawdown_review_at=now, drawdown_review_text=prompt.answer_text or ""
            )
        elif prompt.kind == "drawdown_review" and answer == "s":
            decision = "sell"
        elif prompt.kind in ("review_before_earnings", "pin_risk") and answer in ("c", "b"):
            decision = "close"
        elif prompt.kind == "review_before_earnings" and answer == "o":
            decision = "roll"
        doc = ctx.state.get(pos_scope(pos.id)) or {}
        (doc.get("prompts") or {}).pop(prompt.dedupe_key, None)
        if decision is not None:
            doc["decision"] = decision
        ctx.state.put(pos_scope(pos.id), doc)
        store.add_event(
            "answer",
            pos.symbol_id,
            ctx.session_date,
            f"{prompt.kind}: {answer}",
            position_id=pos.id,
            data={"prompt": prompt.id, "via": prompt.answered_via, "text": prompt.answer_text},
        )
        return await self._evaluate(ctx, store, pos, daily=False) if market_open else []

    async def prompts(self, ctx: OptionStrategyContext) -> list[OwnerPromptRequest]:
        """The questions that should be open now. Read from the plug-in's own tables and state only (the
        host asks on every worker step)."""
        store = _store(ctx)
        positions = store.open_positions()
        held = {p.ticker for p in positions}
        wanted: list[OwnerPromptRequest] = []
        for t in store.tickers():
            if t.status == CANDIDATE and t.last_screen is not None:
                wanted.append(candidate_prompt(t, store.cycles(t.symbol_id)))
            elif t.status == APPROVED and t.last_verdict == "NEEDS_REVIEW" and t.ticker not in held:
                unacknowledged = (t.last_screen or {}).get("unacknowledged", [])
                waiting = [x for x in unacknowledged if x not in t.acknowledged]
                cycle = store.cycles(t.symbol_id) if waiting else 0
                wanted += [ack_prompt(t, test, cycle) for test in waiting]
        for pos in positions:
            doc = ctx.state.get(pos_scope(pos.id)) or {}
            wanted += [_request_from(raw) for raw in (doc.get("prompts") or {}).values()]
        return wanted

    # --- the panel ------------------------------------------------------------------------------------------

    async def panel(self, ctx: OptionStrategyContext) -> StrategyPanel:
        store = _store(ctx)
        wanted = await self.prompts(ctx)
        pending = 0
        for req in wanted:
            asked = ctx.prompt(req.dedupe_key)
            if asked is None or asked.status == "pending":
                pending += 1
        return panel.build(
            positions=store.open_positions(),
            tickers=store.tickers(),
            last_actions=store.last_actions(),
            structures=ctx.structures,
            account=ctx.account,
            paused=_paused(ctx),
            benchmark=self._benchmark(ctx),
            pending_prompts=pending,
        )

    async def on_action(self, ctx: OptionStrategyContext, req: PanelActionRequest) -> PanelActionResult:
        """An owner action from the panel. It changes the wheel's own tables and state only."""
        store = _store(ctx)
        text = req.value.strip() if isinstance(req.value, str) else ""
        if req.action == "clear_pause":
            ctx.state.put(
                ACCOUNT_SCOPE, {"new_positions_paused": False, "cleared_at": ctx.clock.now().isoformat()}
            )
            return PanelActionResult(True, "New wheel positions are allowed again")
        if req.action == "add_ticker":
            return await self._add_ticker(ctx, store, text.upper())
        t = store.ticker(req.row_id or "")
        if t is None:
            return PanelActionResult(False, "Pick a ticker row for this action")
        if req.action in ("approve", "set_reason"):
            if not text:
                return PanelActionResult(False, "Write one sentence on why you would own it")
            if req.action == "approve":
                store.update_ticker(
                    t.ticker, PANEL_ACTOR, status=APPROVED, would_own=True, ownership_reason=text
                )
                return PanelActionResult(True, f"{t.ticker} approved")
            store.update_ticker(t.ticker, PANEL_ACTOR, ownership_reason=text)
            return PanelActionResult(True, f"{t.ticker}: ownership reason saved")
        if req.action == "reject":
            store.update_ticker(t.ticker, PANEL_ACTOR, status=REJECTED, would_own=False)
            return PanelActionResult(True, f"{t.ticker} rejected")
        if req.action == "thesis_broken":
            broken = req.value is True or text.lower() in ("true", "1", "yes", "on")
            store.update_ticker(t.ticker, PANEL_ACTOR, thesis_broken=broken)
            return PanelActionResult(True, f"{t.ticker}: thesis {'broken' if broken else 'intact'}")
        if req.action == "security_type":
            if text not in panel.SECURITY_TYPES:
                return PanelActionResult(False, "Unknown security type")
            store.update_ticker(
                t.ticker, PANEL_ACTOR, security_type_override=None if text == panel.AUTO else text
            )
            return PanelActionResult(True, f"{t.ticker}: security type {text}")
        if req.action == "ack_caution":
            waiting = set((t.last_screen or {}).get("unacknowledged", [])) - t.acknowledged
            if not waiting:
                return PanelActionResult(False, f"{t.ticker} has no caution waiting")
            store.update_ticker(t.ticker, PANEL_ACTOR, acknowledged=t.acknowledged | waiting)
            return PanelActionResult(True, f"{t.ticker}: acknowledged {', '.join(sorted(waiting))}")
        if req.action == "rescreen":
            result = await self._screen_ticker(ctx, store, t, _account(ctx))
            return PanelActionResult(True, f"{t.ticker}: {result.verdict}")
        return PanelActionResult(False, "Unknown action")

    async def _add_ticker(
        self, ctx: OptionStrategyContext, store: WheelStore, ticker: str
    ) -> PanelActionResult:
        if not TICKER_PATTERN.fullmatch(ticker):
            return PanelActionResult(False, "That is not a ticker")
        known = store.ticker(ticker)
        if known is not None and known.status != REJECTED:
            return PanelActionResult(False, f"{ticker} is already on the list ({known.status})")
        if known is None:
            try:
                quote = await ctx.market.underlying_quote(ticker)
            except LookupError:
                quote = None
            if quote is None:
                return PanelActionResult(False, f"{ticker} is unknown to the market data")
            store.add_ticker(quote.symbol_id, ticker, origin="manual", actor=PANEL_ACTOR)
        else:
            store.update_ticker(ticker, PANEL_ACTOR, status=CANDIDATE, would_own=None)
        t = store.ticker(ticker)
        if t is not None:
            await self._screen_ticker(ctx, store, t, _account(ctx))
        return PanelActionResult(True, f"{ticker} added as a candidate")
