# Business Requirements Document — Trader Simulation Platform

| | |
|---|---|
| **Document** | BRD v0.1 (draft for review) |
| **Owner / sole user** | Stephen McGann |
| **Date** | 2026-09-26 (v0.2: infrastructure aligned with FinanceTracker) |
| **Related** | [`SPEC.md`](SPEC.md) · [`../reports/Day trading strategy playbook.md`](../reports/Day%20trading%20strategy%20playbook.md) |

---

## 1. Purpose

Build a self-hosted system that **runs the day-trading strategies from the playbook as a live simulation using real market data**. Simulated buys and sells are recorded in PostgreSQL instead of being sent to a broker. This lets Stephen:

1. See whether the strategies make money in real market conditions, including realistic fills and slippage, before risking capital.
2. Rehearse the daily operating process, including approving each order from his phone.
3. Build the automation that a future live-trading version will reuse.

## 2. Background

- The playbook identifies the **5-minute Opening Range Breakout (ORB) on Stocks in Play** as the core strategy, with a **last-half-hour SPY overlay** that decides whether to hold into the close. A VWAP trend strategy is a secondary candidate.
- Stephen works full time and can only make brief check-ins, so the process has to run on a schedule and alert him only when a decision is needed.
- Placing orders through Questrade's public API is limited to approved partners. The simulation doesn't need order placement, only market data, which the API provides.

## 3. Objectives and success measures

| # | Objective | Success measure |
|---|---|---|
| O1 | Run the strategy every trading day without manual effort | ≥ 95% of scheduled jobs complete successfully over 20 trading days |
| O2 | Produce trustworthy simulated results | Fills are based on real bid/ask quotes plus slippage. Every simulated trade can be traced to its signal, its proposal, its approval and the market data behind it |
| O3 | Rehearse the human-approval workflow | Approve or reject from Telegram or the web app. Record the time each decision took and how long each position went without a stop |
| O4 | Measure the strategy against the kill-switch criteria | Expectancy (R), win rate, drawdown and rule adherence are calculated automatically, and kill switches block new trades when they trip |
| O5 | Test rule changes quickly | A replay mode re-runs the same strategy code over past days |
| O6 | Grow later without redesign | New strategies plug in without changes to the engine. A live broker adapter can be added later |

## 4. Scope

### 4.1 In scope (version 1)
- A single Docker container running Python scripts (the engine and API), cron jobs, and a React web app served by the same container.
- PostgreSQL: a new `trader` database on the **same instance FinanceTracker uses**.
- Market data from the **Questrade API** (read-only personal app).
- Candidate screening by **scraping FinViz web pages**.
- Catalyst classification using the **Claude API**.
- A **strategy plug-in framework**, with ORB on Stocks in Play and the SPY last-half-hour overlay as the first plug-ins.
- A **simulated broker** with a realistic, quote-based fill model and a T+1 settled-cash ledger.
- An **approval-mode toggle**: approve every order, or run fully automatically.
- **Telegram bot** alerts, with Approve and Reject buttons.
- Deployment on the **existing Docker host (192.168.68.73)**, exposed through **Nginx Proxy Manager** at `trader.sunspinner.ca`, with **Cloudflare Access** in front and the app's own login as a second layer. This is the same deployment pattern as FinanceTracker.
- **Replay mode** that runs over past days using candles.
- Configurable starting capital, currency and enabled markets. Defaults: US$720, US only.
- Kill switches, a trade journal, performance metrics and a weekly report.

### 4.2 Out of scope (version 1)
- Placing real orders at Questrade. The design keeps a broker interface for this later.
- Short selling and leverage.
- Options, futures, crypto and forex.
- Tax filing. The journal can be exported as a CSV for T2125 records.
- Multi-user support.

## 5. Stakeholders and users

| Role | Who | Needs |
|---|---|---|
| Trader / approver / administrator | Stephen | Few, clear alerts; one-tap approvals; an honest performance picture; control over settings |

## 6. Business requirements

Priority: **M** = must, **S** = should, **C** = could.

### 6.1 Market data and screening
| ID | Requirement | Pri |
|---|---|---|
| BR-01 | The system shall get quotes and intraday and daily candles from the Questrade API, and keep the API connection working without manual re-authentication (refresh tokens rotate each time they're used). | M |
| BR-02 | The system shall build each night a **universe** of eligible stocks from FinViz, using price, average volume and ATR filters that can be configured. | M |
| BR-03 | The system shall run a pre-market scan that flags gappers and names with news, and records the catalyst for each candidate. | M |
| BR-04 | The system shall compute opening-range relative volume at 9:35 ET for the universe and rank candidates by it, as the playbook specifies. | M |
| BR-05 | The system shall classify each top candidate's catalyst with the Claude API: type, a quality score and a short reason. | M |

### 6.2 Strategies
| ID | Requirement | Pri |
|---|---|---|
| BR-10 | Strategies shall be **plug-ins** with their own name, version and settings. Enabling, disabling or changing settings shall not require changes to the engine. | M |
| BR-11 | Version 1 shall include the **ORB on Stocks in Play** plug-in (long only), with the playbook rules. | M |
| BR-12 | Version 1 shall include the **SPY last-half-hour overlay**, which decides to hold or exit at 15:30 ET. | M |
| BR-13 | Every signal shall record the rule values that produced it: relative volume, candle direction, ATR, entry, stop and size. | M |

### 6.3 Simulated trading
| ID | Requirement | Pri |
|---|---|---|
| BR-20 | Simulated orders (market, limit, stop, stop-limit) shall fill according to real-time bid/ask quotes plus configurable slippage. | M |
| BR-21 | The simulated account shall track cash, positions and T+1 settlement, and shall enforce a settled-cash rule when that setting is on. | M |
| BR-22 | Starting capital, account currency, the one-time currency conversion cost and enabled markets shall be configurable. | M |
| BR-23 | Every buy and sell shall be stored in PostgreSQL with a complete audit trail. | M |

### 6.4 Approvals and alerts
| ID | Requirement | Pri |
|---|---|---|
| BR-30 | An **approval-mode toggle** shall switch between (a) every order requires approval and (b) fully automatic. | M |
| BR-31 | In approval mode, proposals shall be approvable from **Telegram** (inline buttons) and from the **web app**, and shall expire after a configurable time. | M |
| BR-32 | Telegram shall send alerts for new proposals, fills, stop-outs, kill-switch trips, job failures and the daily summary. | M |
| BR-33 | The system shall record the time to each decision and how long each position went without a stop. | S |

### 6.5 Risk controls
| ID | Requirement | Pri |
|---|---|---|
| BR-40 | Position size = min(risk-based shares, cash-limited shares). The percentage risked per trade is configurable (default 2%). | M |
| BR-41 | Kill switches: a daily loss limit (default −5%), a drawdown limit (default −15% from the peak), and an expectancy check after N trades (default 50, stop if ≤ 0). A tripped switch blocks new entries until Stephen re-enables trading. | M |
| BR-42 | All positions shall be flat by the close. Nothing is held overnight. | M |

### 6.6 Web application
| ID | Requirement | Pri |
|---|---|---|
| BR-50 | A dashboard showing today's status: schedule progress, candidates, pending approvals, open positions, P&L and kill-switch state. | M |
| BR-51 | Trade history and journal, including a rule-adherence Yes/No for each day. | M |
| BR-52 | Performance: equity curve, expectancy, win rate, profit factor, drawdown, slippage and adherence. | M |
| BR-53 | Settings: approval mode, capital, markets, strategy settings, slippage and kill-switch thresholds. | M |
| BR-54 | Replay: start a replay over a date range, view its results and compare them with the live simulation. | M |
| BR-55 | System health: job runs, API token status, errors and logs. | M |
| BR-56 | Access through Nginx Proxy Manager, protected by Cloudflare Access plus the app's own login. No other network path to the app. | M |

### 6.7 Reporting
| ID | Requirement | Pri |
|---|---|---|
| BR-60 | A daily post-close summary on Telegram, plus a journal entry. | M |
| BR-61 | A weekly report every Saturday, with metrics and an AI-written commentary. | S |
| BR-62 | CSV export of all trades. | S |

## 7. Non-functional requirements (summary)

| Area | Requirement |
|---|---|
| Reliability | Jobs can be re-run safely without duplicating work. A failed job is retried and triggers an alert. The engine recovers after a container restart during market hours. |
| Timing | The ORB ranking and proposal are ready by **9:36:00 ET** (90 seconds after the opening bar completes). |
| Time zones | Schedules run in **America/New_York** (daylight saving is handled automatically). The UI shows **Mountain Time**. |
| Security | Secrets are stored outside the image. The Questrade refresh token is encrypted at rest. The web app is only reachable through NPM, behind Cloudflare Access and the app's login. Only Stephen's Telegram chat ID can approve. |
| Auditability | Every decision (signal, proposal, approval, order, fill) is saved with timestamps and the data behind it. |
| Maintainability | Python type hints, automated tests, and replay results that come out the same every time for the same inputs. |
| Cost | Only the Claude API costs money, and a daily spending cap is configurable. |

## 8. Assumptions

1. FinanceTracker's PostgreSQL instance can host a new `trader` database, with owner and app roles following the `ledger_prod` pattern.
2. Docker runs on the shared Docker server at `192.168.68.73`, the same host as FinanceTracker. Images are built on the Mac and shipped with `docker save | ssh docker load`.
3. Stephen can register a Questrade API personal app and create a Telegram bot through @BotFather.
4. Nginx Proxy Manager terminates TLS. `sunspinner.ca` DNS is proxied through Cloudflare, so Cloudflare Access can be applied (to be verified).
5. Scraping FinViz is acceptable for personal, low-frequency use (see risk R3).

## 9. Constraints

- Questrade's API can't place orders for retail users, so execution is simulated only.
- The FinViz free site shows **delayed** data (see risk R3).
- The research evidence covers **US stocks only**, so TSX support is a setting, not a validated strategy.

## 10. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Questrade API quotes may be delayed for some markets or account data subscriptions | Fills in the simulation would be unrealistic | Verify in Phase 0. Record the timestamp of every quote and flag stale quotes. Fall back to 1-minute candles if needed |
| R2 | Questrade request limits and how many candles one request returns | The 9:35 universe scan could be too slow | Cache history each night, query in parallel within the limits, and shrink the universe if needed |
| R3 | FinViz blocks scraping, changes its page layout, or its terms prohibit scraping | The screener fails | Scrape slowly, keep the page parser in one isolated module, allow a manual watchlist upload, and consider FinViz Elite's export |
| R4 | The refresh token expires (for example, if the system is unused for about 7 days) | No market data | Refresh daily even on weekends, alert when a refresh fails, and provide a screen to paste a new token |
| R5 | Simulated results look better than real trading would | False confidence | Quote-based fills, slippage, a settled-cash rule, and measuring how long positions go without a stop |
| R6 | Web app exposed to the internet | Unauthorized approvals | Cloudflare Access, app login, no published host port, the origin locked to Cloudflare IPs, an allow-list of your Telegram chat ID, and an audit log |
| R7 | **Trader and FinanceTracker share one Questrade token chain** | Each app's token refresh invalidates the other's, so both lose market data | Trader uses its **own Questrade API personal app**, and reuses FinanceTracker's proven refresh-locking logic |

## 11. Delivery phases (high level)

| Phase | Deliverable |
|---|---|
| 0 | Spikes to confirm assumptions: Questrade API token handling, quote delay, candle limits; FinViz parsing |
| 1 | Data layer: database schema, Questrade client, FinViz scraper, nightly universe and history cache |
| 2 | Engine: strategy framework, ORB and overlay plug-ins, risk manager, simulated broker |
| 3 | Approvals and Telegram bot; cron schedule |
| 4 | Web app (React) and API; authentication; NPM proxy host + Cloudflare Access; deploy script |
| 5 | Replay mode, reports, kill switches, hardening |

## 12. Sign-off

| Item | Status |
|---|---|
| BRD reviewed by Stephen | ☐ |
| Open assumptions confirmed (§8) | ☐ |
