---
name: wheel-evaluator
description: Evaluate a stock as a wheel-strategy candidate (cash-secured puts) using Stephen's eight-test framework, live Questrade data, and web research, then produce a short pass/fail report. Use this whenever Stephen asks to evaluate, check, screen, or "run the tests" on a stock or ticker for the wheel, asks "can I wheel X", "how does X look for selling a put", "is X a good wheel candidate", asks to compare wheel candidates, or shares a ticker/screenshot in a wheel or cash-secured-put context — even if he doesn't say the word "wheel" but the context is put selling or option income.
---

# Wheel Candidate Evaluator

Evaluate one or more stocks against the eight-test wheel framework and deliver a short, plain-language report of what passes, what fails, and why. The full framework reference is Stephen's "Beginner's Guide to Trading Options Using the Wheel Strategy" (in his project files, if present).

## Style rules (mandatory)

- **Plain language.** Define any financial term briefly at first use. No analogies or colourful metaphors.
- **Facts and framework verdicts, not advice.** Report what passes/fails and why. The decision is Stephen's. Never recommend buying or selling.
- **State affordability exactly once, factually** (collateral required in USD and CAD). Never repeat cost warnings or tell him something is too expensive — he tracks his own budget.
- **Timestamp the data.** Note the quote time. If the market is closed, note that volume reads zero and deep in-the-money quotes may be stale; judge liquidity by open interest and out-of-the-money spreads, and flag numbers as "verify live."
- Keep the whole report under ~40 lines unless he asks for depth.

## Data-gathering workflow

Run these in order; parallelize where possible.

### 1. Fundamentals (web search)
Search the ticker's Finviz page or equivalent (`<TICKER> stock finviz EPS debt market cap short float`) and capture: EPS (ttm) and EPS growth (this year, Q/Q) · P/E and forward P/E · Debt/Eq and book value per share · market cap · short float · beta · RSI(14) · 52-week high/low · performance (month/quarter/year) and price vs 50-day/200-day averages · dividend yield and payout ratio (if any) · **next earnings date**. If the trailing P/E looks extreme, note the forward P/E as the possible explanation (writedown distortion).

### 2. Price and options (Questrade connector)
1. `search_symbols` → the underlying's securityUuid (confirm the exchange is US).
2. `get_quotes` on the underlying → live price and day data.
3. `get_option_expiries` → choose the **monthly expiry 30–45 days out** (third Friday). If Stephen named an expiry, use his.
4. `get_option_chain` with `optionType: "Put"`, that expiry, `strikeRange` roughly 75–100% of the stock price, `includeGreeks: true` → identify the strikes with **delta between −0.25 and −0.35** (the framework's standard zone) and one further strike (~−0.15) as the conservative alternative.
5. `get_quotes` on those contracts' securityUuids → bid, ask, last, volume. Compute the spread. If open interest isn't in the quote, say "OI: verify in platform" rather than guessing.
6. Compute per candidate strike: premium (bid × 100) · collateral (strike × 100, USD and CAD at current FX — search the rate if unknown) · breakeven (strike − bid) · cushion ((price − strike)/price) · **RoR** (return on risk = bid ÷ strike, i.e. premium ÷ collateral, per cycle) · annualized (RoR × 365/days). If the expiry is outside 30–45 days, also show RoR scaled to 45 days (RoR × 45/days) and judge Test 8 on that scaled figure.

### 3. Calendar (web search if not already known)
Identify scheduled events inside the expiry window: the ticker's earnings date, US CPI releases, Fed (FOMC) meetings. Flag any that land inside the trade.

## The eight tests

Score each **PASS / CAUTION / FAIL** with a one-line reason.

| # | Test | PASS | CAUTION | FAIL |
|---|------|------|---------|------|
| 1 | Ownership | Stephen has said (now or previously) he'd own it; or it's an established business he can plainly articulate | Unfamiliar business — pose the question to him | He's said no, or the business is unanswerable (e.g. pure leverage products) |
| 2 | Profitability | EPS (ttm) positive; growth not collapsing | Positive but shrinking, or distorted by one-time items (check forward P/E) | EPS negative |
| 3 | Balance sheet | Debt/Eq < 1 (< 2 for utilities/telecom/pipelines); book value positive | Debt/Eq 1–2 outside those sectors | Negative book value (automatic overall fail), or heavy debt on weak earnings |
| 4 | Size | Market cap ≥ $10B | $2–10B | < $2B |
| 5 | Trend | Price at/above 50-day average with the line flat or rising; no new 52-week lows; RSI < 70 | RSI ≥ 70 or a vertical run (fails on *timing* — note "wait for cooling") | Established downtrend / making new lows / recent parabolic collapse |
| 6 | Liquidity | Spread a few cents at candidate strikes; OI hundreds-to-thousands | Spread ~5–15% of premium, or modest OI — "verify live" | Spread a large fraction of premium; near-zero OI; strikes $2.50+ apart on a cheap stock |
| 7 | Affordability | Collateral fits the cash Stephen states (or his known sleeve) | Fits with nothing left over | Exceeds stated cash — state the number once, no commentary |
| 8 | Premium (RoR) | RoR ≥ 2.5% per cycle at the ~0.30-delta strike | RoR 0.5–2.5% per cycle | RoR < 0.5%, or premium negligible at any sensible strike |

**Automatic disqualifiers regardless of other scores:** negative book value (AMC pattern) · leveraged/inverse ETFs (decay makes assignment structurally unrecoverable — TQQQ pattern) · pre-profit companies (fails Test 2; if Stephen would own it anyway, note that a put works only as a one-time acquisition tactic, not a wheel).

**Context patterns to apply** (from Stephen's casebook): high yield + payout ratio > 100% = dividend-cut risk (PFE/KHC/WEN pattern) · short float > 20% = battleground, needs the short case answered (WEN) · IV above ~60% = the premium is hazard pay, say what it's paying for (NOK/CCL) · "optionable ≠ tradeable" — Test 6 is decided only by the actual chain (BN/BTE pattern) · low beta does not mean low risk when problems are company-specific (WEN/BTE).

## Report format

```
WHEEL EVALUATION: <TICKER> — <Company> — $<price> (<timestamp>)
VERDICT: Qualified / Qualified, timing condition / Not a candidate — <one line>

TESTS
1 Ownership      PASS/CAUTION/FAIL — <reason, one line>
2 Profitability  ...
3 Balance sheet  ...
4 Size           ...
5 Trend          ...
6 Liquidity      ...
7 Affordability  ...
8 Premium        ...

CANDIDATE STRIKES (<expiry>, <n> days)
$XX put — delta −0.XX · bid $X.XX → $XX premium · collateral $X,XXX USD (~$X,XXX CAD)
          breakeven $XX.XX · cushion X% · RoR X.X%/cycle ≈ XX% annualized
$XX put — (conservative alternative, same fields)

CALENDAR: earnings <date, inside/outside window> · CPI <date> · Fed <dates>
FLAGS: <anything from the context patterns; omit if none>
Data as of <time>; verify live quotes before trading. Framework verdicts, not advice.
```

If comparing multiple tickers, produce one compact table (tests as columns, PASS/CAUTION/FAIL cells) plus the strike table for each qualifier, and rank by the framework — never by premium alone.

## What this skill must NOT do

- Never place, modify, preview, or suggest placing an order unless Stephen explicitly asks in a separate request.
- Never rank candidates purely by premium/yield (the yield-sorted-screener trap).
- Never skip Test 6 or infer liquidity from company size alone.
- Never editorialize about position size or budget beyond the single Test 7 line.
