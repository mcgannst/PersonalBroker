# Phase 0 spike results

Run on Saturday 2026-09-26 against the Trader-dev Questrade app, FinViz, the dev Telegram bot and the `trader_dev` database. The scripts in this folder are throwaway test code, kept for reference. They read secrets from `docker/.env.dev` (git-ignored).

| # | Spike | Result | Status |
|---|---|---|---|
| S1 | Token refresh and rotation | 3 exchanges in a row, 0.3–0.5 s each. Every exchange returned a new refresh token, which was saved before use. The access token lasts 1,800 s. | **Pass** |
| S2 | Quote freshness | Every quote reports `delay: 0` (US and TSX). The market was closed, so the "timestamp within 2 s" check still needs a weekday during market hours. | **Partial**, rerun Monday 09:35–10:00 ET |
| S3 | Candle limits and history depth | Max **20,000 candles per request**. Intraday history (1-min to 1-hour) starts **2026-06-26**, about 3 months back. Daily candles go back 10 years. | **Pass** |
| S4 | Universe scan timing | 694 symbols in **35.2 s** at 19.7 req/s, 0 errors. That leaves room for ~1,200 symbols in 60 s. Ran on Friday's data; confirm at a live 9:35. | **Pass** (recheck live) |
| S5 | FinViz scrape | Universe of **695 tickers**, all parsed, counts match. News tables parse; no blocking with a browser User-Agent. | **Pass** |
| S6 | Telegram approval round-trip | Tap → callback → DB commit in **13 ms**, button acknowledged 734 ms after the callback arrived, ≈ **1.3 s** from tap to acknowledgement including long-poll delivery. Each Telegram API call takes ~0.57 s from the home network. | **Pass** |

## Findings that affect the design

**Questrade**
- **Candles include extended hours.** Intraday candles cover 04:00–20:00 ET, not just 09:30–16:00. Strategy code must filter to regular hours explicitly.
- **Replay can only reach back about 3 months** unless Trader stores candles itself. The API returned intraday data from 2026-06-26 onward and nothing earlier. To replay further back later, the nightly job has to archive bars as it goes. (Decision needed; see "Open decisions".)
- **Odd request-window rule.** A window that starts before 2026-06-26 still works if it ends inside the available range (the data is trimmed), but some windows that end just before the cutoff return HTTP 400 `code 1002`. The candle client should clamp start times to the available range, not rely on errors.
- **Symbol names differ from FinViz** for share classes: FinViz `BF-B` is Questrade `BF.B`. Map `-` to `.` before lookup. 694 of 695 resolved.
- `GET /v1/symbols?names=` accepts 100 names per call (all 695 resolved in 1.1 s). `symbols/search?prefix=` also works.
- Quotes have both `lastTradePrice` and `lastTradePriceTrHrs` (regular-hours only), plus `VWAP`, `delay`, `isHalted` and `tier`. Whether `lastTradePrice` carries the pre-market price is checked with S2 on Monday.
- Bid/ask come back `null` when the market is closed.
- **Rate limits.** The `X-RateLimit-Remaining` counters (account 30,000/h, market data 15,000/h) carried over between new access tokens, so they're per app or per login, not per token. They were at their maximum before the test, so FinanceTracker wasn't using any this hour; whether the two apps share one budget is still unknown.

**FinanceTracker independence (S1 follow-up).** FinanceTracker's own Questrade status couldn't be read (its production database is off-limits to this session). FinanceTracker refreshes its token daily at 03:30. If its connection still works on Sunday 27 Sep after 03:30, the two token chains are independent.

**FinViz** (parser: `finviz_spike.py`)
- **Universe is 695 tickers, of which 153 are ETFs** (industry "Exchange Traded Fund"). Decide whether ETFs belong in the universe.
- **A browser User-Agent is required.** The default `python-requests` agent gets HTTP 403, and an empty one gets a silent empty page.
- **One request can fetch all needed fields** using a custom view: `v=152&c=1,2,3,4,6,49,63,64,65,66,67,68` (ticker, company, sector, industry, market cap, ATR, avg volume, rel volume, price, change %, volume, earnings). Not yet tested as one URL.
- Filter codes that work: `earningsdate_today`, `earningsdate_nextweek`, `news_date_today`, `news_date_sinceyesterday`, signal `s=n_majornews`.
- **FinViz silently ignores unknown filter codes** and returns the whole universe. The scraper must treat "result count equals the unfiltered count" as an error.
- News dates: the first row of each day has `Sep-25-26 04:18PM` (or `Today 06:07AM`), later rows only the time. Carry the date forward. Times are US Eastern.
- Quote-page news includes stories mainly about other companies (for example an MRVL story on AMD's page). Claude's catalyst classification should expect that.
- Parsing takes ~48 s for 35 pages at 1 request per second.

**Telegram**
- The first S6 attempt's `answerCallbackQuery` returned HTTP 400 (the reason wasn't captured). The rerun, without an emoji in the toast text, worked. The bot should log Telegram's error body and never let a failed acknowledgement block the decision itself, which is already saved by then.
- Each Telegram call costs ~0.57 s, so the worker should acknowledge the button first and edit the message afterwards.

## Open decisions

1. **Archive intraday candles nightly?** Needed if replay should ever cover more than the most recent ~3 months. Five-minute bars for ~700 symbols are about 55,000 rows a day (~14 million a year); one-minute bars are about 5 times that.
2. **Keep or drop ETFs** (153 of 695) in the universe.
