# Opening bar from quotes (QUOTEBAR, decided Tue 2026-09-29)

Approved by Stephen on Tue 2026-09-29; deployed with FIX-401. SPEC §4.1 (market-data package), §5.2 step 1, §8,
§9, §10; master plan §7.1 row "Market data".

## Why

Stephen's Questrade market-data package serves **intraday candles about 10 minutes late**: a candle that
ended less than ~10 minutes ago is refused with HTTP 401, Questrade code 1022 ("...current market data
package..."). **Quotes are real time** (`delay: 0`). So at 09:35:05 the 09:30-09:35 bar cannot be fetched as a
candle (Mon 09-28 and Tue 09-29: 0 opening bars, no trades; see `docs/build/agents/DEBUG-401-a1.md`).

Probe (orchestrator, Tue 09-29, SPY and SMMT quotes against the day's candles):

- quote `openPrice` == the first regular 5-minute candle's open (exact);
- `highPrice`/`lowPrice` are regular-session extremes (premarket excluded), occasionally a little off the
  candles (SMMT high 19.09 vs 19.00);
- quote `volume` is consolidated, about 1.3-1.5x the sum of Questrade's candle volumes (SPY 36.85M vs
  24.75M), and excludes premarket.

## Decision

1. **The live 9:35 scan builds the opening bar from ONE batched quotes pass** (`MarketDataService` with
   `opening_bar_source="quotes"`, set by `build_engine` only): at most 100 ids per request (~6 requests for
   ~550 names), through the same paced client; FIX-401's fail fast still applies (after a 401 the remaining
   requests are not sent). Bar: open = `openPrice`, high = `highPrice`, low = `lowPrice` (widened to contain
   open and close), close = `lastTradePriceTrHrs` (else `lastTradePrice`), start 09:30, end 09:35 (complete at
   09:35:05). Missing: `no_quote` (no quote returned), `no_trade` (no open, no volume, or the last trade
   before 09:30), `quote_incomplete`, `quote_delayed`.
2. **No 09:30 snapshot.** The evidence says quote volume excludes premarket, so the 09:35:05 quote volume is
   the session's volume so far; a snapshot at 09:30 would only subtract the opening print it should keep. The
   five seconds after 09:35:00 leak into high/low/close/volume; that is small and the shadow check measures it.
   Quotes are only used within 5 minutes after the bar's end (`QUOTE_BAR_MAX_LAG`): later, the quote's
   high/low are no longer the bar's, and the candle path is used as before.
3. **Volume on candle scale.** rvol compares the bar's volume with `open_bar_stats.avg_open_vol_14d`, built
   from candles. The quote volume is multiplied by a per-symbol factor = regular-session candle volume /
   quote day volume, **measured by the post-close job of the previous session** (`quote_volume_scale`, one
   quotes pass after the close plus that day's 5-minute candles; a stale quote or a day without its last
   5-minute candle is not measured). Fallbacks: the median of that session's usable factors (0.05-2), then
   1/1.4 = 0.7143. The source used (`symbol`/`median`/`default`) is stored per bar and counted in the job
   detail (`volume_factors`). Considered instead: storing quote-based opening volumes and building a
   quote-scale baseline. Rejected for now: it needs 14 sessions of quote history before it works, and it had
   to work on Wed 09-30. The shadow check's stored pairs (quote volume, official volume) make an
   opening-specific factor possible later.
4. **Candles stay the fallback and the replay source.** Symbols whose quote request failed (error, timeout)
   fall back to the candle path in the same call (`source: quotes+candles`). Replay and golden runs use stored
   candles, unchanged (the service's default is still `candles`).
5. **Quote bars are never candles.** They go to `opening_bar_quotes` (migration 0009), never to
   `intraday_candles` or `candle_archive`; the post-close archive fetches the official candles as before. The
   source is recorded as `quotes` in the orb_open job detail (`source`, `volume_factors`, `quote_lag_s`), in
   the candidates' data and the decision log's scan rows (`bar_source`). The decision log explains the scan
   with the bars it used (`opening_bar_quotes` first).
6. **Shadow check** (`trader openbar-check`, cron 09:47 ET, retried until 10:30 while the candle is still
   refused): for at most 100 quote-built bars (every name that passed or entered first, then the ranked
   candidates, then the largest volumes) fetch the official 09:30-09:35 candle (cached: it is official),
   store it beside the quote bar with `check_status` and `decision_differs` (orb_sip's bar-level screen:
   rvol, candle shape, price band). The post-close summary gets one line: "Opening bars from quotes: N
   compared, prices exact X%, volume within ±10% Y%" (plus "K decisions would differ"). Prices exact =
   open, high and low equal (the close is read 5 s later by design).
7. **1022 is not a token failure.** The client raises a 401 with code 1022 at once, without a forced token
   refresh (a refresh would only rotate the refresh token).

## Deploy notes

- Migration 0009 (two new tables, additive).
- Backfill tonight so Wed 09-30 uses measured factors: `trader volume-scale --date 2026-09-29` after the
  deploy (before the next open). Without it the 9:35 scan uses 1/1.4 and says so (`volume_factors:
  {"default": N}`).
- New cron line `47 9 * * 1-5 trader openbar-check`.
