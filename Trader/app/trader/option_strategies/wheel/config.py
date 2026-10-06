"""The wheel's parameters: one field per key of the wheel rules spec (WS) §2, named `<section>_<key>` with
the sections `screen`, `entry`, `manage`, `cc` (covered call) and `stops`. No rule module holds a number of
its own. A description starting `ASSUMPTION:` marks a value the playbook does not state (WS §14 item 7)."""

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ItmPreference = Literal["ASSIGN", "ROLL_ONCE"]
SESSION_OFFSET_PATTERN = r"^(open|close)[+-](\d+m)?(\d+s)?$"


def _d(text: str) -> Decimal:
    return Decimal(text)


class WheelParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- screen (WS §4) ---
    screen_market_cap_pass_usd: Decimal = Field(
        _d("10000000000"), gt=0, description="Test 8: market cap at or above this passes."
    )
    screen_market_cap_caution_usd: Decimal = Field(
        _d("2000000000"), gt=0, description="Test 8: market cap below this fails."
    )
    screen_debt_to_equity_limit: Decimal = Field(
        _d("1.0"), gt=0, description="Test 3: debt to equity must be below this."
    )
    screen_debt_to_equity_limit_relaxed: Decimal = Field(
        _d("2.0"), gt=0, description="Test 3: the limit for the relaxed sectors."
    )
    screen_relaxed_sectors: tuple[str, ...] = Field(
        ("utilities", "telecom", "pipelines"),
        description="Test 3: a sector whose name contains one of these words uses the relaxed limit.",
    )
    screen_spread_pct_of_bid_pass: Decimal = Field(
        _d("0.10"), ge=0, description="Test 4: (ask - bid) / bid at or below this passes."
    )
    screen_spread_pct_of_bid_fail: Decimal = Field(
        _d("0.15"),
        ge=0,
        description="ASSUMPTION: test 4 fails above this spread; the caution band is 10% to 15%.",
    )
    screen_open_interest_pass: int = Field(
        500, ge=0, description="Test 4: open interest at or above this passes."
    )
    screen_open_interest_fail_below: int = Field(
        100, ge=0, description="ASSUMPTION: test 4 fails below this open interest; caution is 100 to 499."
    )
    screen_iv_pass_min: Decimal = Field(
        _d("0.20"),
        ge=0,
        description="ASSUMPTION: IV below this is CAUTION, not FAIL (the playbook gives only the pass range "
        "and the fail level). Test 5 passes from this IV up.",
    )
    screen_iv_pass_max: Decimal = Field(_d("0.50"), ge=0, description="Test 5 passes up to this IV.")
    screen_iv_fail_above: Decimal = Field(_d("0.60"), ge=0, description="Test 5 fails above this IV.")
    screen_ror_pass: Decimal = Field(
        _d("0.025"), ge=0, description="Test 10: bid / strike per cycle at the reference put, to pass."
    )
    screen_ror_fail_below: Decimal = Field(
        _d("0.005"), ge=0, description="Test 10 fails below this return on risk."
    )
    screen_ror_reference_delta: Decimal = Field(
        _d("0.30"),
        gt=0,
        lt=1,
        description="The reference put is the one with absolute delta closest to this.",
    )
    screen_ror_scale_to_days: int = Field(
        45, gt=0, description="Test 10: outside the DTE band the return is scaled to this many days."
    )
    screen_ror_dte_min: int = Field(30, ge=0, description="Test 10: lowest DTE with no scaling.")
    screen_ror_dte_max: int = Field(45, ge=0, description="Test 10: highest DTE with no scaling.")
    screen_rsi_caution_at: Decimal = Field(
        _d("70"), ge=0, le=100, description="Test 9: RSI(14) at or above this is CAUTION (wait for cooling)."
    )
    screen_sma_period: int = Field(
        50, gt=0, description="Test 9: sessions in the moving average (used when facts are gathered)."
    )
    screen_sma_slope_lookback_sessions: int = Field(
        20,
        gt=0,
        description="ASSUMPTION: the moving average is compared with its value this many sessions ago "
        "(used when facts are gathered).",
    )
    screen_sma_flat_tolerance: Decimal = Field(
        _d("0.01"),
        ge=0,
        lt=1,
        description="ASSUMPTION: a moving average within 1% of its prior value is flat.",
    )
    screen_new_low_lookback_sessions: int = Field(
        20, ge=0, description="ASSUMPTION: a 52-week low within this many sessions fails test 9."
    )
    screen_cash_caution_remaining_pct: Decimal = Field(
        _d("0.05"),
        ge=0,
        le=1,
        description="ASSUMPTION: test 7 is CAUTION when the cash left over is under this share of the wheel "
        "cash.",
    )
    screen_flag_payout_ratio_above: Decimal = Field(
        _d("1.0"),
        ge=0,
        description="Flag dividend-cut risk when a dividend payer's payout ratio is above this.",
    )
    screen_flag_short_float_above: Decimal = Field(
        _d("0.20"), ge=0, description="Flag heavily shorted when the short float is above this."
    )
    screen_flag_iv_above: Decimal = Field(
        _d("0.40"), ge=0, description="Flag high IV when the reference put's IV is above this."
    )

    # --- entry (WS §5) ---
    entry_dte_min: int = Field(30, ge=0, description="Shortest expiry for a new put, in calendar days.")
    entry_dte_max: int = Field(45, ge=0, description="Longest expiry for a new put, in calendar days.")
    entry_target_dte: int = Field(45, ge=0, description="Of several valid expiries, the one closest to this.")
    entry_monthly_expiries_only: bool = Field(True, description="Use only third-Friday expiries.")
    entry_delta_min: Decimal = Field(
        _d("0.20"), gt=0, lt=1, description="Lowest absolute delta of a new put."
    )
    entry_delta_max: Decimal = Field(
        _d("0.30"), gt=0, lt=1, description="Highest absolute delta of a new put; the normal target."
    )
    entry_delta_conservative: Decimal = Field(
        _d("0.20"), gt=0, lt=1, description="Target delta when size or trend is CAUTION, or IV is high."
    )
    entry_iv_conservative_above: Decimal = Field(
        _d("0.40"), ge=0, description="Reference IV above this selects the conservative delta."
    )
    entry_contracts_per_ticker: int = Field(1, ge=1, description="Contracts per ticker.")
    entry_per_ticker_limit_pct_of_wheel_cash: Decimal | None = Field(
        _d("0.50"),
        gt=0,
        le=1,
        description="One ticker's collateral may use at most this share of the wheel cash. None = warn only.",
    )
    entry_allow_margin: bool = Field(False, description="Margin is never counted as cash.")

    # --- manage (WS §8) ---
    manage_profit_target_pct_of_premium: Decimal = Field(
        _d("0.50"), gt=0, lt=1, description="Close once this share of the premium is earned."
    )
    manage_time_exit_dte: int = Field(21, ge=0, description="The time exit: at or under this many days left.")
    manage_max_rolls_per_put: int = Field(1, ge=0, description="Rolls allowed per put.")
    manage_pin_risk_band_pct: Decimal = Field(
        _d("0.01"), ge=0, description="On expiry day, a price within this share of the strike is pin risk."
    )
    manage_itm_at_time_exit_preference: ItmPreference = Field(
        "ASSIGN", description="In the money at the time exit: take assignment, or roll once."
    )

    # --- covered call (WS §9) ---
    cc_dte_min: int = Field(30, ge=0, description="Shortest expiry for a covered call.")
    cc_dte_max: int = Field(45, ge=0, description="Longest expiry for a covered call.")
    cc_delta_min: Decimal = Field(_d("0.20"), gt=0, lt=1, description="Lowest delta of a standard call.")
    cc_delta_max: Decimal = Field(
        _d("0.30"), gt=0, lt=1, description="Highest delta of a standard call; the target."
    )
    cc_low_delta_min: Decimal = Field(_d("0.10"), gt=0, lt=1, description="Lowest delta of a low-delta call.")
    cc_low_delta_max: Decimal = Field(
        _d("0.15"), gt=0, lt=1, description="Highest delta of a low-delta call."
    )
    cc_min_ror: Decimal = Field(_d("0.01"), ge=0, description="Lowest bid / strike of a standard call.")
    cc_low_delta_min_ror: Decimal = Field(
        _d("0.005"),
        ge=0,
        description="ASSUMPTION: the 'worthwhile' floor of bid / strike for a low-delta call.",
    )
    cc_strike_floor: Literal["NET_COST"] = Field(
        "NET_COST", description="The call strike is never below the net cost."
    )
    cc_include_call_premium_in_net_cost: bool = Field(
        False, description="ASSUMPTION: call premium does not lower the net cost (a conservative floor)."
    )

    # --- stops (WS §10) ---
    stops_review_drawdown_from_net_cost: Decimal = Field(
        _d("0.25"), gt=0, lt=1, description="Shares this far under the net cost need a written review."
    )
    stops_drawdown_review_days: int = Field(30, gt=0, description="A drawdown review lasts this many days.")
    stops_fresh_cash_retest_days: int = Field(
        30, gt=0, description="The fresh-cash test is repeated at least this often while shares are held."
    )
    stops_benchmark_ticker: str | None = Field(
        None, description="The benchmark. None = the setting options.benchmark_ticker."
    )
    stops_benchmark_review: Literal["QUARTERLY"] = Field(
        "QUARTERLY", description="When the benchmark is reviewed."
    )
    stops_flat_market_quarter_return_max: Decimal = Field(
        _d("0.02"),
        description="ASSUMPTION: a benchmark quarter return at or below 2% counts as flat or falling.",
    )

    # --- the plug-in (not in WS §2) ---
    daily_event_offset: str = Field(
        "open+60m", pattern=SESSION_OFFSET_PATTERN, description="When the daily evaluation runs."
    )
    walk_orders: bool = Field(True, description="Start limit orders at the midpoint and walk them (WS §5.3).")
    market_screen_filters: tuple[str, ...] = Field(
        (
            "sh_opt_option",
            "cap_largeover",
            "fa_pe_profitable",
            "fa_debteq_u1",
            "ta_sma50_pa",
            "ta_rsi_nob60",
            "sh_avgvol_o1000",
        ),
        description="FinViz filter codes of the market-wide candidate screen.",
    )
    market_screen_max_candidates: int = Field(
        10, ge=0, le=50, description="Most new candidates one market screen may add."
    )

    @field_validator("daily_event_offset")
    @classmethod
    def _offset_has_an_amount(cls, value: str) -> str:
        if not re.search(r"\d", value):
            raise ValueError("not a session offset (e.g. 'open+60m')")
        return value

    @field_validator("screen_relaxed_sectors")
    @classmethod
    def _lower_sectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(s.strip().lower() for s in value if s.strip())
