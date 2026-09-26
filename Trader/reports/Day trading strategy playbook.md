# Day Trading Strategy Playbook

*Prepared for Stephen · September 26, 2026 · US + TSX common stocks · Questrade non-registered account · no leverage · $1,000 CAD starting capital*

---

## Bottom line up front

After screening the standard day-trading strategies against published evidence, **only one strategy has credible, recent, stock-specific evidence of an edge after costs: the 5-minute Opening Range Breakout (ORB) restricted to "Stocks in Play"** ([Zarattini, Barbon & Aziz, 2024](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284)). It also happens to be the best fit for your constraints: one decision at the open, a pre-set stop, and an exit at the close — so it works with brief check-ins and is highly automatable.

One secondary strategy (**VWAP trend-hold**) is kept on **probation** — paper-trade only until your own results prove it. Everything else (gap fade, VWAP mean reversion, small-cap gap-and-go, last-half-hour momentum, news scalping) is **dropped** for weak, decayed, or practitioner-only evidence, or because it needs constant screen time.

Three honest cautions before anything else:

1. **Most day traders lose money.** In the most complete dataset ever studied (all Taiwan Stock Exchange trades, 1992–2006), **more than 80% of day traders lost money in a typical six-month period and fewer than 1% were predictably profitable after fees** ([Barber, Lee, Liu & Odean](https://faculty.haas.berkeley.edu/odean/papers/Day%20Traders/Day%20Trade%20040330.pdf)). In Brazil, **97% of people who persisted more than 300 days lost money** ([Chague, De-Losso & Giovannetti](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101)).
2. **The strongest backtest is still a backtest** — co-authored by a practitioner who sells trading education, run with leverage, US-only, and at least one independent replication found weaker results outside 2016 ([QuantConnect replication](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)).
3. **With $1,000 and no leverage, your 2% risk target usually cannot be reached** — cash, not the stop, caps your position size (worked examples below). That is not a bad thing while learning.

> **Research note:** the environment's network policy blocked direct fetching of source documents during this research, so findings rely on search-engine summaries of the sources cited. The key numbers (Barber et al., Zarattini et al., Questrade fees and order rules) were each found in more than one summary, but please treat exact figures as "verify before relying on".

---

## 1. Strategy selection: what was kept and what was dropped

| Strategy | Evidence | Verdict | Why |
|---|---|---|---|
| **ORB on Stocks in Play** | SSRN paper, 7,000+ US stocks 2016–2023, **Sharpe 2.81, 41.6% annualized, near-zero beta, after costs** ([Zarattini et al.](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284)); earlier ORB paper on QQQ ([Aziz & Zarattini](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622)) | ✅ **Core** | Best evidence; simple rules; bracket-order friendly |
| **VWAP trend-hold** | QQQ backtest: long above VWAP / short below, 2018–2023, Sharpe 2.1, max DD 9.4% ([Zarattini & Aziz, 2023](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631351)) | 🟡 **Probation** | Tested on an ETF not single stocks; short side unavailable to you; needs alerts to manage |
| Last-half-hour momentum | Peer-reviewed (JFE 2018) on SPY 1993–2013 ([Gao, Han, Li & Zhou](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866)) | ❌ Dropped | A 2022–2026 retest on 1,085 sessions found the effect **flat in every year** ([FirmTape retest](https://dev.to/firmtape/intraday-momentum-is-dead-in-the-0dte-era-we-measured-it-on-1085-spx-sessions-43g0) — a blog, not peer-reviewed); index-level, not single stocks |
| Gap fade / gap fill | Firm-level overnight/intraday reversal literature ([Baltussen, Da & Soebhag](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5039009)) | ❌ Dropped | Direct evidence for single-stock gap fills is mixed; often requires shorting |
| VWAP mean reversion (RSI extremes) | Practitioner articles only ([Tradezella](https://www.tradezella.com/blog/vwap-trading-strategy), [LuxAlgo](https://www.luxalgo.com/blog/vwap-entry-strategies-for-day-traders/)) | ❌ Dropped | No rigorous study found; fails in trending markets; needs constant monitoring |
| Small-cap gap-and-go ($2–$20, float <20M) | Practitioner guides; ~52% win rates reported without cost detail ([ORB Setups](https://orbsetups.com/research/gap-and-go-trading-strategy-how-to-combine-pre-market-gaps-with-opening-range-breakouts/), [Warrior Trading](https://www.warriortrading.com/gap-go/)) | ❌ Dropped (as a standalone) | Halts, wide spreads and stop slippage make it unsafe for brief check-ins. Its **useful idea — pre-market catalyst scanning — is folded into the ORB pipeline** |
| News scalping | None credible | ❌ Dropped | Requires speed and screen time you don't have |

---

## 2. Core strategy — Opening Range Breakout on Stocks in Play (ORB-SIP)

### 2.1 Why it works (the theory)

Stocks with news (earnings, upgrades, FDA decisions, guidance) attract unusual volume at the open. When the first 5 minutes show a clear direction on heavy volume, the move tends to continue through the day. The paper found that **restricting ORB to the top 20 "Stocks in Play" ranked by opening relative volume was what made it profitable** — plain ORB on ordinary stocks or the S&P 500 has been weak in other tests ([QuantifiedStrategies](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/)).

### 2.2 Screening and candidate selection

**Stage A — Pre-market watchlist (US: 8:00–9:25 ET = 6:00–7:25 MT)**

| Filter | Paper's rule | Your adaptation for $1,000 |
|---|---|---|
| Price | > $5 | **$5–$50** (so a $1,000 position is several shares; the replication found results deteriorated above $50 ([summary](https://github.com/jsboige/CoursIA/issues/16355))) |
| 14-day average volume | ≥ 1,000,000 shares | same |
| 14-day ATR | > $0.50 | same |
| Catalyst | "mostly news-driven" | Earnings, guidance, analyst action, M&A, FDA, major contract — **required** in your version |
| Pre-market gap | not required | Flag gaps ≥ 3% with pre-market volume well above normal as priority candidates |

FinViz example (pre-market): *Price $5–$50 · Average Volume over 1M · ATR over 0.5 · Gap Up 3%+ (or Change 3%+) · Earnings Today / News today*. TradingView and Questrade's scanner can replicate the price/volume/ATR filters.

**Stage B — The ranking at 9:35 ET (7:35 MT)**

Compute **opening relative volume (RVOL)** = volume in the first 5-minute bar ÷ average first-5-minute-bar volume over the last 14 days. Keep only stocks with **RVOL ≥ 100%** (opening volume at least equal to its 14-day average for that interval), and rank from highest to lowest; the paper traded only the **top 20** ([CXO Advisory summary](https://www.cxoadvisory.com/technical-trading/day-trading-with-an-opening-range-breakout-strategy/)). **Trade only the top of the list** — with $1,000, that means **your single best-ranked long setup**.

### 2.3 Entry (buy) criteria — long only

The paper trades both directions; you trade **long only** (shorting needs a margin account and borrowed shares, which conflicts with "no leverage").

1. The first 5-minute candle (9:30–9:35 ET) **closed higher than it opened** (bullish).
2. Stock is in the top-ranked Stocks in Play with a catalyst.
3. At 9:35 ET, place a **buy stop order at the high of the first 5-minute candle** (1 cent above), day order.
4. If not filled by **11:30 ET (9:30 MT)**, cancel it. *(The paper left orders working all day; the 11:30 cut-off is my adaptation so you are not surprised by a late fill while at work — test both in paper trading.)*

### 2.4 Exit (sell) criteria

| Exit | Rule |
|---|---|
| **Stop-loss** | Entry − **10% of the 14-day ATR** (paper's rule). Example: ATR $2.00 → stop $0.20 below entry |
| **Profit target** | **None in the paper** — winners ride to the close. For a bracket order, set a far target (e.g., +8× the stop distance) mainly so the bracket exists |
| **Time stop** | Close the position at **15:50–15:55 ET (13:50–13:55 MT)**. Never hold overnight |
| Daily loss limit | Stop trading for the day at −5% of account ($50) |

The tight stop means **many small losses and fewer, larger wins** — expect a win rate well under 50%. The edge comes from the size of the winners.

### 2.5 Position sizing at $1,000 (no leverage)

```
risk_shares  = floor( $20 risk ÷ stop distance )
cash_shares  = floor( available cash ÷ entry price )
shares       = min( risk_shares, cash_shares )
```

| Stock price | 14-day ATR | Stop (10% ATR) | Risk-based shares | Cash-limited shares | **Shares traded** | **Actual $ at risk** |
|---|---|---|---|---|---|---|
| $10 | $0.60 | $0.06 | 333 | 100 | **100** | **$6 (0.6%)** |
| $25 | $1.50 | $0.15 | 133 | 40 | **40** | **$6 (0.6%)** |
| $45 | $2.50 | $0.25 | 80 | 22 | **22** | **$5.50 (0.55%)** |

**Takeaway:** without leverage, the 10%-of-ATR stop is so tight that cash is always the limit. You will actually risk ~0.5–0.7% per trade, not 2%. Your aggressive setting becomes relevant only if you widen stops (which changes the tested strategy) or once the account is much larger. The paper's headline returns relied on leverage (up to 4×), so **do not expect paper-sized returns in an unlevered cash account.**

### 2.6 US vs TSX

- **US: yes.** All the evidence is US-listed stocks.
- **TSX: not recommended for now.** No TSX-specific evidence was found; few TSX names meet the volume/ATR filters; and on the TSX **the only stop order allowed is a stop-limit with the same stop and limit price**, which can fail to fill on a fast drop ([Questrade](https://www.questrade.com/learning/options-active-trading/trailing-stop-orders)). Trailing stops at Questrade are **US securities only**.

---

## 3. Probation strategy — VWAP trend-hold

**Paper-trade only** until at least 40 trades show positive expectancy in your journal.

- **Evidence:** VWAP trend-following on QQQ grew $25k to about $193k (2018–2023) net of commissions ([Zarattini & Aziz](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631351)). Not tested on single stocks; your long-only version drops half the strategy.
- **Screen:** same Stocks-in-Play list as ORB (it re-uses the pipeline).
- **Entry:** after 10:00 ET, a top-ranked stock that is **above a rising VWAP** pulls back to VWAP and the next 5-minute bar closes back above it → buy.
- **Exit:** a **5-minute close below VWAP**, or 15:55 ET time stop. Protective hard stop 1× the 14-day ATR ÷ 4 below entry *(my adaptation, untested)*.
- **Sizing:** same formula as ORB.
- **Why probation:** managing it means reacting to a 5-minute close below VWAP — impossible with manual 2-hour check-ins. It only becomes practical once alerts are automated (Section 7).
- **Market:** US.

---

## 4. Daily operating schedule

Alberta and New York change clocks on the same dates, so **Mountain Time is always ET − 2 hours**.

| Time (MT) | Time (ET) | Activity | Automatable? |
|---|---|---|---|
| **6:00–7:15** | 8:00–9:15 | **Pre-market.** Check market context (futures, major economic releases, Fed days — consider sitting out on FOMC/CPI mornings). Run Stage A screen. For each candidate, AI-summarise the catalyst (earnings beat/miss, guidance, analyst action). Build watchlist of ≤10 names. Confirm settled cash available | ✅ Fully (screen + news summary) |
| **7:15–7:25** | 9:15–9:25 | Pre-compute 14-day ATR and 14-day average first-5-min volume for the watchlist; pre-calculate stop distances and share counts | ✅ Fully |
| **7:30–7:35** | 9:30–9:35 | **Open.** Do nothing — let the first 5-minute candle form | — |
| **7:35–7:40** | 9:35–9:40 | **ORB decision.** Rank by RVOL; take the top bullish candle with a catalyst; place **buy-stop + bracket (stop-loss / far target)**. This is the one check-in you must not miss | ✅ Proposal automated; **you approve** |
| **9:30** | 11:30 | **Check-in 1.** Cancel unfilled ORB entry. If in a trade: confirm stop is live; do **not** move the stop against the plan. (Probation strategy: log whether a VWAP setup occurred) | ✅ Cancel can be proposed automatically |
| **11:30** | 13:30 | **Check-in 2.** Confirm position/stop status; check for news on the holding. Daily loss limit reached? → done for the day | ✅ Status push notification |
| **13:45–13:55** | 15:45–15:55 | **Pre-close.** Sell any open position (market or marketable limit) before 15:55 ET. Cancel all open orders. Verify account is flat | ✅ Proposal automated; **you approve** |
| **14:15–14:45** | 16:15–16:45 | **Post-close review** (Section 5) | ✅ Mostly |

---

## 5. Post-close review and journaling

**Every day (15 minutes):**

| Log field | Why |
|---|---|
| Ticker, catalyst, RVOL rank, gap % | Learn which catalysts work |
| Planned entry / stop / shares vs actual fills | Measures **slippage** — critical on a small account |
| Exit reason (stop, time, target) | Checks rule adherence |
| P&L in $ and in **R** (multiples of risk) | Makes trades comparable across sizes |
| Rules followed? (Y/N + note) | The single most predictive metric for improvement |
| Screenshot of the 5-minute chart | For weekly review |

**Weekly (Saturday, 30 minutes):** win rate, average win R vs average loss R, **expectancy** = (win% × avg win R) − (loss% × avg loss R), profit factor, max drawdown, slippage per trade, % of days rules were followed.

**Kill / scale criteria (set them now, not in the moment):**

- After **50 live trades**: expectancy ≤ 0 → stop live trading, return to paper, review.
- Account drawdown of **−15%** from peak → stop and review.
- Scale up capital only after **two consecutive months** of positive expectancy **and** ≥ 90% rule adherence.

**Tax records:** keep the journal — it doubles as your T2125 business-income records (Section 6).

---

## 6. Practical constraints in Canada

### 6.1 The Pattern Day Trader rule — not an issue
The SEC approved eliminating FINRA's $25,000 PDT minimum on April 14, 2026, effective **June 4, 2026**, with brokers allowed until October 20, 2027 to implement ([Schwab](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum), [FINRA Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)). Canada has no equivalent rule ([Lifetimes Canada](https://lifetimescanada.com/blog/stock-trading/pattern-day-trader-pdt-rule-does-it-apply-in-canada-in-2026)).

### 6.2 Settlement (T+1) — the real limit in a cash account
Stocks settle one business day after the trade ([Questrade](https://www.questrade.com/learning/options-active-trading/day-trading-canada-rules-accounts)). In a cash account, **buying with settled cash and selling the same day is fine**. The violation ("freeriding") is buying with *unsettled* sale proceeds and then selling that new position before the funds settle ([Wikipedia](https://en.wikipedia.org/wiki/Freeriding_(stocks))).

**Practical rule for you: one ORB trade per day using settled cash.** Proceeds settle next business day, ready for the next trade. This matches the strategy (one top-ranked name) anyway.

> ⚠️ One search summary suggested Questrade makes cash available before settlement; others describe cash-account violations. **Ask Questrade support to confirm their exact cash-account policy** before trading more than once a day.

### 6.3 Costs
| Cost | Amount | Impact on $1,000 |
|---|---|---|
| Commissions | **$0** on US and Canadian listed stocks/ETFs since June 2025 ([Questrade](https://www.questrade.com/pricing/self-directed-commissions-plans-fees/transaction)) | None |
| ECN fees | ~$0.0035/share, only on some **direct-routed** US orders; none on Canadian orders ([Questrade FAQ](https://www.questrade.com/learning/questrade-basics/0-commissions-faq/frequently-asked-questions-about-commissions)) | Avoid by using default routing |
| **Currency conversion** | ~**1.5%** built into the rate each way ([BrokerChooser](https://brokerchooser.com/broker-reviews/questrade-review/questrade-fees)) | **~3% per round trip — would wipe out any edge if done per trade** |
| Norbert's Gambit (DLR → DLR.U) | ~$9.95 journaling fee, flat ([WealthSavvy](https://wealthsavvy.ca/norberts-gambit-questrade/)) | ~1% on $1,000, one time |

**Recommendation:** convert your trading capital to USD **once** (Norbert's Gambit, or a one-time 1.5% conversion — similar cost at $1,000; Norbert's wins above ~$700–$1,000) and **keep it in USD**. Set the account to settle in "currency of transaction".

### 6.4 Tax (CRA)
Frequent trading, short holding periods, time spent and market knowledge are the factors CRA uses to classify gains as **business income** ([CRA IT-479R, para. 11](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/it479r/archived-transactions-securities.html); [CRA T.I. 2019-0826051E5](https://taxinterpretations.com/cra/severed-letters/2019-0826051e5)). Scheduled day trading will very likely qualify — even alongside a full-time job ([Lucas CPA](https://www.lucas.cpa/blog/what-does-the-cra-consider-as-day-trading-how-to-know-if-youre-day-trading-or-not-canada)).

- Profits are **100% taxable** at your marginal rate (vs 50% inclusion for capital gains) ([TaxTips.ca](https://www.taxtips.ca/personaltax/investing/taxtreatment/are-your-investment-gains-and-losses-capital-or-income.htm)).
- Report on **T2125**; losses are deductible against other income; data feeds, platform fees, and similar costs can be deductible ([Humbled Trader](https://www.humbledtrader.com/blog/how-to-navigate-day-trading-tax-in-canada/)).
- **Never day trade in a TFSA** — CRA can tax the whole TFSA's trading income.
- Worth a one-time conversation with an accountant once you go live.

---

## 7. Automation roadmap — toward "AI proposes, Stephen approves"

### 7.1 What the tools can and can't do

| Component | Capability | Limit |
|---|---|---|
| **Questrade public API** | Quotes, candles, balances, positions ([docs](https://www.questrade.com/api/documentation/getting-started)) | **Order placement is for approved partners only**; rate limits return HTTP 429 ([docs](https://www.questrade.com/api/documentation/rate-limiting)) |
| **Questrade connector in Claude** (already connected to your account) | Quotes, candles, positions, and **`create_order_instruction`, which sends a push-to-approve request to your Questrade mobile app** — nothing executes until you tap approve | **This is exactly the "fully automated with my confirmation" model you asked for.** Not yet tested: whether it supports buy-stop entries and bracket orders — must be verified with its preview tool first |
| **TradingView alerts + webhooks** | Price/indicator alerts POSTed to a URL; paid plan + 2FA required; 3-second timeout ([TradingView](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)) | Alerts only — execution still goes through Questrade |
| Webhook → phone bot | Open-source relays to Telegram/Discord/email ([TradingView-Webhook-Bot](https://github.com/fabston/TradingView-Webhook-Bot)) | You still place/approve the order |
| FinViz (Elite for pre-market/real-time exports) | Stage A screening | Free version is delayed |
| Backtesting | [QuantConnect](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/) has a public ORB-Stocks-in-Play implementation | Use it to re-test on 2024–2026 data before going live |
| Paper trading | Questrade practice account; TradingView paper trading | — |

### 7.2 Target pipeline (US ORB-SIP)

```
06:00 MT  Scheduler starts ─► Screen (FinViz / Questrade candles) ─► AI catalyst summaries
                                                                    │
07:25 MT  Pre-compute ATR, 14-day first-5-min volume, share counts ◄┘
07:35 MT  Pull first 5-min bar ─► rank RVOL ─► pick top bullish candidate
          ─► build order: BUY STOP @ high+0.01, STOP @ entry−0.1×ATR, qty = min(risk, cash)
          ─► Questrade create_order_instruction ─► 📱 push to your phone ─► YOU APPROVE / DENY
09:30 MT  Unfilled? ─► propose cancel ─► 📱 approve
11:30 MT  Status push (position, P&L, stop live?)
13:50 MT  Open position? ─► propose SELL ─► 📱 approve
14:15 MT  Pull fills ─► write journal row ─► daily metrics ─► weekly report on Saturday
```

A buy-stop placed at 7:35 MT doesn't need an instant tap — it waits for the price to trigger — so a phone approval within a few minutes works.

### 7.3 Phased plan

| Phase | Duration | Goal | Exit criteria |
|---|---|---|---|
| **0 — Paper, manual** | 4–6 weeks | Run the schedule by hand on paper; learn the rules | ≥ 30 trades logged, ≥ 90% rule adherence |
| **1 — Backtest check** | parallel | Re-run ORB-SIP (long-only, no leverage, 11:30 cancel) on 2024–2026 data in QuantConnect | Positive expectancy after costs out-of-sample |
| **2 — Semi-automated paper** | 4 weeks | Claude screens, ranks, sizes and proposes; you enter on paper | Proposals match rules 100% |
| **3 — Live, $1,000, approve-each-trade** | ≥ 50 trades | Questrade push-to-approve orders | Positive expectancy after 50 trades |
| **4 — Scale** | ongoing | Add capital in steps; revisit VWAP probation strategy with automated alerts | Two positive months per step |

---

## 8. Conclusion

The evidence supports **one** strategy for you: **long-only 5-minute ORB on catalyst-driven Stocks in Play, US-listed, $5–$50, one trade a day, 10%-of-ATR stop, out by the close.** It fits your check-in schedule and can be automated end-to-end with a push-to-approve step on every order. VWAP trend-hold is a candidate for later, once alerts are automated and your paper results justify it.

The honest framing: this is a **learning system with a strict kill switch**, not an income plan. With $1,000 and no leverage your real risk per trade is ~0.5–0.7%, which is the right size for proving (or disproving) the edge with your own data before scaling.

---

### Sources

- [Barber, Lee, Liu & Odean — Do Individual Day Traders Make Money? (Taiwan)](https://faculty.haas.berkeley.edu/odean/papers/Day%20Traders/Day%20Trade%20040330.pdf)
- [Barber et al. — Learning, Fatalism, and Day Trading (summary)](https://www.tradicted.com/research/barber-learning-2020/)
- [Chague, De-Losso & Giovannetti — Day Trading for a Living?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101)
- [QuantPedia — Retail Day Trading is an Uphill Battle](https://quantpedia.com/retail-day-trading-is-an-uphill-battle/)
- [Zarattini, Barbon & Aziz — A Profitable Day Trading Strategy for the U.S. Equity Market](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284)
- [Aziz & Zarattini — Can Day Trading Really Be Profitable?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622)
- [Zarattini & Aziz — VWAP: The Holy Grail for Day Trading Systems](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4631351)
- [QuantConnect — ORB for Stocks in Play replication](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/)
- [CoursIA replication notes](https://github.com/jsboige/CoursIA/issues/16355)
- [QuantifiedStrategies — ORB backtest](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/)
- [Gao, Han, Li & Zhou — Market Intraday Momentum](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866)
- [FirmTape — intraday momentum retest 2022–2026](https://dev.to/firmtape/intraday-momentum-is-dead-in-the-0dte-era-we-measured-it-on-1085-spx-sessions-43g0)
- [Baltussen, Da & Soebhag — End-of-Day Reversal](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5039009)
- [ORB Setups — Gap and Go + ORB](https://orbsetups.com/research/gap-and-go-trading-strategy-how-to-combine-pre-market-gaps-with-opening-range-breakouts/)
- [Warrior Trading — Gap and Go](https://www.warriortrading.com/gap-go/)
- [Tradezella — VWAP strategy](https://www.tradezella.com/blog/vwap-trading-strategy)
- [LuxAlgo — VWAP entry strategies](https://www.luxalgo.com/blog/vwap-entry-strategies-for-day-traders/)
- [Schwab — SEC approves scrapping $25,000 minimum](https://www.schwab.com/learn/story/sec-approves-scrapping-25000-day-trader-minimum)
- [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [Lifetimes Canada — PDT in Canada 2026](https://lifetimescanada.com/blog/stock-trading/pattern-day-trader-pdt-rule-does-it-apply-in-canada-in-2026)
- [Questrade — Day trading in Canada: rules and accounts](https://www.questrade.com/learning/options-active-trading/day-trading-canada-rules-accounts)
- [Questrade — Transaction fees](https://www.questrade.com/pricing/self-directed-commissions-plans-fees/transaction)
- [Questrade — Commissions FAQ](https://www.questrade.com/learning/questrade-basics/0-commissions-faq/frequently-asked-questions-about-commissions)
- [Questrade — Trailing stop orders](https://www.questrade.com/learning/options-active-trading/trailing-stop-orders)
- [Questrade — Bracket orders](https://www.questrade.com/learning/investment-concepts/adv-order-types-durations/bracket-orders)
- [Questrade API — Getting started](https://www.questrade.com/api/documentation/getting-started)
- [Questrade API — Rate limiting](https://www.questrade.com/api/documentation/rate-limiting)
- [BrokerChooser — Questrade fees 2026](https://brokerchooser.com/broker-reviews/questrade-review/questrade-fees)
- [WealthSavvy — Norbert's Gambit with Questrade](https://wealthsavvy.ca/norberts-gambit-questrade/)
- [Wikipedia — Freeriding](https://en.wikipedia.org/wiki/Freeriding_(stocks))
- [CRA IT-479R — Transactions in securities](https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/it479r/archived-transactions-securities.html)
- [CRA T.I. 2019-0826051E5](https://taxinterpretations.com/cra/severed-letters/2019-0826051e5)
- [TaxTips.ca — Capital or income?](https://www.taxtips.ca/personaltax/investing/taxtreatment/are-your-investment-gains-and-losses-capital-or-income.htm)
- [Lucas CPA — What CRA considers day trading](https://www.lucas.cpa/blog/what-does-the-cra-consider-as-day-trading-how-to-know-if-youre-day-trading-or-not-canada)
- [Humbled Trader — Day trading tax in Canada 2026](https://www.humbledtrader.com/blog/how-to-navigate-day-trading-tax-in-canada/)
- [TradingView — Webhook alerts](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
- [TradingView-Webhook-Bot (GitHub)](https://github.com/fabston/TradingView-Webhook-Bot)
- [GrandAlgo — Automating TradingView indicators](https://grandalgo.com/blog/how-to-automate-tradingview-indicators)
