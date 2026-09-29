"""SIZECAP: the per-stock cap. No entry may cost more than `max_position_pct` of equity (default 10%):
qty = min(risk shares, cap shares, cash shares), where a cap share costs the entry x (1 + slippage_buffer)
plus the per-share ECN fee on direct routing, and the order's commission comes off the cap first. A cap that
buys no share at all rejects the intent as `position_cap`, naming the stock, one share's cost and the cap."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, get_args

from trader.broker.types import AccountState
from trader.engine.risk import Rejection, RiskCheck, RiskContext, RiskManager, SizedOrder
from trader.market.calendar import SessionCalendar
from trader.settings_store import RuntimeSettings
from trader.strategies.base import EnterLong

CAL = SessionCalendar()
RISK = RiskManager(CAL)
DAY = date(2026, 10, 6)
T = datetime(2026, 10, 6, 13, 40, tzinfo=UTC)  # 09:40 ET


def entry(price: str, stop_loss: str) -> EnterLong:
    return EnterLong(7, "stop", Decimal(price), None, Decimal(stop_loss), "orb_breakout", {})


def account(equity: str, buying_power: str) -> AccountState:
    bp = Decimal(buying_power)
    return AccountState(bp, bp, bp, Decimal(0), Decimal(equity))


def ctx(equity: str = "720", buying_power: str = "720", **over: Any) -> RiskContext:
    base = RiskContext(
        now=T,
        session_date=DAY,
        settings=RuntimeSettings(),
        account=account(equity, buying_power),
        positions={},
        orders={},
        strategy_config_id=1,
        symbol_market="US",
        symbol="XYZ",
    )
    return replace(base, **over)


def test_the_default_cap_is_ten_percent_and_binds_on_a_small_account() -> None:
    out = RISK.evaluate(entry("20.01", "19.91"), ctx())
    # cap: 720 x 10% = 72 / (20.01 x 1.005 = 20.11005) = 3.58 -> 3; risk 144; cash 35
    assert isinstance(out, SizedOrder) and out.qty == 3 and out.spec is not None and out.spec.qty == 3
    s = out.sizing
    assert (s["shares_risk"], s["shares_cash"], s["shares_cap"], s["shares"]) == ("144", "35", "3", "3")
    assert s["limited_by"] == "cap" and s["max_position_pct"] == "0.10"
    assert Decimal(s["cap_dollars"]) == Decimal("72")
    assert 3 * Decimal("20.01") * Decimal("1.005") <= Decimal("720") * Decimal("0.10")


def test_risk_binds_when_the_stop_is_wide() -> None:
    # risk: 100000 x 2% = 2000 / 10 = 200; cap: 10000 / 20.11005 = 497; cash 4972
    out = RISK.evaluate(entry("20.01", "10.01"), ctx("100000", "100000"))
    assert isinstance(out, SizedOrder) and out.qty == 200 and out.sizing["limited_by"] == "risk"
    assert out.sizing["shares_cap"] == "497"


def test_cash_binds_when_buying_power_is_short() -> None:
    # cash: 50 / 20.11005 = 2.49 -> 2; cap 1000 / 20.11005 = 49; risk 200 / 0.10 = 2000
    out = RISK.evaluate(entry("20.01", "19.91"), ctx("10000", "50"))
    assert isinstance(out, SizedOrder) and out.qty == 2 and out.sizing["limited_by"] == "cash"


def test_a_share_dearer_than_the_cap_is_rejected_as_position_cap() -> None:
    out = RISK.evaluate(entry("100.00", "99.00"), ctx())
    assert isinstance(out, Rejection) and out.check == "position_cap"
    # one share: 100 x 1.005 = 100.50 against 10% of 720 = 72.00
    assert out.reason == "1 share of XYZ costs $100.50, over the 10% cap $72.00"
    assert out.detail["shares_cap"] == "0" and out.detail["shares"] == "0"
    assert out.detail["limited_by"] == "cap"


def test_position_cap_is_a_typed_risk_check() -> None:
    assert "position_cap" in get_args(RiskCheck)


def test_the_reason_falls_back_to_the_symbol_id_without_a_ticker() -> None:
    out = RISK.evaluate(entry("100.00", "99.00"), ctx(symbol=None))
    assert isinstance(out, Rejection) and out.reason.startswith("1 share of symbol 7 costs $100.50")


def test_the_cap_boundary_is_inclusive() -> None:
    # equity 1005 -> cap 100.50; one share costs 10 x 1.005 = 10.05, so exactly 10 shares fit
    out = RISK.evaluate(entry("10.00", "9.00"), ctx("1005", "100000"))
    assert isinstance(out, SizedOrder) and out.qty == 10 and out.sizing["limited_by"] == "cap"


def test_commission_and_direct_route_ecn_come_off_the_cap() -> None:
    costs = RuntimeSettings(fees_commission=Decimal("1"))
    # (100.50 - 1) / 10.05 = 9.9 -> 9
    out = RISK.evaluate(entry("10.00", "9.00"), ctx("1005", "100000", settings=costs))
    assert isinstance(out, SizedOrder) and out.qty == 9
    ecn = RuntimeSettings(fees_direct_route=True, fees_ecn_per_share=Decimal("0.01"))
    # 100.50 / (10.05 + 0.01) = 9.99 -> 9
    out = RISK.evaluate(entry("10.00", "9.00"), ctx("1005", "100000", settings=ecn))
    assert isinstance(out, SizedOrder) and out.qty == 9
    no_route = RuntimeSettings(fees_direct_route=False, fees_ecn_per_share=Decimal("0.01"))
    out = RISK.evaluate(entry("10.00", "9.00"), ctx("1005", "100000", settings=no_route))
    assert isinstance(out, SizedOrder) and out.qty == 10  # no ECN fee without direct routing


def test_a_commission_above_the_cap_rejects_as_position_cap() -> None:
    costs = RuntimeSettings(fees_commission=Decimal("80"))
    out = RISK.evaluate(entry("1.00", "0.50"), ctx(settings=costs))
    assert isinstance(out, Rejection) and out.check == "position_cap"


def test_a_cap_of_one_keeps_the_old_sizing() -> None:
    whole = RuntimeSettings(max_position_pct=Decimal("1"))
    out = RISK.evaluate(entry("20.01", "19.91"), ctx(settings=whole))
    assert isinstance(out, SizedOrder) and out.qty == 35 and out.sizing["limited_by"] == "cash"


def test_zero_cash_shares_is_still_zero_shares_when_the_cap_fits() -> None:
    out = RISK.evaluate(entry("20.01", "19.91"), ctx("720", "15"))
    assert isinstance(out, Rejection) and out.check == "zero_shares"


def test_sizing_is_decimal_exact_and_strings_only() -> None:
    # in floats 0.3 / 0.1 is 2.9999999999999996 (floor 2); in Decimal the cap buys exactly 3
    s = RuntimeSettings(max_position_pct=Decimal("0.03"), slippage_buffer=Decimal("0"))
    out = RISK.evaluate(entry("0.1", "0.05"), ctx("10", "1000", settings=s))
    assert isinstance(out, SizedOrder) and out.sizing["shares_cap"] == "3" and out.qty == 3
    assert all(isinstance(v, str | bool) for v in out.sizing.values())
    assert Decimal(out.sizing["cap_dollars"]) == Decimal("0.3")
