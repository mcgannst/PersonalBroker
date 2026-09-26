# Day Trading Strategy Playbook (v2)

*Prepared for Stephen · September 26, 2026 · US + TSX common stocks · Questrade non-registered account · no leverage · long-only · $1,000 CAD starting capital*

*v2 replaces the v1 report. The claims in v2 were checked against the full text of the primary sources. 24 of the 25 central claims were confirmed by a 3-vote adversarial check, and 1 was refuted and removed. Chart-pattern and candlestick strategies are now covered.*

---

## Bottom line up front

| Strategy family | Evidence grade | Verdict for you |
|---|---|---|
| **5-min Opening Range Breakout on "Stocks in Play"** | **Moderate** | ✅ **Core strategy.** Paper-trade first |
| Last-half-hour (end-of-day) momentum | Moderate for the index · **Weak for single stocks** | 🟡 **Overlay only.** Decides whether to hold ORB winners into the close |
| VWAP trend-following | Weak for single stocks (tested on the QQQ ETF only) | 🟡 Probation, paper only |
| Classical chart patterns (H&S, double top/bottom, triangles, flags, wedges, cup & handle) | **Weak.** Some statistical information, no proven trading profit | ❌ Dropped as stand-alone strategies |
| Candlestick patterns (engulfing, hammer, doji, morning star…) | **None.** Fail after costs and data-snooping correction, both intraday and daily | ❌ Dropped |
| Support/resistance and channel breakouts | **None** after data-snooping correction | ❌ Dropped |
| Gap fade, VWAP mean reversion, small-cap gap-and-go, news scalping | **No rigorous evidence found** | ❌ Dropped |

**What the research found:**
1. **Only one stock strategy has a documented edge after costs:** the ORB, restricted to the day's top stocks by opening relative volume.
2. **The edge comes almost entirely from the stock selection, not the breakout.** The same ORB run on all liquid stocks earned only **3.2% a year** and lagged the S&P 500 ([Zarattini, Barbon & Aziz 2024, Tables 1–2](https://alexandria.unisg.ch/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/download)).
3. **Chart patterns do not survive rigorous testing** as trading signals (Section 3).

**Reality check:** in full Taiwan market data, **more than 8 out of 10 day traders lost money in a typical six-month period**, and **fewer than 1% earned reliable profits after fees** ([Barber, Lee, Liu & Odean](http://www.econ.yale.edu/~shiller/behfin/2004-04-10/barber-lee-liu-odean.pdf); [Cross-Section of Speculator Skill](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/The%20Cross-Section%20of%20Speculator%20Skill.pdf)). Even the 500 most active traders earned **+14.4 bps/day before costs but −7.4 bps/day after**. Taiwan's costs were high: about 10 bps commission plus a 0.3% tax on sales. Your commission is $0, so that specific cost drag is smaller for you, but the skill finding still applies.

---

## 1. Core strategy — ORB on Stocks in Play (grade: MODERATE)

### 1.1 Evidence

| Version | Total return 2016–2023 | Annual | Sharpe | Hit rate | Max DD |
|---|---|---|---|---|---|
| **ORB + relative-volume filter (top 20)** | **1,637%** | 41.6% IRR | **2.81** | 48.4% | 12% |
| Same ORB, all eligible stocks | 29% | 3.2% | 0.48 | — | — |
| S&P 500 | 198% | — | 0.78 | — | — |

All figures are net of $0.0035/share commission, from Zarattini, Barbon & Aziz (2024), verified 3-0 from the paper's full text ([paper](https://alexandria.unisg.ch/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/download)).

**Why the grade is only "moderate":**
- It's a single unpublished working paper, and the backtest is in-sample.
- The authors run a commercial trading and education business (Concretum and Bear Bull Traders).
- Slippage on stop-order fills is **not modelled**.
- It traded **long and short** with **up to 4× leverage**.
- The QuantConnect re-implementation uses a smaller universe and gives weaker results in some years ([QuantConnect](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)).

**Supporting data:** in Bulkowski's sample of 55 US stocks on 1-minute data, **the day's high was set in the first hour 49% of the time, and the day's low 46% of the time** ([ThePatternSite](https://thepatternsite.com/IntradayHighLow.html)). That's consistent with the open being where the day's direction gets decided. It comes from a practitioner, so it wasn't graded as evidence.

### 1.2 Exact rules from the paper, and your version

| Rule | Paper (verified) | **Your version** |
|---|---|---|
| Price | Open > $5 | $5–$50 (so a $1,000 position is a reasonable number of shares) |
| Liquidity | 14-day avg volume ≥ 1,000,000 shares | same |
| Volatility | 14-day ATR > $0.50 | same |
| Stocks in Play | Opening-range relative volume ≥ 100%; **top 20** by relative volume | same ranking; you take **the #1 qualifying long** |
| Direction | Follows the first 5-min candle; **no trade on a doji** | **Long only.** Skip bearish candles and dojis |
| Entry | Stop order at the opening-range high (long) / low (short) | Buy stop at first-candle high + $0.01 |
| Stop-loss | **10% of 14-day ATR** from the entry fill | same |
| Target | None | None (optional far target) |
| Exit | At the 4:00 pm ET close | Sell at ~15:50–15:55 ET (13:50–13:55 MT), or hold to the close on strong-market days (Section 2) |
| Risk | 1% of capital per trade, ≤ 4× leverage | 2% cap, **no leverage** → cash-limited (see 1.4) |

**Relative volume** = volume in the 9:30–9:35 bar ÷ the average 9:30–9:35 volume over the previous 14 days.

### 1.3 Screening

**Pre-market (6:00–7:25 MT), to build a watchlist:**

| FinViz / TradingView filter | Setting |
|---|---|
| Price | $5 to $50 |
| Average volume | Over 1M |
| ATR | Over 0.5 |
| Gap / pre-market change | Up 3%+ (priority, not required) |
| Catalyst | Earnings today, guidance, analyst action, M&A, FDA/regulatory, major contract |

**At 7:35 MT (9:35 ET), to pick the trade:**
1. Compute opening relative volume for each watchlist name, plus any name the scanner shows with unusual opening volume.
2. Keep the ones at or above 100%, and rank them.
3. Take the highest-ranked name whose first candle is bullish (close above open, not a doji).

### 1.4 Position sizing at $1,000 without leverage

```
shares = min( floor($20 ÷ (0.10 × ATR)),  floor(cash ÷ entry price) )
```

| Price | ATR | Stop distance | Risk-based shares | Cash-limited shares | **Traded** | **Real risk** |
|---|---|---|---|---|---|---|
| $10 | $0.60 | $0.06 | 333 | 100 | **100** | **$6 (0.6%)** |
| $25 | $1.50 | $0.15 | 133 | 40 | **40** | **$6 (0.6%)** |
| $45 | $2.50 | $0.25 | 80 | 22 | **22** | **$5.50 (0.55%)** |

**Cash always binds before risk does.** Your real risk will be about **0.5–0.7% per trade**, and returns will be far smaller than the backtest's leveraged numbers. This will change as the account grows.

### 1.5 US vs TSX
- **US: yes.** All of the evidence is on US stocks.
- **TSX: paper trade only for now.** No TSX evidence was found, and few TSX names pass the 1M-volume and $0.50-ATR filters. Also, **Questrade cannot place trailing *stop* orders on Canadian exchanges, only trailing *stop-limit* orders** ([Questrade](https://www.questrade.com/learning/options-active-trading/trailing-stop-orders)). A stop-limit can fail to fill in a fast drop.

---

## 2. Overlay — Last-half-hour momentum (grade: MODERATE for the index, WEAK for single stocks)

**Evidence:**
- On SPY (1993–2013), the market's return from the prior close through the first half hour predicts the **last half hour's return**. The out-of-sample R² was only **1.4%**, so the signal is weak on any single day. After bid-ask costs, the timing strategy still earned **4.46%/yr (2001–2013)** ([Gao, Han, Li & Zhou, JFE 2018](https://assets.super.so/e46b77e7-ee08-445e-b43f-4ffd88ae0a0e/files/ee7dac49-530b-4950-b5d0-e0b5eee08f2e.pdf)).
- Holding long over the last half hour **without** the signal lost money, so the whole edge is in the signal.
- The effect replicates across **60+ futures markets from 1974 to 2020**. A better predictor is the "rest-of-day" return, from the prior close to 3:30 pm. However, the authors warn it **may not be exploitable after costs** except in S&P 500 futures ([Baltussen, Da, Lammers & Martens, JFE 2021](https://www3.nd.edu/~zda/intramom.pdf)).
- One specific claim, a 6.67%/yr long-short SPY result with a Sharpe of 1.08, was **refuted** in verification and has been removed.

**How you'll use it.** This rule is my inference from the research; it isn't directly tested.
- At **13:30 MT (15:30 ET)**, if you hold an ORB winner:
  - **If SPY is up from yesterday's close:** hold the position to the close.
  - **If SPY is down:** sell at 13:30 MT.
- It is **not** a stand-alone single-stock strategy. The evidence is at the index level.

---

## 3. Chart-pattern and candlestick strategies

You asked for these specifically, so here is what the rigorous research shows.

### 3.1 Classical chart patterns — grade WEAK → dropped as stand-alone
- **Lo, Mamaysky & Wang (2000)** detected head-and-shoulders, double tops and bottoms, triangles, rectangles and broadening formations algorithmically on daily data from 1962–1996.
- Some patterns **do carry statistical information**, especially on Nasdaq stocks. But the authors state explicitly that this **does not imply trading profits**, and they ran no after-cost trading test ([NBER w7613](https://www.nber.org/system/files/working_papers/w7613/w7613.pdf)).
- **Flags, pennants, wedges and cup-and-handle have no rigorous after-cost evidence at all**, intraday or daily.
- **Bulkowski's pattern statistics** are the most widely quoted success rates. By his own description, they come from **daily charts in bull markets**, measure the share of pattern *types* that improved, and are **not cost-adjusted or corrected for data snooping** ([ThePatternSite](https://thepatternsite.com/studystudy.html)). They don't carry over to day trading.

### 3.2 Candlestick patterns — grade NONE → dropped
| Study | Data | Finding |
|---|---|---|
| **Duvinage, Mazza & Petitjean (2013)**, *Quantitative Finance* | **5-minute bars**, 30 DJIA stocks, 83 rules | About ⅓ beat buy-and-hold **before** costs. After costs and data-snooping correction, **no single rule beats buy-and-hold**. Fully automated combinations of the best rules also fail ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2125889)) |
| **Marshall, Young & Rose (2006)**, *J. Banking & Finance* | Daily, 35 DJIA stocks, 28 rules (hammer, doji, engulfing, harami, star patterns…) | **22 of 28 rules had no significant profit on any stock.** The best rule worked on only 3 stocks ([paper](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID1083064_code114671.pdf?abstractid=980583&mirid=1)) |
| Tharavanij et al. (2017), *SAGE Open* | Daily, Thai SET50 | Reversal patterns cannot reliably predict direction ([paper](https://journals.sagepub.com/doi/10.1177/2158244017736799)) |
| Lu & Shiu (2016), *Applied Economics* | Daily, DJIA 1974–2009 | The lone dissent: some 1-day patterns "may" be profitable, with no net-of-cost result ([abstract](https://www.tandfonline.com/doi/abs/10.1080/00036846.2015.1137553)) |

### 3.3 Support/resistance and channel breakouts — grade NONE → dropped
Sullivan, Timmermann & White (1999) tested **7,846 technical rules** on the DJIA, including support/resistance, channel breakouts and moving averages. After correcting for data snooping, the best rule's edge from 1987–1996 had a p-value of about **0.12**, which the authors called "scant evidence". There was also no significant outperformance on S&P 500 futures ([J. Finance](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00163)).

### 3.4 Where patterns can still fit
- **The ORB is itself a simple chart pattern:** a breakout from a 5-minute range. It works in the research only because of the Stocks-in-Play filter.
- **If you want to explore other patterns, do it in a "pattern lab" on paper only.**
  - Write the rules down **before** testing, for example: *bull flag after an ORB fill: 3–6 bars of pullback holding above VWAP on falling volume, then buy on a break of the flag high, with the stop under the flag low.*
  - Test them **only on Stocks in Play**.
  - Require **at least 50 paper trades** with positive expectancy.
- Treat any pattern that looks good in your own backtest as **data-snooped until it's proven in live paper trading**. That's the lesson of the Sullivan/Timmermann/White study.

---

## 4. Probation — VWAP trend-following (grade: WEAK for single stocks)

- **Evidence:** on QQQ (Jan 2018–Sep 2023), $25,000 grew to **$192,656** net of commissions: a 671% total return, 9.4% max drawdown and Sharpe 2.1 ([Concretum](https://concretumgroup.com/volume-weighted-average-price-vwap-the-holy-grail-for-day-trading-systems/)). This figure came from the paper's content but was **not adversarially verified**. It's also an ETF, long and short, and not common stocks.
- **Your version (paper only):**
  - After 10:00 ET, a Stocks-in-Play name above a rising VWAP pulls back to VWAP, then a 5-minute bar closes back above it → buy.
  - Exit on a 5-minute close below VWAP, or at the time stop.
- **It needs automated alerts,** because you can't watch for the exit signal on a two-hour check-in schedule.

---

## 5. Daily operating schedule (Mountain Time = ET − 2 h, all year)

| MT | ET | Activity | Automatable |
|---|---|---|---|
| **6:00–7:15** | 8:00–9:15 | Market context (economic calendar; consider sitting out FOMC and CPI mornings). Pre-market screen. AI summary of each candidate's catalyst. Watchlist of up to 10 names. Confirm settled USD cash | ✅ Fully |
| **7:15–7:25** | 9:15–9:25 | Pre-compute the 14-day ATR, 14-day average opening-bar volume, stop distance and share count for each name | ✅ Fully |
| **7:30–7:35** | 9:30–9:35 | Hands off while the first 5-minute candle forms | — |
| **7:35–7:40** | 9:35–9:40 | **Key check-in.** Rank by relative volume, pick the top bullish name, send a **BUY STOP** order → you approve on your phone | ✅ Proposal automated · **you approve** |
| On fill | — | Send a **SELL STOP** at entry − 0.10 × ATR → you approve | ✅ · **you approve** |
| **9:30** | 11:30 | Check-in 1: cancel an unfilled entry *(my adaptation; the paper left orders working all day)*. Confirm the stop is live | ✅ |
| **11:30** | 13:30 | Check-in 2: position status and any new news; check the daily loss limit (−5% = $50) | ✅ Status push |
| **13:30** | 15:30 | **Overlay decision:** SPY down on the day → sell now; SPY up → hold into the close | ✅ Proposal · **you approve** |
| **13:50–13:55** | 15:50–15:55 | Sell any remaining position; cancel all orders; confirm the account is flat | ✅ Proposal · **you approve** |
| **14:15–14:45** | 16:15–16:45 | Post-close review (Section 6) | ✅ Mostly |

---

## 6. Post-close review, metrics and kill switches

**Daily journal:**
- Ticker, catalyst, relative-volume rank, gap %
- Planned vs actual entry, stop and shares (this measures **slippage**, which the paper didn't model)
- Exit reason
- P&L in dollars and in **R** (multiples of the risk taken)
- Rules followed (yes/no)
- Chart screenshot

**Weekly:**
- Win rate. The paper's hit rate was 48%, so expect **more losers than winners**.
- Average win R vs average loss R
- **Expectancy** = (win% × avg win R) − (loss% × avg loss R)
- Profit factor, max drawdown, average slippage
- Rule adherence %

**Kill switches (decide these now):**
- **50 live trades with expectancy ≤ 0** → stop and return to paper trading.
- **−15% drawdown from peak** → stop and review.
- **Scale capital** only after 2 consecutive positive months with ≥ 90% rule adherence.

---

## 7. Canadian practical constraints

| Topic | What's verified | Source |
|---|---|---|
| **Commissions** | **$0** on US and Canadian listed stocks and ETFs | [Questrade fees page](https://www.questrade.com/pricing/self-directed-commissions-plans-fees/transaction) (read directly) |
| **ECN fees** | None on normal routing. Only **direct-routed** US orders pay $0.003–$0.004/share | same |
| **SEC fee** | 0.0000206 × sale value on US sells (≈ $0.02 on a $1,000 sale) | same |
| **Currency conversion** | **1.5%** each way | same |
| **Norbert's Gambit** | Flat journaling fee (reported as $9.95); cheaper than 1.5% once you convert more than roughly $700–$1,000 | [WealthSavvy](https://wealthsavvy.ca/norberts-gambit-questrade/) (secondary) |
| **Settlement** | **T+1** for Canadian and US equities | [Questrade](https://www.questrade.com/learning/options-active-trading/day-trading-canada-rules-accounts) (read directly) |
| **Cash-account reuse of unsettled funds** | ⚠️ **Not confirmed.** Questrade's page doesn't state its freeriding policy. Plan on **one trade a day with settled cash** until support confirms | — |
| **PDT rule** | The $25k rule was eliminated effective June 4, 2026. Canada has no equivalent. The FINRA page blocked automated reading, so this is confirmed through secondary sources | [Schwab](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum), [FINRA 26-10](https://www.finra.org/rules-guidance/notices/26-10) |
| **Bracket orders** | Available in Edge Web, Edge Desktop and Edge Mobile. Gaps can skip your stop | [Questrade](https://www.questrade.com/learning/investment-concepts/adv-order-types-durations/bracket-orders) (read directly) |
| **Tax** | Frequent short-term trading can be treated as **business income, which is 100% taxable** (vs 50% for capital gains). **Don't day trade in a TFSA** | [Questrade](https://www.questrade.com/learning/options-active-trading/day-trading-canada-rules-accounts), CRA IT-479R (the CRA page couldn't be fetched) |

**Currency recommendation:** convert once and keep the account's trading money in USD. Converting on every trade would cost about 3% per round trip, which is more than the strategy's expected edge.

---

## 8. Automation — "Claude proposes, Stephen approves"

### 8.1 What's verified

| Tool | Can do | Can't do |
|---|---|---|
| **Questrade API (personal app)** | Account data and market data (quotes, candles, balances) | **Place trades.** That's only for approved **partner** developers ([Questrade API](https://www.questrade.com/api/documentation/getting-started), read directly) |
| **Questrade connector in Claude** (already linked to your account) | Create, modify and cancel **market, limit, stop and stop-limit** orders, **day or GTC**. **Every order goes to your Questrade app for approval** before it reaches the market. Fractional shares are allowed on market day orders for eligible tickers | **No bracket orders.** The protective stop has to be a second order after the fill, so each trade needs 2–3 approvals: entry, stop, then exit |
| TradingView webhooks | Send an alert to your phone or a bot | Execute trades at Questrade |
| QuantConnect | Backtest ORB on Stocks in Play; a public implementation exists | — |

### 8.2 Pipeline

```
06:00 MT  Screen + catalyst summaries ─► watchlist
07:25 MT  ATR, 14-day opening-bar volume, share counts pre-computed
07:35 MT  Rank by relative volume ─► BUY STOP proposal ─► 📱 approve
  fill    SELL STOP (entry − 0.10×ATR) proposal ─► 📱 approve
09:30 MT  Unfilled? ─► cancel proposal ─► 📱 approve
13:30 MT  SPY overlay ─► sell or hold proposal ─► 📱 approve
13:50 MT  Flatten proposal ─► 📱 approve
14:15 MT  Pull fills ─► journal ─► metrics (weekly report on Saturday)
```

**Risk to know about:** between the entry filling and you approving the stop, **the position is unprotected**. Two ways to handle it:
- Approve promptly. The buy-stop typically fills within minutes of 7:35 MT.
- Enter the ORB as a **bracket order yourself in Edge Mobile**, and use the connector only for proposals, cancels and exits.

### 8.3 Rollout

| Phase | Duration | Exit criteria |
|---|---|---|
| 0: Paper, manual | 4–6 weeks | ≥ 30 trades, ≥ 90% rule adherence |
| 1: Backtest check (QuantConnect, long-only, no leverage, 2024–2026) | parallel | Positive expectancy out-of-sample |
| 2: Semi-automated paper | 4 weeks | Proposals match the rules 100% |
| 3: Live $1,000, approve every order | ≥ 50 trades | Positive expectancy |
| 4: Scale, and test the VWAP / pattern-lab ideas on paper | ongoing | 2 positive months per step |

---

## 9. Evidence gaps

The research could **not** verify these:
- Any rigorous evidence for gap-and-go, gap fade, VWAP mean reversion or news plays.
- TSX-specific day-trading evidence.
- Questrade's cash-account policy on reusing unsettled funds.
- The CRA IT-479R text itself (the page couldn't be fetched).
- The Brazil day-trader study (Chague et al.). Its PDF couldn't be read, so it isn't used in this version.

---

## Sources

**Verified primary (full text read):**
- [Zarattini, Barbon & Aziz (2024), *A Profitable Day Trading Strategy for the U.S. Equity Market*](https://alexandria.unisg.ch/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/download)
- [Gao, Han, Li & Zhou (2018), *Market Intraday Momentum*, JFE](https://assets.super.so/e46b77e7-ee08-445e-b43f-4ffd88ae0a0e/files/ee7dac49-530b-4950-b5d0-e0b5eee08f2e.pdf)
- [Baltussen, Da, Lammers & Martens (2021), *Hedging Demand and Market Intraday Momentum*, JFE](https://www3.nd.edu/~zda/intramom.pdf)
- [Concretum: *Beat the Market*, SPY intraday momentum](https://concretumgroup.com/beat-the-market-an-effective-intraday-momentum-strategy-for-sp500-etf-spy/)
- [Lo, Mamaysky & Wang (2000), *Foundations of Technical Analysis*, NBER w7613](https://www.nber.org/system/files/working_papers/w7613/w7613.pdf)
- [Duvinage, Mazza & Petitjean (2013), intraday candlesticks](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2125889)
- [Marshall, Young & Rose (2006), candlesticks on DJIA stocks](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID1083064_code114671.pdf?abstractid=980583&mirid=1)
- [Tharavanij et al. (2017), SAGE Open](https://journals.sagepub.com/doi/10.1177/2158244017736799)
- [Lu & Shiu (2016), Applied Economics](https://www.tandfonline.com/doi/abs/10.1080/00036846.2015.1137553)
- [Sullivan, Timmermann & White (1999), J. Finance](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00163) · [PDF](https://www.kevinsheppard.com/files/teaching/mfe/advanced-econometrics/Sullivan_Timmermann_White.pdf)
- [Barber, Lee, Liu & Odean, *Do Individual Day Traders Make Money?*](http://www.econ.yale.edu/~shiller/behfin/2004-04-10/barber-lee-liu-odean.pdf)
- [Barber, Lee, Liu & Odean, *The Cross-Section of Speculator Skill*](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/The%20Cross-Section%20of%20Speculator%20Skill.pdf)
- [Questrade: transaction fees](https://www.questrade.com/pricing/self-directed-commissions-plans-fees/transaction)
- [Questrade: trailing stop orders](https://www.questrade.com/learning/options-active-trading/trailing-stop-orders)
- [Questrade: bracket orders](https://www.questrade.com/learning/investment-concepts/adv-order-types-durations/bracket-orders)
- [Questrade: day trading in Canada](https://www.questrade.com/learning/options-active-trading/day-trading-canada-rules-accounts)
- [Questrade API: getting started](https://www.questrade.com/api/documentation/getting-started)

**Secondary / practitioner:**
- [Concretum: VWAP trend trading](https://concretumgroup.com/volume-weighted-average-price-vwap-the-holy-grail-for-day-trading-systems/)
- [QuantConnect: ORB for Stocks in Play](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)
- [Bulkowski: intraday highs and lows](https://thepatternsite.com/IntradayHighLow.html) · [Bulkowski: study of studies](https://thepatternsite.com/studystudy.html)
- [Schwab: SEC approves scrapping the $25,000 minimum](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum) · [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [WealthSavvy: Norbert's Gambit at Questrade](https://wealthsavvy.ca/norberts-gambit-questrade/)
