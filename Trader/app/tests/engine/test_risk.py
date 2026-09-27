from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from trader.broker.types import AccountState, OrderView, PositionView
from trader.engine.risk import Rejection, RiskContext, RiskManager, SizedOrder
from trader.market.calendar import SessionCalendar
from trader.settings_store import RuntimeSettings
from trader.strategies.base import Cancel, EnterLong, Exit

CAL = SessionCalendar()
RISK = RiskManager(CAL)
DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET
ENTRY = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("19.91"), "orb_breakout", {})


def account(equity: str = "720", buying_power: str = "720") -> AccountState:
    return AccountState(
        Decimal(buying_power), Decimal(buying_power), Decimal(buying_power), Decimal(0), Decimal(equity)
    )


POS = PositionView(3, 7, 1, 35, Decimal("20.02"), Decimal("19.91"), T, DAY, None, T, 0)
ORDER = OrderView(9, 7, "buy", "stop", "entry", 35, Decimal("20.01"), None, "working", None, 1, 4, T)


def ctx(**over: Any) -> RiskContext:
    base = RiskContext(
        now=T,
        session_date=DAY,
        settings=RuntimeSettings(),
        account=account(),
        positions={POS.id: POS},
        orders={ORDER.id: ORDER},
        strategy_config_id=1,
        symbol_market="US",
    )
    return replace(base, **over)


def test_sizing_is_the_smaller_of_risk_and_cash_shares() -> None:
    out = RISK.evaluate(ENTRY, ctx())
    assert isinstance(out, SizedOrder) and out.kind == "entry"
    # risk: 720 x 2% = 14.40 / 0.10 = 144; cash: 720 / (20.01 x 1.005) = 35.8 -> 35
    assert out.qty == 35
    assert out.sizing["shares_risk"] == "144" and out.sizing["shares_cash"] == "35"
    assert out.sizing["limited_by"] == "cash" and out.sizing["risk_dollars"] == "14.40"
    assert out.spec is not None
    assert (out.spec.side, out.spec.order_type, out.spec.qty, out.spec.stop) == (
        "buy",
        "stop",
        35,
        Decimal("20.01"),
    )
    assert out.spec.stop_loss == Decimal("19.91") and out.spec.strategy_config_id == 1


def test_risk_limited_when_cash_is_plentiful() -> None:
    out = RISK.evaluate(ENTRY, ctx(account=account(equity="720", buying_power="100000")))
    assert isinstance(out, SizedOrder) and out.qty == 144 and out.sizing["limited_by"] == "risk"


ALL_BAD: dict[str, Any] = {
    "blocking_switch": "max_drawdown_pct",
    "daily_pnl_pct": Decimal("-0.06"),
    "entries_today": 1,
    "now": datetime(2026, 10, 6, 19, 45, tzinfo=UTC),  # 15:45 ET, inside the last 30 minutes
    "account": account(buying_power="0"),
    "symbol_market": "TSX",
}
ORDER_OF_CHECKS = [
    "kill_switch",
    "daily_loss",
    "max_positions",
    "market_hours",
    "settled_cash",
    "market_enabled",
]


def test_checks_run_in_spec_order() -> None:
    bad = dict(ALL_BAD)
    fixes: dict[str, Any] = {
        "blocking_switch": None,
        "daily_pnl_pct": Decimal("-0.01"),
        "entries_today": 0,
        "now": T,
        "account": account(),
        "symbol_market": "US",
    }
    for check, key in zip(ORDER_OF_CHECKS, list(fixes), strict=True):
        out = RISK.evaluate(ENTRY, ctx(**bad))
        assert isinstance(out, Rejection) and out.check == check, (check, out)
        bad[key] = fixes[key]
    assert isinstance(RISK.evaluate(ENTRY, ctx(**bad)), SizedOrder)


def test_zero_shares_is_rejected() -> None:
    out = RISK.evaluate(ENTRY, ctx(account=account(buying_power="15")))
    assert isinstance(out, Rejection) and out.check == "zero_shares" and out.detail["shares_cash"] == "0"


@pytest.mark.parametrize(
    ("now", "ok"),
    [
        (datetime(2026, 10, 6, 13, 29, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 6, 13, 30, tzinfo=UTC), True),
        (datetime(2026, 10, 6, 19, 29, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 6, 19, 30, tzinfo=UTC), False),
    ],
)
def test_entry_window_boundaries(now: datetime, ok: bool) -> None:
    assert isinstance(RISK.evaluate(ENTRY, ctx(now=now)), SizedOrder) is ok


def test_entry_window_follows_an_early_close_and_holidays() -> None:
    early = date(2026, 11, 27)  # close 18:00Z, so no entries from 17:30Z
    assert isinstance(
        RISK.evaluate(ENTRY, ctx(session_date=early, now=datetime(2026, 11, 27, 17, 29, tzinfo=UTC))),
        SizedOrder,
    )
    late = RISK.evaluate(ENTRY, ctx(session_date=early, now=datetime(2026, 11, 27, 17, 30, tzinfo=UTC)))
    assert isinstance(late, Rejection) and late.check == "market_hours"
    holiday = RISK.evaluate(
        ENTRY, ctx(session_date=date(2026, 11, 26), now=datetime(2026, 11, 26, 15, 0, tzinfo=UTC))
    )
    assert isinstance(holiday, Rejection) and holiday.check == "market_hours"


def test_enabled_markets() -> None:
    both = RuntimeSettings(markets_enabled=["US", "TSX"])
    assert isinstance(RISK.evaluate(ENTRY, ctx(symbol_market="TSX", settings=both)), SizedOrder)
    assert isinstance(RISK.evaluate(ENTRY, ctx(symbol_market=None)), Rejection)


def test_market_entries_need_a_reference_price_and_a_stop_below_it() -> None:
    market = EnterLong(7, "market", None, None, Decimal("19.91"), "test", {})
    out = RISK.evaluate(market, ctx())
    assert isinstance(out, Rejection) and out.check == "invalid"
    sized = RISK.evaluate(market, ctx(reference_price=Decimal("20.00")))
    assert isinstance(sized, SizedOrder) and sized.qty == 35
    upside_down = EnterLong(7, "stop", Decimal("20.01"), None, Decimal("20.50"), "test", {})
    bad = RISK.evaluate(upside_down, ctx())
    assert isinstance(bad, Rejection) and bad.check == "invalid"


def test_exits_and_cancels_pass_when_everything_is_tripped() -> None:
    """Review Focus 5: no check can trap an open position."""
    tripped = ctx(**ALL_BAD, session_date=date(2026, 11, 26))  # also a holiday, after hours
    flatten = RISK.evaluate(Exit(POS.id, "market", None, "flatten_close"), tripped)
    assert isinstance(flatten, SizedOrder) and flatten.kind == "exit" and flatten.qty == 35
    assert flatten.spec is not None and flatten.spec.side == "sell" and flatten.spec.position_id == POS.id
    stop = RISK.evaluate(Exit(POS.id, "stop", Decimal("19.91"), "protective_stop"), tripped)
    assert isinstance(stop, SizedOrder) and stop.kind == "stop" and stop.spec is not None
    assert stop.spec.stop == Decimal("19.91") and stop.spec.purpose == "stop"
    cancel = RISK.evaluate(Cancel(ORDER.id, "entry_cancel_at"), tripped)
    assert isinstance(cancel, SizedOrder) and cancel.kind == "cancel" and cancel.cancel_order_id == ORDER.id


def test_exits_and_cancels_of_unknown_things_are_invalid() -> None:
    assert isinstance(RISK.evaluate(Exit(999, "market", None, "x"), ctx()), Rejection)
    assert isinstance(RISK.evaluate(Cancel(999, "x"), ctx()), Rejection)
    stopless = RISK.evaluate(Exit(POS.id, "stop", None, "x"), ctx())
    assert isinstance(stopless, Rejection) and stopless.check == "invalid"
