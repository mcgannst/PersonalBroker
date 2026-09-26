# FinViz screener filter codes used by this skill

URL shape: `https://finviz.com/screener.ashx?v=<view>&f=<code>,<code>,...&o=<sort>`

Codes were checked against the open-source `finvizfinance` (lit26) and `finviz-screener` (knicola)
libraries, not against finviz.com directly. If FinViz returns an empty or error page, check the codes
in the FinViz UI first. Rows marked (unverified) came from a summary rather than the source code.

## Views
| View | v= | Useful columns |
|---|---|---|
| Overview | 111 | Ticker, Company, Sector, Industry, Market Cap, P/E, Price, Change, Volume |
| Valuation | 121 | P/E, Fwd P/E, P/B, EPS growth |
| Financial | 161 | Dividend, ROE, Debt/Eq, margins, earnings date |
| Technical | 171 | Beta, ATR, SMA20/50/200 %, 52W High/Low %, RSI |

## Filters mapped to the eight tests
| Test | Filter | Code |
|---|---|---|
| Base | Optionable | `sh_opt_option` |
| Base | Stocks only (no ETFs/funds, so no leveraged ETFs) | `ind_stocksonly` |
| Base | USA | `geo_usa` |
| 2 Profitability | P/E profitable (>0), so EPS ttm is positive | `fa_pe_profitable` |
| 2 Profitability | EPS growth this year > 0 (optional) | `fa_epsyoy_pos` |
| 3 Balance sheet | Debt/Eq under 1 | `fa_debteq_u1` (no "under 2" preset exists) |
| 4 Size | Market cap over $10B / over $2B | `cap_largeover` / `cap_midover` |
| 5 Trend | Price above SMA50 | `ta_sma50_pa` |
| 5 Trend | SMA50 above SMA200 | `ta_sma50_sa200` |
| 5 Trend | RSI not overbought (<60) | `ta_rsi_nob60` (no "<70" preset; post-filter RSI instead) |
| 6 Liquidity (pre-screen only) | Avg volume over 500K / 1M | `sh_avgvol_o500` / `sh_avgvol_o1000` (unverified) |
| 7 Affordability | Price presets | see below (unverified) |
| Calendar | Earnings this/next week | `earningsdate_thisweek` / `earningsdate_nextweek` (prefix unverified) |
| Sector | Utilities | `sec_utilities` |

## Price presets (prefix `sh_price_`)
- Under: u1 u2 u3 u4 u5 u7 u10 u15 u20 u30 u40 u50
- Over: o1 o2 o3 o4 o5 o7 o10 o15 o20 o30 o40 o50 o60 o70 o80 o90 o100
- Ranges: 1to5 1to10 1to20 5to10 5to20 5to50 10to20 10to50 20to50 50to100

Custom values (for example `sh_price_5to40`) may work on finviz.com but have not been confirmed, so the
script only uses presets and asks for an exact post-filter.

## Known gaps a screener cannot close
- **Negative book value.** A company with negative equity shows a negative Debt/Eq, which slips through
  `fa_debteq_u1`. There is no positive-only P/B filter. Always check book value per share on the quote
  page (`https://finviz.com/quote.ashx?t=<TICKER>`, "Book/sh").
- **RSI 60 to 70** passes the framework but is cut by `ta_rsi_nob60`, so only the strict tier uses it.
- **Utilities/telecom/pipelines** may carry Debt/Eq up to 2 under the framework; use the wide tier.
- **Liquidity and premium (Tests 6 and 8)** need the real option chain, not screener volume.
