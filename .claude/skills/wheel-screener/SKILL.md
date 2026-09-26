---
name: wheel-screener
description: Screen the market with FinViz for new wheel-strategy (cash-secured put) candidates that fit the eight-test wheel framework AND a maximum stock price he provides, then return a short, framework-ranked shortlist ready for the wheel-evaluator. Use this whenever you are ask to find, screen for, scan for, or list wheel candidates, stocks to sell puts on, or "stocks under $X for the wheel", or gives a price cap or cash amount and asks what he could wheel with it — even if he doesn't name FinViz. For judging one specific ticker he already has in mind, use wheel-evaluator instead.
---

# Wheel Screener (FinViz)

Find stocks that pass the screenable parts of the eight-test wheel framework and trade at or below a price the user gives. The output is a shortlist, not a verdict: Tests 1 (ownership), 6 (liquidity) and 8 (premium) cannot be decided by a screener, so qualifiers go on to the **wheel-evaluator** skill for the full report.

## Style rules (mandatory, same as wheel-evaluator)

- Plain language; define a financial term briefly at first use. No analogies or colourful metaphors.
- Facts and framework verdicts, not advice. Never recommend buying or selling.
- State affordability once, factually (max collateral per contract in USD, and CAD if the FX rate is known). No budget commentary.
- Timestamp the data and say where it came from (FinViz screener, FinViz quote page, or FMP fallback).
- **Never rank by premium, yield or dividend.** Rank by the framework (see Step 5).

## Step 1 — Get the inputs

| Input | Required | Default |
|---|---|---|
| Max stock price (USD) | Yes (or cash) | — |
| Cash for one contract | Alternative to max price | max price = cash / 100 |
| Min stock price | No | $5 (avoids penny stocks) |
| Tier | No | `standard` |
| Sectors to include/exclude | No | all |

If you are given cash in CAD, convert to USD at the current rate (search it if unknown) before dividing by 100, and say which rate you used. If no price or cash is given, ask for it — it is the one input this skill cannot assume.

Tiers:
- `strict` — only names that should PASS every screenable test (adds SMA50 above SMA200, RSI < 60, volume > 1M).
- `standard` — the framework's PASS thresholds that FinViz can express.
- `wide` — also lets in CAUTION-tier size ($2–10B) and drops the Debt/Eq filter so utilities, telecom and pipelines (allowed up to 2) are not excluded. Use when `standard` returns fewer than ~5 names, or when the user asks for more.

## Step 2 — Build the FinViz URLs

Run the bundled script (path relative to this skill's folder):

```bash
python scripts/build_finviz_url.py --max-price <MAX> [--min-price <MIN>] [--tier strict|standard|wide]
# or: --cash-usd <CASH>
```

It prints the filters and four URLs (Overview, Valuation, Financial, Technical views) that share the same filters, sorted by market cap. FinViz only has preset price buckets, so the script picks the tightest one that contains the requested window and tells you when to post-filter the exact price. If Python isn't available, build the URL by hand from `references/finviz-filters.md`.

What the filters cover:

| Test | Screener filter | Still needs checking in Step 4 |
|---|---|---|
| — | Optionable, stocks only (no ETFs, so no leveraged/inverse ETFs), USA | — |
| 2 Profitability | P/E > 0 (so EPS ttm > 0) | Shrinking EPS, one-time distortions (forward P/E) |
| 3 Balance sheet | Debt/Eq < 1 (standard/strict) | **Book value positive** — negative equity slips through as a negative Debt/Eq |
| 4 Size | Market cap > $10B (> $2B in wide) | — |
| 5 Trend | Price above 50-day average | RSI < 70, not near 52-week low, 50-day line not falling |
| 6 Liquidity | Average volume > 500K (pre-screen only) | Real option chain in wheel-evaluator |
| 7 Affordability | Price preset | Exact price ≤ max |

## Step 3 — Fetch the results

1. Fetch the **Technical** URL and the **Financial** URL (and Valuation if forward P/E is needed). FinViz shows 20 rows per page; add `&r=21`, `&r=41`, … for more pages. Stop at ~60 names — if there are more, tell the user the count and suggest `strict` or a lower max price rather than reading hundreds.
2. **If FinViz can't be fetched** (blocked, 403, empty table): say so in one line, give the user the Overview URL to open himself, and continue with the FMP fallback if the FMP connector is available:
   - `mcp__FMP__search` with `endpoint: "search-company-screener"`, `priceMoreThan`/`priceLowerThan` = the window, `marketCapMoreThan: 10000000000` (2000000000 for wide), `isEtf: false`, `isFund: false`, `country: "US"`, `volumeMoreThan: 500000`, `isActivelyTrading: true`, `limit: 100`.
   - Then pull ratios (Debt/Eq, book value, EPS) and technicals (SMA 50/200, RSI 14) from the FMP statements and technicalIndicators tools for each name. FMP does not say whether a stock is optionable; mark that "verify in Questrade" and let the wheel-evaluator's chain check settle it.
   - If the user pastes or screenshots the FinViz results instead, use those.

## Step 4 — Post-filter and score

For every row, apply these checks from the screener columns, fetching the quote page (`https://finviz.com/quote.ashx?t=<TICKER>`) only when a needed field is missing:

| Check | Rule | Result |
|---|---|---|
| Price | Outside the user’s window | Drop |
| Book value | Book/sh ≤ 0, or Debt/Eq negative | **Drop — automatic disqualifier** |
| Profitability | EPS growth this year negative | CAUTION (note forward P/E if trailing P/E looks extreme) |
| Balance sheet | Debt/Eq 1–2 outside utilities/telecom/pipelines; > 2 anywhere | CAUTION / Drop |
| Size | $2–10B | CAUTION |
| Trend | RSI ≥ 70 | CAUTION — "timing: wait for cooling" |
| Trend | Within ~5% of 52-week low, or SMA50 below SMA200 and falling | Drop (established downtrend) |
| Flags | Short float > 20% | FLAG "battleground" |
| Flags | Dividend payout ratio > 100% | FLAG "dividend-cut risk" |
| Flags | Earnings date within the next ~45 days | FLAG "earnings inside a 30–45 day put" |

Also drop anything that is plainly a leveraged/inverse product or a pre-profit company if one slipped through.

## Step 5 — Rank and report

Rank survivors by: fewest CAUTIONs → no flags → larger market cap. Never by yield or premium. Show the top 10 (or all if fewer), and the count dropped per reason.

```
WHEEL SCREEN — stocks $<min>–$<max> — tier <tier> — <timestamp>, source <FinViz|FMP>
Max collateral per contract: $<max×100> USD (~$<CAD> CAD)
Screener: <N> matched → <M> after post-filter (dropped: <k> price, <k> book value, <k> downtrend, ...)

#  Ticker  Company         Price   MktCap  EPS gr  D/E   RSI  vs SMA50  Status            Flags
1  XXX     ...             $xx.xx  $xxB    +x%     0.xx  xx   +x%       PASS (screenable) —
2  YYY     ...             $xx.xx  $xB     -x%     1.xx  xx   +x%       2 CAUTION         earnings 10/28
...

Not tested here: 1 Ownership (your call), 6 Liquidity and 8 Premium (need the option chain).
Next step: run wheel-evaluator on any of these for strikes, liquidity and premium.
FinViz screener URL: <overview URL>
Framework screen, not advice.
```

Then offer, in one line, to run the **wheel-evaluator** on the top 3 (or the ones the user picks). If he agrees, follow that skill for each ticker and use its multi-ticker comparison format.

## What this skill must NOT do

- Never place, preview or suggest orders.
- Never rank or sort by dividend yield, premium or "highest return".
- Never mark a name "Qualified" — only the wheel-evaluator gives the overall verdict, because Tests 1, 6 and 8 are not checked here.
- Never infer option liquidity from market cap or share volume.
- Never skip the book-value check; the Debt/Eq filter does not catch negative equity.
