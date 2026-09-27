"""Risk manager (SPEC §6.1, BR-40): sizes entries and runs the six checks in SPEC order.

Exits and cancels are never blocked, so a kill switch can't trap an open position (Review Focus 5).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from trader.broker.types import AccountState, OrderSpec, OrderView, PositionView
from trader.market.calendar import SessionCalendar
from trader.settings_store import Market, RuntimeSettings
from trader.strategies.base import Cancel, EnterLong, Exit, Intent

ProposalKind = Literal["entry", "stop", "exit", "cancel"]
RiskCheck = Literal[
    "kill_switch",
    "daily_loss",
    "max_positions",
    "market_hours",
    "settled_cash",
    "market_enabled",
    "zero_shares",
    "invalid",
]


@dataclass(frozen=True, slots=True)
class RiskContext:
    now: datetime
    session_date: date
    settings: RuntimeSettings
    account: AccountState
    positions: Mapping[int, PositionView]
    orders: Mapping[int, OrderView]
    strategy_config_id: int
    blocking_switch: str | None = None
    daily_pnl_pct: Decimal = Decimal(0)
    entries_today: int = 0
    max_positions: int = 1
    symbol_market: Market | None = None
    reference_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizedOrder:
    intent: Intent
    kind: ProposalKind
    qty: int
    spec: OrderSpec | None
    cancel_order_id: int | None = None
    position_id: int | None = None
    sizing: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Rejection:
    intent: Intent
    check: RiskCheck
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def _floor(v: Decimal) -> int:
    return int(v.to_integral_value(rounding=ROUND_FLOOR))


class RiskManager:
    def __init__(self, calendar: SessionCalendar) -> None:
        self._cal = calendar

    def evaluate(self, intent: Intent, ctx: RiskContext) -> SizedOrder | Rejection:
        if isinstance(intent, Cancel):
            return self._cancel(intent, ctx)
        if isinstance(intent, Exit):
            return self._exit(intent, ctx)
        return self._entry(intent, ctx)

    @staticmethod
    def _cancel(intent: Cancel, ctx: RiskContext) -> SizedOrder | Rejection:
        order = ctx.orders.get(intent.order_id)
        if order is None or order.status != "working":
            return Rejection(intent, "invalid", f"order {intent.order_id} is not working")
        return SizedOrder(
            intent, "cancel", order.qty, None, cancel_order_id=order.id, position_id=order.position_id
        )

    @staticmethod
    def _exit(intent: Exit, ctx: RiskContext) -> SizedOrder | Rejection:
        pos = ctx.positions.get(intent.position_id)
        if pos is None:
            return Rejection(intent, "invalid", f"position {intent.position_id} is not open")
        kind: Literal["stop", "exit"] = "stop" if intent.order_type == "stop" else "exit"
        try:
            spec = OrderSpec(
                pos.symbol_id,
                "sell",
                intent.order_type,
                pos.qty,
                stop=intent.stop,
                purpose=kind,
                position_id=pos.id,
                strategy_config_id=pos.strategy_config_id,
                reason=intent.reason,
            )
        except ValueError as exc:
            return Rejection(intent, "invalid", str(exc))
        return SizedOrder(intent, kind, pos.qty, spec, position_id=pos.id)

    def _entry(self, intent: EnterLong, ctx: RiskContext) -> SizedOrder | Rejection:
        """Run the six SPEC §6.1 checks, then size the entry.

        `ctx.account.buying_power` is already the right cash for the account mode: the broker computes it as
        settled cash when `cash_account_mode` is on and total cash when it is off (P2-T5
        `SimBroker.account_state`). The risk manager never recomputes it; `sizing["cash_account_mode"]`
        only records which mode the buying power was computed under, for the audit trail.
        """
        s = ctx.settings
        if ctx.blocking_switch is not None:
            return Rejection(intent, "kill_switch", f"kill switch {ctx.blocking_switch} is tripped")
        if ctx.daily_pnl_pct <= -s.killswitch_daily_loss_pct:
            return Rejection(
                intent,
                "daily_loss",
                f"today's P&L {ctx.daily_pnl_pct} is at the -{s.killswitch_daily_loss_pct} limit",
            )
        if ctx.entries_today >= ctx.max_positions:
            return Rejection(
                intent, "max_positions", f"{ctx.entries_today} of {ctx.max_positions} entries used today"
            )
        if not self._cal.is_session(ctx.session_date):
            return Rejection(intent, "market_hours", f"{ctx.session_date} is not a trading session")
        open_ = self._cal.session_open(ctx.session_date)
        cutoff = self._cal.session_close(ctx.session_date) - timedelta(
            minutes=s.no_entry_before_close_minutes
        )
        if not open_ <= ctx.now < cutoff:
            return Rejection(
                intent,
                "market_hours",
                f"entries are allowed from the open until "
                f"{s.no_entry_before_close_minutes} minutes before the close",
            )
        buying_power = ctx.account.buying_power
        if buying_power <= 0:
            what = "settled cash" if s.cash_account_mode else "cash"
            return Rejection(intent, "settled_cash", f"no {what} to buy with")
        if ctx.symbol_market is None or ctx.symbol_market not in s.markets_enabled:
            return Rejection(intent, "market_enabled", f"market {ctx.symbol_market} is not enabled")

        if intent.order_type == "stop":
            entry = intent.stop
        elif intent.order_type in ("limit", "stop_limit"):
            entry = intent.limit
        else:
            entry = ctx.reference_price
        if entry is None or entry <= 0:
            return Rejection(intent, "invalid", "no entry price to size from")
        if (
            intent.stop_loss <= 0
        ):  # second safety net: a strategy bug must never size off a stop at or below 0
            return Rejection(intent, "invalid", f"stop_loss {intent.stop_loss} is not above zero")
        per_share = entry - intent.stop_loss
        if per_share <= 0:
            return Rejection(
                intent, "invalid", f"stop_loss {intent.stop_loss} is not below the entry {entry}"
            )
        risk_dollars = ctx.account.equity * s.risk_pct
        shares_risk = _floor(risk_dollars / per_share)
        shares_cash = _floor(buying_power / (entry * (1 + s.slippage_buffer)))
        shares = min(shares_risk, shares_cash)
        sizing = {
            "equity": str(ctx.account.equity),
            "buying_power": str(buying_power),
            "risk_pct": str(s.risk_pct),
            "risk_dollars": str(risk_dollars),
            "entry": str(entry),
            "stop_loss": str(intent.stop_loss),
            "per_share_risk": str(per_share),
            "slippage_buffer": str(s.slippage_buffer),
            "shares_risk": str(shares_risk),
            "shares_cash": str(shares_cash),
            "shares": str(max(shares, 0)),
            "limited_by": "risk" if shares_risk <= shares_cash else "cash",
            "cash_account_mode": s.cash_account_mode,
        }
        if shares <= 0:
            return Rejection(intent, "zero_shares", "the position size rounds to zero shares", sizing)
        spec = OrderSpec(
            intent.symbol_id,
            "buy",
            intent.order_type,
            shares,
            stop=intent.stop,
            limit=intent.limit,
            purpose="entry",
            strategy_config_id=ctx.strategy_config_id,
            stop_loss=intent.stop_loss,
            reason=intent.reason,
        )
        return SizedOrder(intent, "entry", shares, spec, sizing=sizing)
