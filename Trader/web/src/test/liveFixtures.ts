// Fixtures for the live dashboard (`GET /api/live`) and the Control page (`GET /api/control`) (DB-T1).
// The session day is fixtures.ts's (2026-10-06, a Tuesday; the server's "now" is 10:00 ET / 08:00 MT).
// Every builder returns a fresh deep copy, so a test can change what it gets without touching the fixtures.
import type {
  ActivityItemOut,
  BooksCheckOut,
  ClaudeTodayOut,
  ControlOut,
  EngineOut,
  EquityPointLiveOut,
  EquitySeriesOut,
  FillMarkerOut,
  HealthPanelOut,
  KillSwitchLightOut,
  LiveOut,
  LivePositionOut,
  PartErrorOut,
  PeriodPnlOut,
  RejectionsOut,
  RiskOut,
  ScheduleItemOut,
  SoakSummaryOut,
  SparkPointOut,
  StrategyCardOut,
  WorkerOut,
} from "../api/types";
import { MANUAL_JOBS } from "../api/types";
import {
  RUN_ID,
  SERVER_TIME,
  SESSION_DATE,
  errorEvents,
  killswitchEvents,
  notificationsFailed,
  timeline,
  tokenOut,
  workerOut,
} from "./fixtures";

/** The live run's start (Tuesday 2026-09-29, 09:00 ET). */
export const RUN_STARTED_AT = "2026-09-29T13:00:00Z";
export const RUN_START_DATE = "2026-09-29";
/** A Saturday: not a session; the dashboard shows Friday 2026-10-09. */
export const EMPTY_DAY_SERVER_TIME = "2026-10-10T15:00:00Z";
export const XSS = "<img src=x onerror=alert(1)>";

function clone<T>(value: T): T {
  return structuredClone(value);
}

function iso(ms: number): string {
  return new Date(ms).toISOString().replace(".000Z", "Z");
}

function fmt4(n: number): string {
  return n.toFixed(4);
}

const T_OPEN = Date.parse("2026-10-06T13:30:00Z");
const MINUTE = 60_000;

// ---------------------------------------------------------------- positions

const AAA_ENTRY = 21.56;
const AAA_QTY = 30;
const AAA_MARK = 21.96;

/** AAA's price path from 09:37 to 10:00 ET (one point a minute), ending at the mark. */
function aaaPrice(i: number, n: number): number {
  return AAA_ENTRY + ((AAA_MARK - AAA_ENTRY) * i) / (n - 1) + (i % 3 === 1 ? 0.03 : i % 3 === 2 ? -0.02 : 0);
}

const aaaSpark: SparkPointOut[] = Array.from({ length: 24 }, (_, i) => ({
  ts: iso(T_OPEN + (7 + i) * MINUTE),
  price: fmt4(i === 23 ? AAA_MARK : aaaPrice(i, 24)),
}));

export const aaaEntryFill: FillMarkerOut = {
  fill_id: 51,
  ts: "2026-10-06T13:36:15Z",
  ticker: "AAA",
  side: "buy",
  purpose: "entry",
  qty: AAA_QTY,
  price: "21.5600",
  position_id: 7,
};

/** The one open position of `liveOut`: AAA long 30 with a live mark, 1.69 R above its stop. */
export const livePosition: LivePositionOut = {
  id: 7,
  symbol_id: 101,
  ticker: "AAA",
  strategy_key: "orb_sip",
  side: "long",
  qty: AAA_QTY,
  entry: "21.5600",
  mark: "21.9600",
  bid: "21.9500",
  ask: "21.9700",
  mark_at: "2026-10-06T13:59:58Z",
  mark_state: "live",
  stop: "20.9800",
  stop_working: true,
  target: null,
  planned_risk: "17.4000",
  unrealized: "12.0000",
  unrealized_r: "0.6897",
  distance_to_stop_r: "1.6897",
  near_stop: false,
  opened_at: "2026-10-06T13:36:15Z",
  held_seconds: 1425,
  unprotected_seconds: 0,
  spark: aaaSpark,
  bars: null,
  fills: [aaaEntryFill],
  link: "/trades?position=7",
};

const TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK", "LLL", "MMM", "NNN", "OOO", "PPP", "QQQ", "RRR", "SSS", "TTT"];

/**
 * `n` open positions (0, 1 and 20 are the plan's cases) with distinct ids, tickers and unrealised P&L.
 * `staleMarks`: every mark is 95 s old (`stale`) and the last one is `missing`; `nearStop`: every position is
 * 0.2 R above its stop (`near_stop`).
 */
export function positionsN(n: number, opts: { staleMarks?: boolean; nearStop?: boolean } = {}): LivePositionOut[] {
  return Array.from({ length: n }, (_, i) => {
    const entry = 10 + i * 2.5;
    const risk = 0.5 + (i % 4) * 0.1;
    const stop = entry - risk;
    const qty = 10 + i * 5;
    const rMove = opts.nearStop ? -0.8 : ((i * 7) % 11) / 5 - 1; // between -1 R and +1 R
    const mark = entry + rMove * risk;
    const missing = opts.staleMarks === true && i === n - 1;
    const unrealized = (mark - entry) * qty;
    const ticker = TICKERS[i % TICKERS.length] + (i >= TICKERS.length ? String(i) : "");
    const opened = T_OPEN + (6 + i) * MINUTE;
    return {
      ...clone(livePosition),
      id: 100 + i,
      symbol_id: 200 + i,
      ticker,
      qty,
      entry: fmt4(entry),
      mark: missing ? null : fmt4(mark),
      bid: missing ? null : fmt4(mark - 0.01),
      ask: missing ? null : fmt4(mark + 0.01),
      mark_at: missing ? null : opts.staleMarks ? "2026-10-06T13:58:25Z" : "2026-10-06T13:59:58Z",
      mark_state: missing ? "missing" : opts.staleMarks ? "stale" : "live",
      stop: fmt4(stop),
      planned_risk: fmt4(risk * qty),
      unrealized: missing ? null : fmt4(unrealized),
      unrealized_r: missing ? null : fmt4(rMove),
      distance_to_stop_r: missing ? null : fmt4((mark - stop) / risk),
      near_stop: !missing && (mark - stop) / risk <= 0.25,
      opened_at: iso(opened),
      held_seconds: Math.round((Date.parse(SERVER_TIME) - opened) / 1000),
      spark: missing ? [] : [{ ts: iso(opened), price: fmt4(entry) }, { ts: "2026-10-06T13:59:00Z", price: fmt4(mark) }],
      fills: [{ ...aaaEntryFill, fill_id: 300 + i, ts: iso(opened), ticker, qty, price: fmt4(entry), position_id: 100 + i }],
      link: `/trades?position=${100 + i}`,
    };
  });
}

// ---------------------------------------------------------------- periods, costs, books

export const periods: PeriodPnlOut[] = [
  {
    period: "today",
    date_from: SESSION_DATE,
    date_to: SESSION_DATE,
    realized: "0.0000",
    unrealized: "11.0000",
    unrealized_partial: false,
    pnl_after_fees: "11.0000",
    fees: "1.0000",
    claude_usd: "0.1200",
    net_after_ai: "10.8800",
    trades: 0,
    wins: 0,
    losses: 0,
    win_rate: null,
    expectancy_r: null,
    trades_without_r: 0,
  },
  {
    period: "week",
    date_from: "2026-10-05",
    date_to: SESSION_DATE,
    realized: "8.7250",
    unrealized: "11.0000",
    unrealized_partial: false,
    pnl_after_fees: "19.7250",
    fees: "3.0000",
    claude_usd: "0.2400",
    net_after_ai: "19.4850",
    trades: 1,
    wins: 1,
    losses: 0,
    win_rate: "1.0000",
    expectancy_r: "0.7091",
    trades_without_r: 0,
  },
  {
    period: "run",
    date_from: RUN_START_DATE,
    date_to: SESSION_DATE,
    realized: "8.7250",
    unrealized: "11.0000",
    unrealized_partial: false,
    pnl_after_fees: "19.7250",
    fees: "3.0000",
    claude_usd: "0.9600",
    net_after_ai: "18.7650",
    trades: 1,
    wins: 1,
    losses: 0,
    win_rate: "1.0000",
    expectancy_r: "0.7091",
    trades_without_r: 0,
  },
];

export const claudeToday: ClaudeTodayOut = {
  date: SESSION_DATE,
  spent_usd: "0.1200",
  cap_usd: "1.0000",
  used_fraction: "0.1200",
};

/** cash 110.925 + AAA at cost 646.80 = 750 + 10.725 realised − 3.00 fees. */
export const booksOk: BooksCheckOut = {
  ok: true,
  cash: "110.9250",
  positions_at_cost: "646.8000",
  actual: "757.7250",
  starting_cash: "750.0000",
  realized_gross: "10.7250",
  fees_paid: "3.0000",
  expected: "757.7250",
  difference: "0.0000",
  realized_recorded: "8.7250",
  open_positions: 1,
};

/** A missing fee row: the ledger holds 1.00 more cash than the trades and fills explain. */
export const booksBroken: BooksCheckOut = {
  ...booksOk,
  ok: false,
  cash: "111.9250",
  actual: "758.7250",
  difference: "1.0000",
};

// ---------------------------------------------------------------- equity

/** 30 points of `session_day`: two snapshots, 27 minute points from marks and the final `now` point. */
const equityPoints: EquityPointLiveOut[] = Array.from({ length: 30 }, (_, i) => {
  if (i === 29) return { ts: SERVER_TIME, equity: "769.7250", source: "now" };
  if (i < 5) return { ts: iso(T_OPEN + (1 + i) * MINUTE), equity: "758.7250", source: i === 0 ? "snapshot" : "marks" };
  if (i === 5) return { ts: "2026-10-06T13:36:15Z", equity: "757.7250", source: "snapshot" };
  const price = aaaPrice(i - 5, 24);
  return { ts: iso(T_OPEN + (1 + i) * MINUTE), equity: fmt4(757.725 + (price - AAA_ENTRY) * AAA_QTY), source: "marks" };
});

export const equityToday: EquitySeriesOut = {
  range: "today",
  start_equity: "758.7250",
  points: equityPoints,
  fills: [aaaEntryFill],
  downsampled: false,
};

export const equityRun: EquitySeriesOut = {
  range: "run",
  start_equity: "750.0000",
  points: [
    { ts: RUN_STARTED_AT, equity: "750.0000", source: "snapshot" },
    { ts: "2026-10-05T13:36:40Z", equity: "749.0000", source: "snapshot" },
    { ts: "2026-10-05T19:50:00Z", equity: "758.7250", source: "snapshot" },
    { ts: "2026-10-06T13:36:15Z", equity: "757.7250", source: "snapshot" },
    { ts: SERVER_TIME, equity: "769.7250", source: "now" },
  ],
  fills: [
    { fill_id: 40, ts: "2026-10-05T13:36:40Z", ticker: "BBB", side: "buy", purpose: "entry", qty: 30, price: "14.2100", position_id: 3 },
    { fill_id: 41, ts: "2026-10-05T19:50:00Z", ticker: "BBB", side: "sell", purpose: "exit", qty: 30, price: "14.5675", position_id: 3 },
    aaaEntryFill,
  ],
  downsampled: false,
};

// ---------------------------------------------------------------- risk

export const killswitchLights: KillSwitchLightOut[] = [
  {
    switch: "daily_loss_pct",
    label: "Daily loss",
    tripped: false,
    tripped_at: null,
    value: "0.0000",
    threshold: "0.0500",
    unit: "pct",
    count: null,
    count_min: null,
    trip_value: null,
    trip_threshold: null,
    automatic: true,
    needs_web_reset: false,
    clears: "clears at the next session",
  },
  {
    switch: "max_drawdown_pct",
    label: "Max drawdown",
    tripped: false,
    tripped_at: null,
    value: "0.0013",
    threshold: "0.2000",
    unit: "pct",
    count: null,
    count_min: null,
    trip_value: null,
    trip_threshold: null,
    automatic: true,
    needs_web_reset: true,
    clears: "reset it on the Control page with a reason",
  },
  {
    switch: "expectancy",
    label: "Expectancy",
    tripped: false,
    tripped_at: null,
    value: "0.7091",
    threshold: "0.0000",
    unit: "r",
    count: 1,
    count_min: 20,
    trip_value: null,
    trip_threshold: null,
    automatic: true,
    needs_web_reset: true,
    clears: "reset it on the Control page with a reason",
  },
  {
    switch: "manual_pause",
    label: "Paused",
    tripped: false,
    tripped_at: null,
    value: null,
    threshold: null,
    unit: "none",
    count: null,
    count_min: null,
    trip_value: null,
    trip_threshold: null,
    automatic: false,
    needs_web_reset: false,
    clears: "resume on the Control page or with /resume",
  },
];

export const riskOut: RiskOut = {
  killswitches: killswitchLights,
  equity: "769.7250",
  open_risk: "17.4000",
  open_risk_cap: "37.5000",
  slots_used: 1,
  slots_max: 3,
  open_positions: 1,
};

// ---------------------------------------------------------------- activity and rejections

/** 12 items of the day across every chip, newest first. */
export const activity: ActivityItemOut[] = [
  { id: "event_log:207", ts: "2026-10-06T13:45:00Z", kind: "alert", chip: "alerts", ticker: null, text: "market.data_service: HTTP 429 from Questrade, paused 1.2 s", amount: null, tone: "warn", link: "/control" },
  { id: "proposal:14:expired", ts: "2026-10-06T13:41:20Z", kind: "proposal_expired", chip: "proposals", ticker: "FFF", text: "Expired entry FFF", amount: null, tone: "warn", link: "/dashboard?proposal=14" },
  { id: "order:23:cancelled", ts: "2026-10-06T13:41:20Z", kind: "order_cancelled", chip: "trades", ticker: "FFF", text: "Cancelled entry FFF: proposal expired", amount: null, tone: "neutral", link: null },
  { id: "proposal:14", ts: "2026-10-06T13:36:20Z", kind: "proposal_created", chip: "proposals", ticker: "FFF", text: "Proposal entry FFF 40 sh (manual)", amount: null, tone: "neutral", link: "/dashboard?proposal=14" },
  { id: "order:22", ts: "2026-10-06T13:36:15Z", kind: "order_placed", chip: "trades", ticker: "AAA", text: "Sell stop 30 AAA @ 20.98 (stop)", amount: null, tone: "neutral", link: "/trades?position=7" },
  { id: "fill:51", ts: "2026-10-06T13:36:15Z", kind: "fill", chip: "trades", ticker: "AAA", text: "Filled buy 30 AAA @ 21.56, slippage 0.00/share", amount: null, tone: "neutral", link: "/trades?position=7" },
  { id: "order:21", ts: "2026-10-06T13:36:12Z", kind: "order_placed", chip: "trades", ticker: "AAA", text: "Buy stop 30 AAA @ 21.56 (entry)", amount: null, tone: "neutral", link: "/trades?position=7" },
  { id: "proposal:12:decided", ts: "2026-10-06T13:36:12Z", kind: "proposal_approved", chip: "proposals", ticker: "AAA", text: "Approved entry AAA via telegram at 07:36 MT", amount: null, tone: "neutral", link: "/dashboard?proposal=12" },
  { id: "proposal:12", ts: "2026-10-06T13:36:10Z", kind: "proposal_created", chip: "proposals", ticker: "AAA", text: "Proposal entry AAA 30 sh (manual)", amount: null, tone: "neutral", link: "/dashboard?proposal=12" },
  { id: "scan:2026-10-06", ts: "2026-10-06T13:35:05Z", kind: "scan", chip: "scan", ticker: null, text: "09:35 scan: 543 → 12 passed → AAA, FFF, GGG", amount: null, tone: "neutral", link: "/reports?day=2026-10-06" },
  { id: "job_run:302", ts: "2026-10-06T12:01:00Z", kind: "job_failed", chip: "alerts", ticker: null, text: "premarket failed (attempt 1): Claude request timed out", amount: null, tone: "warn", link: "/control" },
  { id: "kill_switch:4:reset", ts: "2026-10-06T12:00:00Z", kind: "kill_switch_reset", chip: "alerts", ticker: null, text: "Kill switch daily loss reset by system: new session", amount: null, tone: "neutral", link: "/control" },
];

/** An exit with its money outcome (not in `liveOut`, whose only position is still open). */
export const exitActivity: ActivityItemOut = {
  id: "trade:9",
  ts: "2026-10-06T13:50:00Z",
  kind: "exit",
  chip: "trades",
  ticker: "HHH",
  text: "Exit HHH (stop): -5.80, -1.00 R",
  amount: "-5.8000",
  tone: "down",
  link: "/trades?position=9",
};

function tickerList(n: number, prefix: string): string[] {
  return Array.from({ length: n }, (_, i) => `${prefix}${String(i).padStart(2, "0")}`);
}

export const rejections: RejectionsOut = {
  session_date: SESSION_DATE,
  source: "decision_log",
  total: 531,
  rules: [
    {
      stage: "scan",
      rule: "rvol_below_min",
      count: 498,
      tickers: tickerList(50, "RV"),
      truncated: true,
      link: "/reports?day=2026-10-06&stage=scan&outcome=rejected",
    },
    {
      stage: "scan",
      rule: "gap_below_min",
      count: 33,
      tickers: tickerList(33, "GP"),
      truncated: false,
      link: "/reports?day=2026-10-06&stage=scan&outcome=rejected",
    },
  ],
  final: false,
  recorded_at: "2026-10-06T13:59:10Z",
};

// ---------------------------------------------------------------- the aggregate

const liveWorker: WorkerOut = {
  ...workerOut,
  detail: {
    rate_limit: { market: 18, account: 29 },
    marks: { written_at: "2026-10-06T13:59:58Z", symbols: 1, failing: false },
  },
};

const LIVE_OUT: LiveOut = {
  server_time: SERVER_TIME,
  run_id: RUN_ID,
  run_started_at: RUN_STARTED_AT,
  session: {
    date: SESSION_DATE,
    phase: "open",
    is_session: true,
    open_at: "2026-10-06T13:30:00Z",
    close_at: "2026-10-06T20:00:00Z",
  },
  session_day: SESSION_DATE,
  approval_mode: "manual",
  trading: "running",
  telegram_configured: true,
  worker: liveWorker,
  worker_stale: false,
  marks_stale_seconds: 30,
  closed_today: 0,
  periods,
  claude_today: claudeToday,
  books: booksOk,
  equity: equityToday,
  risk: riskOut,
  positions: [livePosition],
  activity,
  rejections,
  timeline,
  pending: [],
  part_errors: [],
};

/** A session day at 10:00 ET: 1 open position with a live mark, 3 periods, books ok, 30 equity points,
 * 12 activity items across all chips, 2 rejection rules, the day's timeline and no pending proposal. */
export const liveOut: LiveOut = clone(LIVE_OUT);

/** `liveOut` with some fields replaced (a fresh copy). */
export function liveWith(overrides: Partial<LiveOut>): LiveOut {
  return { ...clone(LIVE_OUT), ...clone(overrides) };
}

/** Saturday 2026-10-10: not a session (the day shown is Friday), no positions, activity or rejections. */
export const liveEmptyDay: LiveOut = liveWith({
  server_time: EMPTY_DAY_SERVER_TIME,
  session: { date: "2026-10-10", phase: "closed_day", is_session: false, open_at: null, close_at: null },
  session_day: "2026-10-09",
  worker: { ...workerOut, phase: "idle", beat_at: "2026-10-10T14:59:57Z", age_seconds: 3, session_date: null },
  closed_today: 0,
  periods: periods.map((p) =>
    p.period === "today"
      ? { ...p, date_from: "2026-10-09", date_to: "2026-10-09", unrealized: null, pnl_after_fees: "0.0000", fees: "0.0000", claude_usd: "0.0000", net_after_ai: "0.0000" }
      : { ...p, unrealized: null, pnl_after_fees: p.realized, date_to: "2026-10-10" },
  ),
  claude_today: { date: "2026-10-10", spent_usd: "0.0000", cap_usd: "1.0000", used_fraction: "0.0000" },
  books: { ...booksOk, cash: "758.7250", positions_at_cost: "0.0000", actual: "758.7250", fees_paid: "2.0000", expected: "758.7250", open_positions: 0 },
  equity: { range: "today", start_equity: "758.7250", points: [], fills: [], downsampled: false },
  risk: { ...riskOut, equity: "758.7250", open_risk: "0.0000", slots_used: 0, open_positions: 0 },
  positions: [],
  activity: [],
  rejections: { session_date: "2026-10-09", source: "none", total: 0, rules: [], final: true, recorded_at: null },
  timeline: [],
  pending: [],
});

/** The parts of `LiveOut` that can fail on their own (S15). */
export const LIVE_PARTS = [
  "periods",
  "claude_today",
  "books",
  "equity",
  "risk",
  "positions",
  "activity",
  "rejections",
  "timeline",
  "pending",
] as const satisfies readonly (keyof LiveOut)[];

function partErrors(parts: readonly string[]): PartErrorOut[] {
  return parts.map((part) => ({ part, message: `OperationalError: ${part} could not be read` }));
}

/** Every part null, each with a `part_errors` entry: the page frame still renders. */
export const liveAllPartsFailed: LiveOut = liveWith({
  periods: null,
  claude_today: null,
  books: null,
  equity: null,
  risk: null,
  positions: null,
  activity: null,
  rejections: null,
  timeline: null,
  pending: null,
  part_errors: partErrors(LIVE_PARTS),
});

// ---------------------------------------------------------------- control

export const engineOut: EngineOut = {
  approval_mode: "manual",
  trading: "running",
  paused_at: null,
  run_id: RUN_ID,
  run_started_at: RUN_STARTED_AT,
  run_start_date: RUN_START_DATE,
  version: "phase-5-complete-24-g459e172",
  app_env: "dev",
  alembic_revision: "0008",
};

export const strategyCards: StrategyCardOut[] = [
  {
    key: "orb_sip",
    kind: "entry",
    enabled: true,
    revision: 3,
    version: "1.0.0",
    updated_at: "2026-09-29T12:00:00Z",
    updated_by: "web:stephen",
    owns_open_positions: true,
    max_positions: 3,
    settings_link: "/settings#strategy-orb_sip",
  },
  {
    key: "spy_overlay",
    kind: "overlay",
    enabled: true,
    revision: 1,
    version: "1.0.0",
    updated_at: "2026-09-29T12:00:00Z",
    updated_by: null,
    owns_open_positions: false,
    max_positions: null,
    settings_link: "/settings#strategy-spy_overlay",
  },
];

export const schedule: ScheduleItemOut[] = [
  { key: "nightly", label: "Nightly universe", kind: "job", at: "2026-10-06T00:30:00Z", status: "done", detail: null, started_at: "2026-10-06T00:30:00Z", finished_at: "2026-10-06T00:34:12Z", duration_seconds: 252, attempts: 1, summary: "412 symbols from finviz", rerun: "nightly" },
  { key: "premarket", label: "Pre-market scan", kind: "job", at: "2026-10-06T12:00:00Z", status: "done", detail: null, started_at: "2026-10-06T12:00:00Z", finished_at: "2026-10-06T12:01:41Z", duration_seconds: 101, attempts: 2, summary: "8 catalysts classified", rerun: "premarket" },
  { key: "preopen", label: "Pre-open check", kind: "job", at: "2026-10-06T13:20:00Z", status: "done", detail: null, started_at: "2026-10-06T13:20:00Z", finished_at: "2026-10-06T13:20:03Z", duration_seconds: 3, attempts: 1, summary: null, rerun: "preopen" },
  { key: "orb_open", label: "ORB entry (orb_open)", kind: "event", at: "2026-10-06T13:35:05Z", status: "done", detail: null, started_at: null, finished_at: null, duration_seconds: null, attempts: 0, summary: "543 bars in 31 s", rerun: null },
  { key: "entry_cancel", label: "Cancel unfilled entries", kind: "event", at: "2026-10-06T15:30:00Z", status: "next", detail: null, started_at: null, finished_at: null, duration_seconds: null, attempts: 0, summary: null, rerun: null },
  { key: "flatten", label: "Flatten", kind: "event", at: "2026-10-06T19:50:00Z", status: "upcoming", detail: null, started_at: null, finished_at: null, duration_seconds: null, attempts: 0, summary: null, rerun: null },
  { key: "postclose", label: "Post-close", kind: "job", at: "2026-10-06T20:15:00Z", status: "upcoming", detail: null, started_at: null, finished_at: null, duration_seconds: null, attempts: 0, summary: null, rerun: "postclose" },
];

export const healthPanel: HealthPanelOut = {
  worker: liveWorker,
  worker_stale: false,
  token: tokenOut,
  db_ok: true,
  db_latency_ms: 3,
  telegram_configured: true,
  questrade: {
    day: SESSION_DATE,
    since: "2026-10-06T04:00:00Z",
    market: { requests: 1204, http_429: 1, pause_s: 1.2, http_5xx: 0, transport_errors: 0 },
    account: { requests: 96, http_429: 0, pause_s: 0, http_5xx: 0, transport_errors: 0 },
    rate_limit: { market_remaining: 18, account_remaining: 29 },
  },
  opening_bars: {
    session_date: SESSION_DATE,
    started_at: "2026-10-06T13:35:00Z",
    symbols: 543,
    completed: 543,
    errors: 0,
    outstanding: 0,
    elapsed_s: 31.2,
    deadline_s: 45,
    http_429: 0,
    pause_s: 0,
    complete: true,
    raised: null,
  },
  marks: { written_at: "2026-10-06T13:59:58Z", symbols: 1, failing: false },
  notifications_failed: notificationsFailed,
  tz_iana_version: "2026c",
};

export const soakSummary: SoakSummaryOut = {
  target: 10,
  consecutive_clean: 3,
  total_clean: 3,
  day_one: "2026-09-29",
  earliest_finish: "2026-10-12",
  last_final: "2026-10-05",
  today: { session_date: SESSION_DATE, verdict: "clean so far", failed: [], provisional: true },
  generated_at: SERVER_TIME,
};

const CONTROL_OUT: ControlOut = {
  server_time: SERVER_TIME,
  session: LIVE_OUT.session,
  manual_jobs: [...MANUAL_JOBS],
  engine: engineOut,
  killswitches: killswitchLights,
  killswitch_history: killswitchEvents,
  strategies: strategyCards,
  schedule,
  health: healthPanel,
  soak: soakSummary,
  errors: [
    { id: 207, ts: "2026-10-06T13:45:00Z", level: "warning", source: "market.data_service", message: "HTTP 429 from Questrade, paused 1.2 s", data: null },
    ...errorEvents,
  ],
  part_errors: [],
};

/** The Control page on the same session day: every part present. */
export const controlOut: ControlOut = clone(CONTROL_OUT);

export function controlWith(overrides: Partial<ControlOut>): ControlOut {
  return { ...clone(CONTROL_OUT), ...clone(overrides) };
}

/** The worker's last heartbeat is 95 s old (over the 60 s badge and the stale threshold). */
export const controlStaleWorker: ControlOut = (() => {
  const worker: WorkerOut = { ...liveWorker, ok: false, beat_at: "2026-10-06T13:58:25Z", age_seconds: 95 };
  return controlWith({ health: { ...healthPanel, worker, worker_stale: true } });
})();

/** No soak day recorded yet (no day one, nothing today). */
export const controlNoSoak: ControlOut = controlWith({
  soak: {
    target: 10,
    consecutive_clean: 0,
    total_clean: 0,
    day_one: null,
    earliest_finish: null,
    last_final: null,
    today: null,
    generated_at: SERVER_TIME,
  },
});

// ---------------------------------------------------------------- XSS variants

// Free-text fields (reasons, messages, labels, tickers, names): each gets `XSS` appended. Typed literals
// (`kind`, `status`, `source` of a series, ...) are left alone; `source` changes only on events (`level` set).
const FREE_TEXT = new Set([
  "ticker",
  "tickers",
  "text",
  "rule",
  "message",
  "label",
  "detail",
  "summary",
  "reason",
  "error",
  "clears",
  "strategy_key",
  "verdict",
  "failed",
  "reset_reason",
  "reset_by",
  "raised",
  "host",
  "version",
  "decided_by",
  "updated_by",
  "brief",
]);

function xssify(value: unknown, key: string | null, parent: Record<string, unknown> | null): unknown {
  const free = key !== null && (FREE_TEXT.has(key) || (key === "source" && parent !== null && "level" in parent));
  if (typeof value === "string") return free ? `${value}${XSS}` : value;
  if (Array.isArray(value)) return value.map((v) => xssify(v, key, parent));
  if (value !== null && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, xssify(v, k, obj)]));
  }
  return value;
}

/** `liveOut` (with an exit item) and `controlOut` with `XSS` in every free-text field, for the XSS tests. */
export function withXssText(): { live: LiveOut; control: ControlOut } {
  const live = liveWith({ activity: [exitActivity, ...activity] });
  return { live: xssify(live, null, null) as LiveOut, control: xssify(clone(CONTROL_OUT), null, null) as ControlOut };
}
