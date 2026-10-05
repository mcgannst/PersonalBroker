# Wheel Strategy Rules Spec

Version 1.0, 2026-10-05. Source: "Wheel Strategy Playbook: Screening Criteria and Decision Tree".

This file defines the screening tests and the buy and sell rules for a wheel-strategy application. It is written for the engineer (Claude Code) who will implement the logic. Every threshold lives in the config block in section 2. Do not hard-code numbers anywhere else.

Lines marked **ASSUMPTION** are values the playbook does not state. They were added so the logic is complete. Keep them configurable and list them in the app's settings screen.

## 1. Scope

In scope:

- Score a stock or ETF against ten tests and return a verdict.
- Select the expiry and strike for a new cash-secured put.
- Evaluate each open position once per trading day and return one recommended action.
- Select and manage covered calls after assignment.
- Enforce stop rules and raise alerts.
- Track results per position and per account.

Out of scope unless the owner asks:

- Placing, changing or cancelling orders. The app recommends. The user acts.
- Tax calculations.

Terms:

| Term | Meaning |
| --- | --- |
| Put | Contract that obliges the seller to buy 100 shares at the strike if assigned |
| Call | Contract that obliges the seller to sell 100 shares at the strike if assigned |
| DTE | Calendar days from today to the option's expiry date |
| Delta | Option sensitivity to the stock price. Stored signed: puts negative, calls positive. Rules below use the absolute value |
| IV | Implied volatility of the specific option contract, annualized, as a decimal (0.35 = 35%) |
| OI | Open interest: contracts outstanding at that strike and expiry |
| Premium | Price received per share when the option was sold |
| RoR | Return on risk: option bid divided by strike |
| Net cost | Assignment strike minus all put premium collected per share on this position |

## 2. Config

```yaml
screen:
  market_cap_pass_usd: 10_000_000_000
  market_cap_caution_usd: 2_000_000_000
  debt_to_equity_limit: 1.0
  debt_to_equity_limit_relaxed: 2.0          # utilities, telecom, pipelines
  relaxed_sectors: [utilities, telecom, pipelines]
  spread_pct_of_bid_pass: 0.10
  spread_pct_of_bid_fail: 0.15               # ASSUMPTION: caution band is 10% to 15%
  open_interest_pass: 500
  open_interest_fail_below: 100              # ASSUMPTION: caution band is 100 to 499
  iv_pass_min: 0.20
  iv_pass_max: 0.50
  iv_fail_above: 0.60
  ror_pass: 0.025                            # per cycle, at the delta 0.30 put
  ror_fail_below: 0.005
  ror_reference_delta: 0.30
  ror_scale_to_days: 45                      # used only when DTE is outside 30 to 45
  rsi_caution_at: 70
  sma_period: 50
  sma_slope_lookback_sessions: 20            # ASSUMPTION
  sma_flat_tolerance: 0.01                   # ASSUMPTION: within 1% counts as flat
  new_low_lookback_sessions: 20              # ASSUMPTION

entry:
  dte_min: 30
  dte_max: 45
  monthly_expiries_only: true
  delta_min: 0.20
  delta_max: 0.30
  delta_conservative: 0.20
  iv_conservative_above: 0.40
  contracts_per_ticker: 1
  per_ticker_limit_pct_of_wheel_cash: null   # owner must set. null = warn, do not block
  allow_margin: false

manage:
  profit_target_pct_of_premium: 0.50
  time_exit_dte: 21
  max_rolls_per_put: 1
  pin_risk_band_pct: 0.01
  itm_at_time_exit_preference: ASSIGN        # ASSIGN or ROLL_ONCE

covered_call:
  dte_min: 30
  dte_max: 45
  delta_min: 0.20
  delta_max: 0.30
  low_delta_min: 0.10
  low_delta_max: 0.15
  min_ror: 0.01                              # for a standard call
  low_delta_min_ror: 0.005                   # ASSUMPTION: "worthwhile" floor for a low-delta call
  strike_floor: NET_COST                     # conventional rule. Never below net cost
  include_call_premium_in_net_cost: false    # ASSUMPTION: keeps the strike floor conservative

stops:
  review_drawdown_from_net_cost: 0.25
  fresh_cash_retest_days: 30
  benchmark_ticker: null                     # owner must set: a broad index fund
  benchmark_review: QUARTERLY
  flat_market_quarter_return_max: 0.02       # ASSUMPTION: benchmark quarter return at or below 2% counts as flat or falling
```

## 3. Data inputs

### 3.1 Per underlying

| Field | Type | Used by |
| --- | --- | --- |
| `ticker` | string | all |
| `security_type` | STOCK, BROAD_INDEX_ETF, SECTOR_ETF, LEVERAGED_OR_INVERSE_ETF | disqualifiers, tests 2 and 3 |
| `sector` | string | test 3 |
| `price` | decimal, USD | entry, trend, management |
| `eps_ttm` | decimal | test 2 |
| `eps_growth_yoy` | decimal | test 2 |
| `debt_to_equity` | decimal | test 3 |
| `book_value_per_share` | decimal | test 3, disqualifiers |
| `market_cap_usd` | decimal | test 8 |
| `sma50`, `sma50_prior` | decimal. `sma50_prior` is the value `sma_slope_lookback_sessions` ago | test 9 |
| `low_52w`, `sessions_since_52w_low` | decimal, int | test 9 |
| `rsi14` | decimal | test 9 |
| `next_earnings_date` | date or null | test 6, management |
| `next_ex_dividend_date` | date or null | covered call alert |
| `dividend_yield`, `payout_ratio` | decimal or null | informational flag |
| `short_float` | decimal or null | informational flag |
| `quote_time`, `market_open` | timestamp, bool | data quality |

### 3.2 Per option contract

`type` (PUT, CALL), `expiry`, `strike`, `bid`, `ask`, `delta`, `iv`, `open_interest`, `is_monthly` (third-Friday expiry).

### 3.3 Owner inputs, stored per ticker

| Field | Type | Notes |
| --- | --- | --- |
| `would_own` | true, false, unset | Test 1 |
| `ownership_reason` | string | One sentence. Required when `would_own` is true |
| `thesis_broken` | bool, default false | Owner sets this for events data feeds miss: dividend cut, accounting problem |

### 3.4 Account

`cash_usd`, `wheel_cash_usd` (cash allocated to this strategy), `open_put_collateral_usd`, `new_positions_paused` (bool).

### 3.5 Data quality rules

- If `market_open` is false, mark every option quote `STALE` and show "verify live" beside liquidity and premium results. Do not block.
- If a required field is null, the test that needs it returns `CAUTION` with reason `MISSING_DATA`. It never returns `PASS`.

## 4. Screening

Each test returns `PASS`, `CAUTION` or `FAIL` plus a one-line reason. Tests 4, 5, 6, 7 and 10 need an expiry and a reference strike. Run section 5.1 and 5.2 first to get them.

### 4.1 Automatic disqualifiers

Return verdict `NOT_A_CANDIDATE` immediately when any is true:

- `book_value_per_share <= 0`
- `security_type == LEVERAGED_OR_INVERSE_ETF`
- `security_type == STOCK` and `eps_ttm <= 0`

### 4.2 Hard tests

| # | Test | PASS | CAUTION | FAIL |
| --- | --- | --- | --- | --- |
| 1 | Ownership | `would_own == true` and `ownership_reason` is not empty | `would_own` is unset. Ask the owner | `would_own == false` |
| 2 | Profitability | `eps_ttm > 0` and `eps_growth_yoy >= 0` | `eps_ttm > 0` and `eps_growth_yoy < 0` | `eps_ttm <= 0` |
| 3 | Balance sheet | `book_value_per_share > 0` and `debt_to_equity < limit` | `debt_to_equity >= limit` and test 2 is PASS | `book_value_per_share <= 0`, or `debt_to_equity >= limit` and test 2 is not PASS |
| 4 | Liquidity | spread pct <= `spread_pct_of_bid_pass` and OI >= `open_interest_pass` | anything between PASS and FAIL | `bid == 0`, or spread pct > `spread_pct_of_bid_fail`, or OI < `open_interest_fail_below` |
| 5 | Volatility | `iv_pass_min <= iv <= iv_pass_max` | `iv < iv_pass_min`, or `iv_pass_max < iv <= iv_fail_above` | `iv > iv_fail_above` |
| 6 | Earnings | `next_earnings_date > expiry` | `next_earnings_date` is null | `today <= next_earnings_date <= expiry` |
| 7 | Cash | collateral fits (see below) | fits with nothing left over: remaining cash under 5% of `wheel_cash_usd` (**ASSUMPTION**) | does not fit |

Notes:

- Test 3 `limit` is `debt_to_equity_limit_relaxed` when `sector` is in `relaxed_sectors`, else `debt_to_equity_limit`.
- Tests 2 and 3 return `PASS` with reason `NOT_APPLICABLE` when `security_type` is `BROAD_INDEX_ETF` or `SECTOR_ETF`.
- Tests 4 and 5 read the reference strike from section 5.2. Spread pct = `(ask - bid) / bid`.
- Test 7: `collateral = strike * 100 * contracts`. It fits when `collateral + open_put_collateral_usd <= cash_usd`, and, if `per_ticker_limit_pct_of_wheel_cash` is set, `collateral <= limit * wheel_cash_usd`. Margin is never counted as cash.
- **ASSUMPTION:** test 5 treats IV below 20% as CAUTION, not FAIL. The playbook gives only the pass range and the fail level.

### 4.3 Soft tests

| # | Test | PASS | CAUTION | FAIL |
| --- | --- | --- | --- | --- |
| 8 | Size | `market_cap_usd >= market_cap_pass_usd` | between the two limits | `market_cap_usd < market_cap_caution_usd` |
| 9 | Trend | `price >= sma50`, `sma50 >= sma50_prior * (1 - sma_flat_tolerance)`, no 52-week low within `new_low_lookback_sessions`, and `rsi14 < rsi_caution_at` | `rsi14 >= rsi_caution_at` (reason: wait for cooling), or any mixed case | `price < sma50` and `sma50 < sma50_prior * (1 - sma_flat_tolerance)`, or a 52-week low within the lookback |
| 10 | Premium | `ror >= ror_pass` | `ror_fail_below <= ror < ror_pass` | `ror < ror_fail_below` |

Test 10: `ror = bid / strike` at the put whose absolute delta is closest to `ror_reference_delta` in the chosen expiry. If DTE is outside 30 to 45, use `ror * ror_scale_to_days / dte`.

Test 8 returns `PASS` with reason `NOT_APPLICABLE` for ETFs.

### 4.4 Verdict

```
if any automatic disqualifier:            NOT_A_CANDIDATE
elif any hard test == FAIL:               NOT_A_CANDIDATE
elif any soft test == FAIL:               NOT_NOW
elif any hard test == CAUTION:            NEEDS_REVIEW
elif any soft test == CAUTION:            QUALIFIED_WITH_CONDITION
else:                                     QUALIFIED
```

- `NEEDS_REVIEW`: show the cautions. The owner must acknowledge each one before entry is allowed.
- `QUALIFIED_WITH_CONDITION`: entry allowed. Apply the conservative delta rule in section 5.2 when the caution is on size or trend.
- `NOT_NOW`: the stock is acceptable but the timing or premium is not. Re-screen later.

### 4.5 Ranking

When several tickers are screened together, sort by verdict, then by count of PASS results. Never sort by premium, RoR or IV.

### 4.6 Informational flags

Shown beside the verdict. They do not change it.

- `dividend_yield > 0` and `payout_ratio > 1.0`: dividend-cut risk.
- `short_float > 0.20`: heavily shorted.
- `iv > 0.40`: premium reflects a higher chance of a large fall.

## 5. Opening a put

Preconditions, all required:

- Verdict is `QUALIFIED`, `QUALIFIED_WITH_CONDITION`, or `NEEDS_REVIEW` with every caution acknowledged.
- No open position of any kind in this ticker (put, shares or call).
- `new_positions_paused == false`.

### 5.1 Expiry

1. List expiries with `dte_min <= dte <= dte_max` and `is_monthly == true`.
2. Drop any expiry on or after `next_earnings_date`.
3. If more than one remains, pick the one closest to 45 DTE.
4. If none remains, return `NO_VALID_EXPIRY`. Test 6 is FAIL.

### 5.2 Strike

1. Reference strike: the put with absolute delta closest to `ror_reference_delta`. Tests 4, 5 and 10 use it.
2. Target delta is `delta_conservative` when test 8 or test 9 is CAUTION, or when reference IV is above `iv_conservative_above`. Otherwise it is `delta_max`.
3. Chosen strike: the put with absolute delta closest to the target, within `delta_min` to `delta_max`.
4. Re-run test 4 (liquidity) on the chosen strike if it differs from the reference strike.
5. Show `breakeven = strike - bid`. The owner confirms this is a price they would pay.

### 5.3 Order guidance

- Limit order at the midpoint of bid and ask.
- If unfilled, lower one tick at a time. Never below the bid.
- When filled, recommend a good-till-cancelled buy-to-close order at `premium * (1 - profit_target_pct_of_premium)`.

### 5.4 Record at entry

`open_date`, `ticker`, `expiry`, `strike`, `contracts`, `premium`, `breakeven`, `delta_at_entry`, `iv_at_entry`, `next_earnings_date`, `ownership_reason`, `usd_cad_rate`.

## 6. Position states

```
NONE -> PUT_OPEN
PUT_OPEN -> NONE            (closed, or expired worthless)
PUT_OPEN -> PUT_OPEN        (rolled once)
PUT_OPEN -> SHARES_HELD     (assigned)
SHARES_HELD -> CALL_OPEN    (call sold)
SHARES_HELD -> NONE         (shares sold)
CALL_OPEN -> SHARES_HELD    (call closed, or expired worthless)
CALL_OPEN -> NONE           (shares called away)
```

## 7. Business check

Used on every open position. It is narrower than the full screen on purpose.

```
business_check_passes =
      thesis_broken == false
  and test_1 != FAIL
  and test_2 != FAIL
  and test_3 != FAIL
  and no automatic disqualifier
```

Liquidity, volatility, earnings and cash are not part of this check. Volatility rises when a stock falls, so using it here would force exits at the worst prices.

## 8. Daily evaluation: PUT_OPEN

Run once per trading day. Return the first action that applies.

| Order | Condition | Action |
| --- | --- | --- |
| 1 | `business_check_passes == false` | `CLOSE_PUT_NOW`. Do not take assignment. Record the loss |
| 2 | put `ask <= premium * (1 - profit_target_pct_of_premium)` | `CLOSE_PUT_PROFIT`. Re-screen before any new put |
| 3 | `next_earnings_date` is now on or before `expiry`, and unrealized P/L > 0 | `CLOSE_PUT_BEFORE_EARNINGS` |
| 4 | `next_earnings_date` is now on or before `expiry`, and unrealized P/L <= 0 | `REVIEW_BEFORE_EARNINGS`. Owner chooses close, hold, or roll, before the report |
| 5 | `dte == 0` and `abs(price - strike) / strike <= pin_risk_band_pct` | `PIN_RISK`. Owner chooses: buy to close, or accept possible assignment |
| 6 | `dte <= time_exit_dte` and `price > strike` | `CLOSE_PUT_TIME`. Sell the next monthly put only if the screen still passes |
| 7 | `dte <= time_exit_dte` and `price <= strike` | See 8.1 |
| 8 | otherwise | `HOLD` |

Unrealized P/L per share = `premium - ask`.

A large paper loss with more than `time_exit_dte` days left and a passing business check is `HOLD`. There is no price-based stop on a put.

### 8.1 In the money at the time exit

```
if itm_at_time_exit_preference == ROLL_ONCE and roll_count < max_rolls_per_put:
    candidate = put in the next monthly expiry with strike <= current strike,
                highest strike first, where
                (candidate.bid - current.ask) > 0
                and candidate.expiry < next_earnings_date (or earnings date is null)
    if candidate exists: return ROLL_PUT(candidate)
return TAKE_ASSIGNMENT        # hold to expiry and accept the shares
```

A roll is always a net credit. A second roll is not allowed.

### 8.2 Events

- Put expires with `price > strike`: state `NONE`. Keep the premium. Re-screen.
- Assignment at expiry or earlier: state `SHARES_HELD`. Go to section 9.

## 9. Assigned shares and covered calls

### 9.1 Net cost

```
net_cost = assignment_strike - sum(put premiums per share on this position, including roll credits)
```

Call premium and dividends are tracked for the full-cycle result. They do not lower `net_cost` unless `include_call_premium_in_net_cost` is true.

### 9.2 Fresh-cash test

Question for the owner: if you held cash instead of these shares, would you open a position in this stock today at today's price? The purchase price is not part of the question.

```
fresh_cash_passes = business_check_passes and owner_confirms_would_open_today
```

Show the current results of all ten tests beside the confirmation prompt. Store the owner's answer with a date.

Run it:

- on the day of assignment,
- before every covered call is sold,
- at least every `fresh_cash_retest_days` while shares are held.

If it fails: action `SELL_SHARES`. Record the loss. State `NONE`.

### 9.3 Selecting a call

Conventional rule: the call strike is never below `net_cost`.

```
expiries = monthly expiries with covered_call.dte_min <= dte <= covered_call.dte_max
           and expiry < next_earnings_date (or earnings date is null)
if none: return SKIP_CALL_THIS_CYCLE (reason: earnings)

standard = calls with strike >= net_cost
           and delta_min <= delta <= delta_max
           and bid / strike >= min_ror
if standard not empty:
    return SELL_CALL(the one with delta closest to delta_max)

low = calls with strike >= net_cost
      and low_delta_min <= delta <= low_delta_max
      and bid / strike >= low_delta_min_ror
if low not empty:
    return SELL_CALL(the one with the highest bid)

return HOLD_UNCOVERED     # no call. Re-run the fresh-cash test on schedule
```

Apply test 4 (liquidity) to the chosen call. A FAIL removes that contract from the list.

### 9.4 Daily evaluation: SHARES_HELD (no call open)

| Order | Condition | Action |
| --- | --- | --- |
| 1 | `business_check_passes == false` | `SELL_SHARES` |
| 2 | fresh-cash test is due | `RUN_FRESH_CASH_TEST` |
| 3 | `price <= net_cost * (1 - review_drawdown_from_net_cost)` and no written review in the last 30 days | `DRAWDOWN_REVIEW`. Owner writes why the stock still passes, or sells |
| 4 | fresh-cash test passed within its window | run 9.3 |

### 9.5 Daily evaluation: CALL_OPEN

| Order | Condition | Action |
| --- | --- | --- |
| 1 | `business_check_passes == false` | `CLOSE_CALL_AND_SELL_SHARES` |
| 2 | call `ask <= premium * (1 - profit_target_pct_of_premium)` | `CLOSE_CALL_PROFIT`. Then fresh-cash test, then 9.3 |
| 3 | call is in the money and `next_ex_dividend_date <= expiry` | `EARLY_ASSIGNMENT_RISK`. Expect the shares to be called the day before the ex-dividend date. Owner accepts or rolls before that date |
| 4 | `dte <= time_exit_dte` and `price > strike` | `HOLD_FOR_CALL_AWAY`. Let the shares be called at the strike |
| 5 | `dte <= time_exit_dte` and `price <= strike` | `CLOSE_OR_EXPIRE_CALL`. Then fresh-cash test, then 9.3 |
| 6 | otherwise | `HOLD` |

Rolling a call up is allowed only when the stock passes the fresh-cash test at the current price, and only for a net credit.

### 9.6 Events

- Shares called away: state `NONE`. Record the full-cycle result (section 11). Re-screen before any new put. Never re-enter the same ticker automatically.
- Call expires worthless: state `SHARES_HELD`.

## 10. Stop rules

These override everything else.

| Trigger | Effect |
| --- | --- |
| `business_check_passes == false` on any open position | Exit that position within one trading day |
| Fresh-cash test fails | `SELL_SHARES` |
| Shares at or below `net_cost * (1 - review_drawdown_from_net_cost)` | `DRAWDOWN_REVIEW` every 30 days while it stays there |
| A new put or share purchase in a ticker that already has an open position | Block |
| `roll_count >= max_rolls_per_put` | Block further rolls |
| `collateral + open_put_collateral_usd > cash_usd` | Block new puts |
| At a quarterly review: benchmark quarter return <= `flat_market_quarter_return_max` and wheel account return < benchmark return | Set `new_positions_paused = true` until the owner clears it. Trailing in a rising market does not trigger this |

## 11. Calculations

```
collateral          = strike * 100 * contracts
breakeven           = strike - premium
cushion             = (price - strike) / price
ror                 = bid / strike
ror_annualized      = ror * 365 / dte
spread_pct          = (ask - bid) / bid
put_pl_per_share    = premium - current_ask
net_cost            = assignment_strike - total_put_premium_per_share
drawdown_from_cost  = (net_cost - price) / net_cost
full_cycle_result   = (total_put_premium + total_call_premium + dividends
                       + sale_price - assignment_strike) * 100 * contracts - fees
account_value       = cash + shares at market price - cost to close all short options at the ask
```

`account_value` includes unrealized losses. Report it as the headline number. Show premium collected as a secondary figure only.

Benchmark: the value of the same deposits and withdrawals, on the same dates, invested in `benchmark_ticker`. Compare at each quarter end.

Store amounts in USD. Store the USD/CAD rate with every transaction so results can be reported in CAD.

## 12. Alerts

| Alert | Fires when |
| --- | --- |
| `STRIKE_TOUCHED` | Stock price reaches the strike of an open put or call |
| `TIME_EXIT_DUE` | An open option reaches `time_exit_dte` |
| `EARNINGS_MOVED` | `next_earnings_date` changes to on or before an open option's expiry |
| `EX_DIVIDEND_AHEAD` | An open call is in the money and the ex-dividend date is on or before expiry |
| `FRESH_CASH_DUE` | `fresh_cash_retest_days` since the last recorded answer |
| `DRAWDOWN_REVIEW_DUE` | Shares at or beyond the review level with no review in 30 days |
| `BENCHMARK_REVIEW_DUE` | Quarter end |
| `THESIS_FLAG` | Test 2 or 3 changes to FAIL on a ticker with an open position |

## 13. Acceptance cases

Use these as unit tests. Config values are the defaults above.

| # | Given | Expect |
| --- | --- | --- |
| 1 | Stock, `eps_ttm = -0.40` | Verdict `NOT_A_CANDIDATE` (automatic disqualifier) |
| 2 | All hard tests PASS, reference put RoR 1.8%, size and trend PASS | Test 10 `CAUTION`, verdict `QUALIFIED_WITH_CONDITION`, target delta 0.30 |
| 3 | All tests PASS except trend, where `rsi14 = 74` | Verdict `QUALIFIED_WITH_CONDITION`, target delta 0.20 |
| 4 | Reference put IV 0.64 | Test 5 `FAIL`, verdict `NOT_A_CANDIDATE` |
| 5 | Earnings in 20 days, only monthly expiry in the window is 38 DTE | `NO_VALID_EXPIRY`, test 6 `FAIL` |
| 6 | Put sold for 1.20, now ask 0.58, 30 DTE, business check passes | `CLOSE_PUT_PROFIT` |
| 7 | Put sold for 1.20, now ask 2.90, 33 DTE, stock below strike, business check passes | `HOLD` |
| 8 | Put at 19 DTE, stock above strike, ask 0.80 on 1.20 premium | `CLOSE_PUT_TIME` |
| 9 | Put at 20 DTE, stock below strike, preference `ASSIGN` | `TAKE_ASSIGNMENT` |
| 10 | Put at 20 DTE, stock below strike, preference `ROLL_ONCE`, `roll_count = 1` | `TAKE_ASSIGNMENT` |
| 11 | Put open, `eps_ttm` turns negative | `CLOSE_PUT_NOW` |
| 12 | Shares held, net cost 47.80, stock 48.50, call at strike 50 with delta 0.27 and bid 0.60 | `SELL_CALL` at 50 |
| 13 | Shares held, net cost 47.80, stock 38.00, no call at or above 47.80 with delta >= 0.10 | `HOLD_UNCOVERED`. Drawdown is 20.5%, so no `DRAWDOWN_REVIEW`. At stock 35.00 (26.8%) the action is `DRAWDOWN_REVIEW` |
| 14 | Shares held, owner answers "no" to the fresh-cash question | `SELL_SHARES` |
| 15 | Call open at 18 DTE, stock above strike | `HOLD_FOR_CALL_AWAY` |
| 16 | Ticker has shares held, owner tries to open a new put on it | Blocked |

## 14. Open decisions for the owner

1. `per_ticker_limit_pct_of_wheel_cash`: no value set yet.
2. `benchmark_ticker`: no value set yet.
3. `itm_at_time_exit_preference`: default is `ASSIGN`. Confirm or change to `ROLL_ONCE`.
4. Manual override: should the owner be able to open a put on a ticker the screen rejects, with a recorded reason? Not included in this version.
5. Order placement: this version only recommends. Confirm whether the app should ever send orders to the broker.
6. The fresh-cash test here uses the business check plus the owner's answer. The playbook text says "all seven hard tests". The narrower version is used because liquidity, earnings timing and volatility should not force a sale of shares already held. Confirm.
7. Every line marked ASSUMPTION in sections 2 and 4.

## 15. Not financial advice

These rules come from published research and practitioner conventions. They are a rule set for software, not personal financial advice.
