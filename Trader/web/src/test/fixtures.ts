// Realistic API responses for web tests: one object per response type. The day is Tuesday 2026-10-06
// (a regular session: 09:30-16:00 ET = 13:30-20:00Z = 07:30-14:00 MT). Money is always a string.
// FakeApiClient hands out deep copies, so tests may modify what they receive; to change what the fake
// returns, pass overrides to `new FakeApiClient({...})` or call `fake.set(method, value)`.
import type {
  CandidateOut,
  CandidatesOut,
  CandleOut,
  CatalystOut,
  DashboardOut,
  DecisionDayOut,
  DecisionDaysOut,
  DecisionOut,
  DecisionRowOut,
  DecisionSummaryOut,
  EquityOut,
  EventOut,
  FieldOut,
  FillOut,
  HealthOut,
  Items,
  JobLaunchOut,
  JobRunOut,
  JournalDayOut,
  KillSwitchEventOut,
  KillSwitchesOut,
  KillSwitchOut,
  MetaOut,
  MetricsOut,
  NotificationOut,
  OkOut,
  OrderOut,
  PnlOut,
  PositionDetailOut,
  PositionOut,
  ProposalOut,
  ReplayOptionsOut,
  ReplayOut,
  ReplaySummaryOut,
  SessionOut,
  SettingOut,
  SettingsOut,
  SignalOut,
  StrategyOut,
  SystemOut,
  TelegramTestOut,
  TimelineItemOut,
  TokenOut,
  TotpSetupOut,
  TradeOut,
  WatchlistOut,
  WatchlistUploadOut,
  WeeklyReportOut,
  WorkerOut,
} from "../api/types";

export const SESSION_DATE = "2026-10-06";
/** The server's "now" in the fixtures: 10:00 ET / 08:00 MT. */
export const SERVER_TIME = "2026-10-06T14:00:00Z";
export const RUN_ID = 1;

// ---------------------------------------------------------------- auth and meta

export const sessionOut: SessionOut = {
  user: { username: "stephen", totp_enabled: false },
  csrf_token: "csrf-test-token",
  expires_at: "2026-11-05T14:00:00Z",
};

export const okOut: OkOut = { ok: true, message: null };

export const totpSetupOut: TotpSetupOut = {
  secret: "JBSWY3DPEHPK3PXP",
  otpauth_uri: "otpauth://totp/Trader:stephen?secret=JBSWY3DPEHPK3PXP&issuer=Trader",
};

export const metaOut: MetaOut = {
  server_time: SERVER_TIME,
  app_env: "dev",
  version: "dev",
  tz_display: "America/Edmonton",
  tz_offset_minutes: -360,
  tz_iana_version: "2026c",
  public_base_url: "https://trader-dev.example.test",
};

export const healthOut: HealthOut = {
  status: "ok",
  time: SERVER_TIME,
  version: "dev",
  db_ok: true,
  db_latency_ms: 3,
  token_ok: true,
  token_age_hours: 2.5,
  worker_ok: true,
  worker_phase: "session",
  worker_age_seconds: 4.2,
};

// ---------------------------------------------------------------- status parts

export const tokenOut: TokenOut = {
  ok: true,
  seeded: true,
  age_hours: 2.5,
  expires_at: "2026-10-06T14:30:00Z",
  last_refresh_at: "2026-10-06T11:30:00Z",
  error: null,
};

export const workerOut: WorkerOut = {
  ok: true,
  phase: "session",
  beat_at: "2026-10-06T13:59:56Z",
  age_seconds: 4.2,
  session_date: SESSION_DATE,
  pid: 42,
  host: "trader-dev",
  detail: { rate_limit: { market: 18, account: 29 } },
};

export const events: EventOut[] = [
  {
    id: 103,
    ts: "2026-10-06T13:36:10Z",
    level: "info",
    source: "proposals",
    message: "Proposal 12 created: BUY 30 AAA stop 21.56",
    data: { proposal_id: 12 },
  },
  {
    id: 102,
    ts: "2026-10-06T13:35:06Z",
    level: "info",
    source: "engine",
    message: "orb_open fired: 3 candidates, 1 intent",
    data: null,
  },
  {
    id: 101,
    ts: "2026-10-06T12:00:40Z",
    level: "warning",
    source: "premarket",
    message: "Claude classified 7 of 8 catalysts (1 timed out)",
    data: { classified: 7, total: 8 },
  },
];

export const errorEvents: EventOut[] = [
  {
    id: 90,
    ts: "2026-10-05T20:16:00Z",
    level: "error",
    source: "postclose",
    message: "Candle archive failed for 1 symbol",
    data: { symbols: ["ZZZ"] },
  },
];

export const killswitchStates: KillSwitchOut[] = [
  {
    switch: "daily_loss_pct",
    label: "Daily loss",
    tripped: false,
    tripped_at: null,
    value: null,
    threshold: null,
    automatic: true,
    needs_web_reset: false,
    clears: "",
  },
  {
    switch: "max_drawdown_pct",
    label: "Max drawdown",
    tripped: false,
    tripped_at: null,
    value: null,
    threshold: null,
    automatic: true,
    needs_web_reset: false,
    clears: "",
  },
  {
    switch: "expectancy",
    label: "Expectancy",
    tripped: false,
    tripped_at: null,
    value: null,
    threshold: null,
    automatic: true,
    needs_web_reset: false,
    clears: "",
  },
  {
    switch: "manual_pause",
    label: "Paused",
    tripped: false,
    tripped_at: null,
    value: null,
    threshold: null,
    automatic: false,
    needs_web_reset: false,
    clears: "",
  },
];

/** `max_drawdown_pct` tripped: needs a typed-reason reset on the web. */
export const killswitchDrawdownTripped: KillSwitchOut = {
  switch: "max_drawdown_pct",
  label: "Max drawdown",
  tripped: true,
  tripped_at: "2026-10-06T15:10:00Z",
  value: "0.2150",
  threshold: "0.2000",
  automatic: true,
  needs_web_reset: true,
  clears: "reset it on the web with a reason (Settings → Kill switches)",
};

export const killswitchEvents: KillSwitchEventOut[] = [
  {
    id: 4,
    switch: "daily_loss_pct",
    session_date: "2026-10-02",
    tripped_at: "2026-10-02T17:20:00Z",
    value: "0.0310",
    threshold: "0.0300",
    reset_at: "2026-10-05T12:00:00Z",
    reset_reason: "new session",
    reset_by: "system",
  },
];

export const killswitchesOut: KillSwitchesOut = { switches: killswitchStates, history: killswitchEvents };

// ---------------------------------------------------------------- trading

/** A pending entry proposal for AAA (expires 5 minutes after creation). */
export const pendingProposal: ProposalOut = {
  id: 12,
  kind: "entry",
  status: "pending",
  ticker: "AAA",
  side: "buy",
  order_type: "stop",
  qty: 30,
  stop: "21.5600",
  limit: null,
  stop_loss: "20.9800",
  risk_usd: "17.4000",
  reason: "ORB breakout above 21.55 with rvol 3.1 and an earnings beat",
  strategy_key: "orb_sip",
  created_at: "2026-10-06T13:36:10Z",
  expires_at: "2026-10-06T14:04:10Z",
  decided_at: null,
  decided_via: null,
  decided_by: null,
  decision_latency_ms: null,
  error: null,
  order_id: null,
  position_id: null,
};

/** The same kind of proposal, approved on the web (it created the entry order of position 3). */
export const decidedProposal: ProposalOut = {
  ...pendingProposal,
  id: 11,
  ticker: "BBB",
  status: "submitted",
  stop: "14.2100",
  stop_loss: "13.8000",
  risk_usd: "12.3000",
  qty: 30,
  reason: "ORB breakout above 14.20 with rvol 2.4",
  created_at: "2026-10-05T13:36:05Z",
  expires_at: "2026-10-05T13:41:05Z",
  decided_at: "2026-10-05T13:36:40Z",
  decided_via: "web",
  decided_by: "web:stephen",
  decision_latency_ms: 35_000,
  order_id: 21,
  position_id: 3,
};

/** The protective stop proposal of position 3 (auto-approved). */
export const stopProposal: ProposalOut = {
  ...decidedProposal,
  id: 13,
  kind: "stop",
  status: "auto_approved",
  side: "sell",
  order_type: "stop",
  stop: "13.8000",
  stop_loss: null,
  risk_usd: null,
  reason: "protective stop for position 3",
  created_at: "2026-10-05T13:37:02Z",
  expires_at: "2026-10-05T13:42:02Z",
  decided_at: "2026-10-05T13:37:02Z",
  decided_via: "auto",
  decided_by: "auto",
  decision_latency_ms: 0,
  order_id: 22,
};

export const decisionOut: DecisionOut = {
  proposal: { ...pendingProposal, status: "submitted", decided_at: "2026-10-06T13:37:00Z", decided_via: "web", decided_by: "web:stephen", decision_latency_ms: 50_000, order_id: 30 },
  already_decided: false,
  blocked: null,
  message: "Approved",
};

export const decisionBlocked: DecisionOut = {
  proposal: { ...pendingProposal, status: "rejected", error: "entry blocked: kill switch manual_pause is tripped", decided_at: "2026-10-06T13:37:00Z", decided_via: "web", decided_by: "web:stephen" },
  already_decided: false,
  blocked: "kill switch manual_pause is tripped",
  message: "Entry blocked: kill switch manual_pause is tripped",
};

/** An open position with a working stop. */
export const openPosition: PositionOut = {
  id: 4,
  symbol_id: 501,
  ticker: "CCC",
  strategy_key: "orb_sip",
  status: "open",
  qty: 25,
  entry: "18.4200",
  last: "18.9000",
  stop: "17.9500",
  stop_working: true,
  unrealized_pnl: "12.0000",
  unprotected_seconds: 1,
  opened_at: "2026-10-06T13:40:12Z",
  closed_at: null,
};

/** An open position whose stop order is not working and whose quote failed. */
export const unprotectedPosition: PositionOut = {
  ...openPosition,
  id: 5,
  symbol_id: 502,
  ticker: "DDD",
  last: null,
  stop: "9.4000",
  stop_working: false,
  unrealized_pnl: null,
  unprotected_seconds: 200,
};

/** Position 3: BBB, closed on 2026-10-05 by the flatten. */
export const closedPosition: PositionOut = {
  id: 3,
  symbol_id: 500,
  ticker: "BBB",
  strategy_key: "orb_sip",
  status: "closed",
  qty: 30,
  entry: "14.2150",
  last: null,
  stop: "13.8000",
  stop_working: false,
  unrealized_pnl: null,
  unprotected_seconds: 1,
  opened_at: "2026-10-05T13:37:01Z",
  closed_at: "2026-10-05T19:50:02Z",
};

export const orders: OrderOut[] = [
  {
    id: 21,
    proposal_id: 11,
    position_id: 3,
    symbol_id: 500,
    ticker: "BBB",
    side: "buy",
    order_type: "stop",
    purpose: "entry",
    qty: 30,
    stop_price: "14.2100",
    limit_price: null,
    stop_loss: "13.8000",
    tif: "day",
    status: "filled",
    reason: "ORB breakout",
    session_date: "2026-10-05",
    submitted_at: "2026-10-05T13:36:40Z",
    closed_at: "2026-10-05T13:37:01Z",
    cancel_reason: null,
  },
  {
    id: 22,
    proposal_id: 13,
    position_id: 3,
    symbol_id: 500,
    ticker: "BBB",
    side: "sell",
    order_type: "stop",
    purpose: "stop",
    qty: 30,
    stop_price: "13.8000",
    limit_price: null,
    stop_loss: null,
    tif: "gtc",
    status: "cancelled",
    reason: "protective stop",
    session_date: "2026-10-05",
    submitted_at: "2026-10-05T13:37:02Z",
    closed_at: "2026-10-05T19:50:00Z",
    cancel_reason: "position flattened",
  },
  {
    id: 23,
    proposal_id: null,
    position_id: 3,
    symbol_id: 500,
    ticker: "BBB",
    side: "sell",
    order_type: "market",
    purpose: "exit",
    qty: 30,
    stop_price: null,
    limit_price: null,
    stop_loss: null,
    tif: "day",
    status: "filled",
    reason: "flatten: close-10m",
    session_date: "2026-10-05",
    submitted_at: "2026-10-05T19:50:00Z",
    closed_at: "2026-10-05T19:50:02Z",
    cancel_reason: null,
  },
];

export const fills: FillOut[] = [
  {
    id: 31,
    order_id: 21,
    ticker: "BBB",
    side: "buy",
    purpose: "entry",
    ts: "2026-10-05T13:37:01Z",
    qty: 30,
    price: "14.2150",
    fees: { commission: "0.0000", ecn: "0.1050", sec: "0.0000", taf: "0.0000" },
    quote_snapshot: { bid: "14.2000", ask: "14.2200", last: "14.2100", last_trade_time: "2026-10-05T13:37:00Z" },
    slippage: "0.0050",
  },
  {
    id: 32,
    order_id: 23,
    ticker: "BBB",
    side: "sell",
    purpose: "exit",
    ts: "2026-10-05T19:50:02Z",
    qty: 30,
    price: "14.8800",
    fees: { commission: "0.0000", ecn: "0.1050", sec: "0.0100", taf: "0.0050" },
    quote_snapshot: { bid: "14.8800", ask: "14.9000", last: "14.8900", last_trade_time: "2026-10-05T19:50:01Z" },
    slippage: "0.0100",
  },
];

export const trade: TradeOut = {
  id: 7,
  position_id: 3,
  symbol_id: 500,
  ticker: "BBB",
  strategy_key: "orb_sip",
  session_date: "2026-10-05",
  entry_price: "14.2150",
  exit_price: "14.8800",
  qty: 30,
  pnl: "19.7250",
  pnl_r: "1.6035",
  planned_risk: "12.3000",
  exit_reason: "flatten",
  slippage_total: "0.0150",
  fees_total: "0.2250",
  opened_at: "2026-10-05T13:37:01Z",
  closed_at: "2026-10-05T19:50:02Z",
};

export const trades: TradeOut[] = [
  trade,
  {
    ...trade,
    id: 6,
    position_id: 2,
    symbol_id: 499,
    ticker: "EEE",
    session_date: "2026-10-02",
    entry_price: "31.0500",
    exit_price: "30.4000",
    qty: 15,
    pnl: "-9.9000",
    pnl_r: "-1.0000",
    planned_risk: "9.9000",
    exit_reason: "stop",
    opened_at: "2026-10-02T13:38:00Z",
    closed_at: "2026-10-02T15:02:11Z",
  },
];

export const signal: SignalOut = {
  id: 55,
  strategy_key: "orb_sip",
  config_revision: 3,
  config_version: "1.0.0",
  event_key: "orb_open",
  ts: "2026-10-05T13:35:05Z",
  intent: { type: "EnterLong", symbol_id: 500, order_type: "stop", stop: "14.21", stop_loss: "13.80" },
  evidence: { rvol: "2.40", atr: "0.52", candle: { open: "14.02", high: "14.20", low: "13.98", close: "14.18" }, rank: 1 },
};

/** 5-minute candles of 2026-10-05 around the entry (09:30-10:00 ET). */
export const candles: CandleOut[] = [
  { start: "2026-10-05T13:30:00Z", open: "14.0200", high: "14.2000", low: "13.9800", close: "14.1800", volume: 182_000 },
  { start: "2026-10-05T13:35:00Z", open: "14.1800", high: "14.3100", low: "14.1500", close: "14.2900", volume: 141_500 },
  { start: "2026-10-05T13:40:00Z", open: "14.2900", high: "14.4000", low: "14.2200", close: "14.3600", volume: 98_200 },
  { start: "2026-10-05T13:45:00Z", open: "14.3600", high: "14.4500", low: "14.3000", close: "14.4100", volume: 77_900 },
  { start: "2026-10-05T13:50:00Z", open: "14.4100", high: "14.5200", low: "14.3800", close: "14.5000", volume: 64_300 },
  { start: "2026-10-05T13:55:00Z", open: "14.5000", high: "14.5600", low: "14.4400", close: "14.4700", volume: 55_100 },
];

/** Position 3's full chain (a closed trade with its chart). */
export const positionDetail: PositionDetailOut = {
  position: closedPosition,
  trade,
  signal,
  proposals: [decidedProposal, stopProposal],
  orders,
  fills,
  candles,
  chart_error: null,
};

// ---------------------------------------------------------------- candidates

export const candidatesRanking: CandidateOut[] = [
  {
    id: 801,
    session_date: SESSION_DATE,
    strategy_key: "orb_sip",
    symbol_id: 510,
    ticker: "AAA",
    rvol: "3.1000",
    rank: 1,
    passed: true,
    reject_reason: null,
    candle: { open: "21.10", high: "21.55", low: "21.02", close: "21.50", volume: 210_000 },
    data: { atr: "0.58", avg_volume: 2_100_000 },
  },
  {
    id: 802,
    session_date: SESSION_DATE,
    strategy_key: "orb_sip",
    symbol_id: 511,
    ticker: "FFF",
    rvol: "2.2000",
    rank: 2,
    passed: false,
    reject_reason: "doji opening candle",
    candle: { open: "8.40", high: "8.55", low: "8.30", close: "8.41", volume: 95_000 },
    data: null,
  },
  {
    id: 803,
    session_date: SESSION_DATE,
    strategy_key: "orb_sip",
    symbol_id: 512,
    ticker: "GGG",
    rvol: "1.4000",
    rank: 3,
    passed: false,
    reject_reason: "no confirmed catalyst",
    candle: null,
    data: null,
  },
];

export const catalysts: CatalystOut[] = [
  {
    symbol_id: 510,
    ticker: "AAA",
    session_date: SESSION_DATE,
    catalyst_type: "earnings",
    direction: "positive",
    quality: 82,
    confirmed: true,
    reason: "Q3 revenue and EPS beat; guidance raised",
    gap_pct: "0.0640",
    earnings_date: "2026-10-05",
    headlines: [
      { ts: "2026-10-05T20:05:00Z", title: "AAA beats on revenue, raises full-year outlook", source: "Business Wire", url: "https://www.businesswire.com/news/aaa-q3" },
      { ts: "2026-10-06T11:20:00Z", title: "AAA shares jump premarket", source: null, url: "javascript:alert(1)" },
    ],
    model: "claude-haiku",
    classified_at: "2026-10-06T12:00:30Z",
  },
];

export const candidatesOut: CandidatesOut = {
  session_date: SESSION_DATE,
  brief: "Pre-market brief 2026-10-06\n1. AAA +6.4% earnings beat (quality 82)\n2. FFF +3.1% analyst upgrade",
  catalysts,
  ranking: candidatesRanking,
};

// ---------------------------------------------------------------- dashboard

export const timeline: TimelineItemOut[] = [
  { key: "premarket", label: "Pre-market scan", kind: "job", at: "2026-10-06T12:00:00Z", status: "done", detail: null },
  { key: "preopen", label: "Pre-open check", kind: "job", at: "2026-10-06T13:20:00Z", status: "done", detail: null },
  { key: "orb_open", label: "ORB entry (orb_open)", kind: "event", at: "2026-10-06T13:35:05Z", status: "done", detail: null },
  { key: "entry_cancel", label: "Cancel unfilled entries", kind: "event", at: "2026-10-06T15:30:00Z", status: "next", detail: null },
  { key: "checkin@11:30", label: "Check-in", kind: "job", at: "2026-10-06T15:30:00Z", status: "upcoming", detail: null },
  { key: "overlay_decision", label: "SPY overlay decision", kind: "event", at: "2026-10-06T19:30:00Z", status: "upcoming", detail: null },
  { key: "flatten", label: "Flatten", kind: "event", at: "2026-10-06T19:50:00Z", status: "upcoming", detail: null },
  { key: "postclose", label: "Post-close", kind: "job", at: "2026-10-06T20:15:00Z", status: "upcoming", detail: null },
];

export const pnlOut: PnlOut = {
  session_date: SESSION_DATE,
  realized_today: "0.0000",
  unrealized: "12.0000",
  unrealized_partial: false,
  week_to_date: "19.7250",
  equity: "751.7250",
  peak_equity: "751.7250",
  drawdown_pct: "0.0000",
};

export const dashboardOut: DashboardOut = {
  server_time: SERVER_TIME,
  run_id: RUN_ID,
  session: {
    date: SESSION_DATE,
    phase: "open",
    is_session: true,
    open_at: "2026-10-06T13:30:00Z",
    close_at: "2026-10-06T20:00:00Z",
  },
  approval_mode: "manual",
  telegram_configured: true,
  timeline,
  pending: [pendingProposal],
  positions: [openPosition],
  pnl: pnlOut,
  killswitches: killswitchStates,
  events,
  token: tokenOut,
  worker: workerOut,
  candidates_top: candidatesRanking,
  candidates_count: 3,
};

// ---------------------------------------------------------------- performance and journal

export const metricsOut: MetricsOut = {
  run_id: RUN_ID,
  from_date: null,
  to_date: null,
  trades: 4,
  wins: 2,
  win_rate: "0.5000",
  avg_win_r: "1.2500",
  avg_loss_r: "-1.0000",
  expectancy_r: "0.1250",
  profit_factor: "1.2500",
  avg_slippage: "0.0075",
  max_drawdown_pct: "0.0210",
  adherence_pct: "0.7500",
  total_pnl: "5.0000",
  r_histogram: [
    { lo: "-1.0", hi: "-0.5", count: 2 },
    { lo: "0.5", hi: "1.0", count: 1 },
    { lo: "2.0", hi: "2.5", count: 1 },
  ],
  losses: 2,
  total_fees: "0.0412",
  avg_slippage_per_share: "0.0003",
  trades_without_r: 0,
};

export const emptyMetrics: MetricsOut = {
  ...metricsOut,
  trades: 0,
  wins: 0,
  win_rate: null,
  avg_win_r: null,
  avg_loss_r: null,
  expectancy_r: null,
  profit_factor: null,
  avg_slippage: null,
  max_drawdown_pct: null,
  adherence_pct: null,
  total_pnl: "0.0000",
  r_histogram: [],
  losses: 0,
  total_fees: "0.0000",
  avg_slippage_per_share: null,
  trades_without_r: 0,
};

export const equityOut: EquityOut = {
  run_id: RUN_ID,
  points: [
    { ts: "2026-10-01T20:00:00Z", equity: "720.0000", cash: "720.0000", settled_cash: "720.0000", peak_equity: "720.0000", drawdown_pct: "0.0000" },
    { ts: "2026-10-02T20:00:00Z", equity: "710.1000", cash: "710.1000", settled_cash: "710.1000", peak_equity: "720.0000", drawdown_pct: "0.0138" },
    { ts: "2026-10-05T20:00:00Z", equity: "729.8250", cash: "729.8250", settled_cash: "710.1000", peak_equity: "729.8250", drawdown_pct: "0.0000" },
  ],
};

export const journalDays: JournalDayOut[] = [
  { session_date: "2026-10-05", rules_followed: true, notes: "Clean entry, flattened on time.", answered_via: "telegram", updated_at: "2026-10-05T20:31:00Z", trades: 1, realized_pnl: "19.7250" },
  { session_date: "2026-10-02", rules_followed: false, notes: null, answered_via: "web", updated_at: "2026-10-02T21:00:00Z", trades: 1, realized_pnl: "-9.9000" },
  { session_date: "2026-10-01", rules_followed: null, notes: null, answered_via: null, updated_at: null, trades: 0, realized_pnl: null },
];

// ---------------------------------------------------------------- settings and strategies

function field(partial: Partial<FieldOut> & Pick<FieldOut, "name" | "kind" | "title">): FieldOut {
  return {
    description: null,
    default: null,
    minimum: null,
    maximum: null,
    exclusive_minimum: false,
    exclusive_maximum: false,
    enum: null,
    item_enum: null,
    pattern: null,
    nullable: false,
    ...partial,
  };
}

function setting(
  key: string,
  group: string,
  value: unknown,
  f: Partial<FieldOut> & Pick<FieldOut, "kind" | "title">,
  extra: Partial<SettingOut> = {},
): SettingOut {
  const fo = field({ name: key, ...f });
  return {
    key,
    value,
    default: fo.default,
    is_default: JSON.stringify(value) === JSON.stringify(fo.default),
    group,
    field: fo,
    updated_at: null,
    updated_by: null,
    ...extra,
  };
}

/** One setting of each `FieldKind` (plus a nullable one), grouped as `trader.api.forms.SETTING_GROUPS`. */
export const settingsItems: SettingOut[] = [
  setting("approval_mode", "Approvals", "manual", { kind: "enum", title: "Approval Mode", default: "manual", enum: ["manual", "auto"] }),
  setting("cash_account_mode", "Account", true, { kind: "boolean", title: "Cash Account Mode", default: true }),
  setting("markets_enabled", "Account", ["US"], { kind: "enum_list", title: "Markets Enabled", default: ["US"], item_enum: ["US", "TSX"] }),
  setting("starting_cash", "Account", "720", { kind: "decimal", title: "Starting Cash", default: "720", minimum: "0", exclusive_minimum: true, maximum: "10000000" }),
  setting(
    "risk_pct",
    "Risk",
    "0.02",
    { kind: "decimal", title: "Risk Pct", description: "Fraction of equity risked per trade", default: "0.02", minimum: "0", exclusive_minimum: true, maximum: "0.10" },
  ),
  setting("no_entry_before_close_minutes", "Risk", 30, { kind: "integer", title: "No Entry Before Close Minutes", default: 30, minimum: "0", maximum: "390" }),
  setting("quote_poll_seconds", "Fill model", 2, { kind: "number", title: "Quote Poll Seconds", default: 2, minimum: "1", maximum: "60" }),
  setting("universe.finviz_filters", "Screening", "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa", {
    kind: "string",
    title: "Universe Finviz Filters",
    default: "ind_stocksonly,sh_price_5to50,sh_avgvol_o1000,ta_averagetruerange_o0.5,geo_usa",
    pattern: "^[a-z0-9_.]+(,[a-z0-9_.]+)*$",
  }),
  setting("universe.extra_symbols", "Screening", ["SPY"], { kind: "string_list", title: "Universe Extra Symbols", default: ["SPY"] }),
  setting(
    "scheduler.always_fire_late",
    "Worker and Telegram",
    ["flatten", "entry_cancel", "overlay_decision"],
    { kind: "string_list", title: "Scheduler Always Fire Late", default: ["flatten", "entry_cancel", "overlay_decision"] },
  ),
  setting(
    "killswitch.daily_loss_pct",
    "Kill switches",
    "0.05",
    { kind: "decimal", title: "Daily Loss Pct", default: "0.03", minimum: "0", exclusive_minimum: true, maximum: "1", nullable: true },
    { updated_at: "2026-10-01T15:00:00Z", updated_by: "web:stephen" },
  ),
];

export const settingsOut: SettingsOut = { items: settingsItems };

export const orbSipStrategy: StrategyOut = {
  key: "orb_sip",
  version: "1.0.0",
  kind: "entry",
  enabled: true,
  revision: 3,
  params: {
    price_min: "5",
    price_max: "50",
    min_avg_volume: 1_000_000,
    rvol_min: "1.00",
    top_n: 20,
    require_catalyst: true,
    entry_cancel_at: "open+120m",
    stale_universe: "skip",
  },
  schema: { title: "OrbSipParams", type: "object", properties: {} },
  fields: [
    field({ name: "price_min", kind: "decimal", title: "Price Min", default: "5", minimum: "0", exclusive_minimum: true }),
    field({ name: "price_max", kind: "decimal", title: "Price Max", default: "50", minimum: "0", exclusive_minimum: true }),
    field({ name: "min_avg_volume", kind: "integer", title: "Min Avg Volume", default: 1_000_000, minimum: "0" }),
    field({ name: "rvol_min", kind: "decimal", title: "Rvol Min", default: "1.00", minimum: "0" }),
    field({ name: "top_n", kind: "integer", title: "Top N", default: 20, minimum: "1", maximum: "200" }),
    field({ name: "require_catalyst", kind: "boolean", title: "Require Catalyst", default: true }),
    field({ name: "entry_cancel_at", kind: "string", title: "Entry Cancel At", default: "open+120m", nullable: true }),
    field({ name: "stale_universe", kind: "enum", title: "Stale Universe", default: "skip", enum: ["skip", "trade"] }),
  ],
  updated_at: "2026-10-01T15:00:00Z",
  updated_by: "web:stephen",
  owns_open_positions: true,
};

export const spyOverlayStrategy: StrategyOut = {
  key: "spy_overlay",
  version: "1.0.0",
  kind: "overlay",
  enabled: true,
  revision: 1,
  params: { decision_at: "close-30m", benchmark: "SPY", signal: "rest_of_day", max_quote_age_seconds: 120 },
  schema: { title: "SpyOverlayParams", type: "object", properties: {} },
  fields: [
    field({ name: "decision_at", kind: "string", title: "Decision At", default: "close-30m" }),
    field({ name: "benchmark", kind: "string", title: "Benchmark", default: "SPY", pattern: "^[A-Z][A-Z0-9.\\-]{0,9}$" }),
    field({ name: "signal", kind: "enum", title: "Signal", default: "rest_of_day", enum: ["rest_of_day"] }),
    field({ name: "max_quote_age_seconds", kind: "integer", title: "Max Quote Age Seconds", default: 120, minimum: "1", maximum: "3600" }),
  ],
  updated_at: "2026-09-27T12:00:00Z",
  updated_by: null,
  owns_open_positions: false,
};

export const strategies: Items<StrategyOut> = { items: [orbSipStrategy, spyOverlayStrategy] };

// ---------------------------------------------------------------- system and jobs

export const jobRuns: JobRunOut[] = [
  { id: 301, job: "nightly", session_date: SESSION_DATE, started_at: "2026-10-06T00:30:00Z", finished_at: "2026-10-06T00:34:12Z", status: "succeeded", error: null, detail: { source: "finviz", symbols: 412 }, duration_seconds: 252 },
  { id: 302, job: "premarket", session_date: SESSION_DATE, started_at: "2026-10-06T12:00:00Z", finished_at: "2026-10-06T12:00:41Z", status: "succeeded", error: null, detail: null, duration_seconds: 41 },
  { id: 303, job: "preopen", session_date: SESSION_DATE, started_at: "2026-10-06T13:20:00Z", finished_at: "2026-10-06T13:20:03Z", status: "succeeded", error: null, detail: null, duration_seconds: 3 },
  { id: 290, job: "postclose", session_date: "2026-10-05", started_at: "2026-10-05T20:15:00Z", finished_at: "2026-10-05T20:16:00Z", status: "failed", error: "Candle archive failed for 1 symbol\nTraceback omitted", detail: null, duration_seconds: 60 },
];

export const notificationsFailed: NotificationOut[] = [
  { id: 71, kind: "proposal", status: "failed", created_at: "2026-10-05T13:36:06Z", sent_at: null, attempts: 3, error: "TimedOut" },
];

export const systemOut: SystemOut = {
  server_time: SERVER_TIME,
  version: "dev",
  app_env: "dev",
  alembic_revision: "0005",
  tz_iana_version: "2026c",
  telegram_configured: true,
  token: tokenOut,
  worker: workerOut,
  rate_limit: { market: 18, account: 29 },
  last_runs: jobRuns,
  errors: errorEvents,
  notifications_failed: notificationsFailed,
  manual_jobs: ["nightly", "premarket", "preopen", "postclose", "token-refresh"],
};

export const jobLaunchOut: JobLaunchOut = { job: "nightly", session_date: "2026-10-07", launched: true, message: "nightly launched for 2026-10-07" };

export const telegramTestOut: TelegramTestOut = { sent: true, message: "Test message sent: check your Telegram" };

// ---------------------------------------------------------------- watchlist

export const watchlistOut: WatchlistOut = {
  session_date: "2026-10-07",
  tickers: ["AAPL", "BF.B", "MSFT"],
  filename: "watchlist.csv",
  uploaded_at: "2026-10-06T22:10:00Z",
  uploaded_by: "web:stephen",
};

export const watchlistUploadOut: WatchlistUploadOut = {
  watchlist: watchlistOut,
  rejected: [
    { row: 4, value: "AAPL", reason: "duplicate" },
    { row: 5, value: "$$$", reason: "invalid ticker" },
  ],
  launched: null,
};

// ---------------------------------------------------------------- replays (P5)
// A replay of the week 2026-11-23..2026-11-27 (Thanksgiving on the 26th, so 4 sessions); the 23rd had no
// universe snapshot, so it is a biased day.

export const replayOptions: ReplayOptionsOut = {
  override_keys: [
    "cash_account_mode",
    "fees.commission",
    "fees.sec_rate",
    "killswitch.daily_loss_pct",
    "killswitch.expectancy_min_trades",
    "killswitch.expectancy_threshold_r",
    "killswitch.max_drawdown_pct",
    "no_entry_before_close_minutes",
    "replay.catalyst_mode",
    "replay.half_spread_bps",
    "risk_pct",
    "slippage_bps",
    "slippage_min",
    "starting_cash",
  ],
  max_sessions: 130,
  latest_allowed: "2026-11-27",
  questrade_from: "2026-09-03",
  archive_from: "2026-10-06",
  snapshots_from: "2026-09-28",
  busy: false,
  offline_now: false,
};

const replayBase: ReplayOut = {
  id: 12,
  label: "Thanksgiving week",
  status: "queued",
  date_from: "2026-11-23",
  date_to: "2026-11-27",
  created_at: "2026-11-28T15:00:00Z",
  finished_at: null,
  data_mode: "full",
  catalyst_mode: "stored",
  half_spread_bps: "5",
  overrides: {},
  strategies: [
    { key: "orb_sip", config_id: 3, revision: 2, version: "1.0.0", scope: "live", enabled: true, params: { top_n: 20, require_catalyst: true } },
    { key: "spy_overlay", config_id: 4, revision: 1, version: "1.0.0", scope: "live", enabled: true, params: {} },
  ],
  progress: {
    sessions_total: 4,
    sessions_done: 0,
    current_date: null,
    trades: 0,
    forced_closes: 0,
    biased_days: [],
    missing_opening_bars: 0,
    missing_minute_bars: 0,
    questrade_requests: 0,
  },
  biased: false,
  cancel_requested: false,
  error: null,
  metrics: null,
  live_metrics: null,
  events: [],
};

export const replayQueued: ReplayOut = replayBase;

export const replayRunning: ReplayOut = {
  ...replayBase,
  id: 13,
  label: "Risk 1%",
  status: "running",
  overrides: { risk_pct: "0.01" },
  progress: {
    ...replayBase.progress,
    sessions_total: 10,
    sessions_done: 3,
    current_date: "2026-11-18",
    trades: 2,
    questrade_requests: 41,
  },
};

export const replayCompleted: ReplayOut = {
  ...replayBase,
  id: 11,
  label: "Thanksgiving week (biased universe)",
  status: "completed",
  finished_at: "2026-11-28T15:04:30Z",
  strategies: [
    { key: "orb_sip", config_id: 9, revision: 2, version: "1.0.0", scope: "replay", enabled: true, params: { top_n: 10, require_catalyst: true } },
    { key: "spy_overlay", config_id: 4, revision: 1, version: "1.0.0", scope: "live", enabled: true, params: {} },
  ],
  progress: {
    sessions_total: 4,
    sessions_done: 4,
    current_date: "2026-11-27",
    trades: 5,
    forced_closes: 1,
    biased_days: ["2026-11-23"],
    missing_opening_bars: 2,
    missing_minute_bars: 1,
    questrade_requests: 64,
  },
  biased: true,
  metrics: {
    run_id: 11,
    from_date: "2026-11-23",
    to_date: "2026-11-27",
    trades: 5,
    wins: 2,
    win_rate: "0.4000",
    avg_win_r: "1.2500",
    avg_loss_r: "-1.0000",
    expectancy_r: "0.1250",
    profit_factor: "1.0870",
    avg_slippage: "0.2000",
    max_drawdown_pct: "0.0310",
    adherence_pct: null,
    total_pnl: "2.0000",
    r_histogram: [
      { lo: "-1.0", hi: "-0.5", count: 2 },
      { lo: "0.5", hi: "1.0", count: 1 },
      { lo: "2.0", hi: "2.5", count: 1 },
    ],
    losses: 3,
    total_fees: "0.0412",
    avg_slippage_per_share: "0.0100",
    trades_without_r: 1,
  },
  live_metrics: {
    run_id: RUN_ID,
    from_date: "2026-11-23",
    to_date: "2026-11-27",
    trades: 4,
    wins: 2,
    win_rate: "0.5000",
    avg_win_r: "1.1000",
    avg_loss_r: "-0.9000",
    expectancy_r: "0.1000",
    profit_factor: "1.2000",
    avg_slippage: "0.1500",
    max_drawdown_pct: "0.0250",
    adherence_pct: "0.7500",
    total_pnl: "4.1000",
    r_histogram: [],
    losses: 2,
    total_fees: "0.0330",
    avg_slippage_per_share: "0.0080",
    trades_without_r: 0,
  },
  events: [
    {
      id: 5012,
      ts: "2026-11-27T17:50:00Z",
      level: "info",
      source: "replay",
      message: "Forced close of 1 position at 12:59 ET",
      data: { forced_closes: 1 },
    },
    {
      id: 5003,
      ts: "2026-11-23T14:35:05Z",
      level: "warning",
      source: "replay.data",
      message: "2 opening bars missing on 2026-11-23",
      data: { missing: 2 },
    },
  ],
};

export const replaySummaries: ReplaySummaryOut[] = [
  {
    id: 13,
    label: "Risk 1%",
    status: "running",
    date_from: "2026-11-09",
    date_to: "2026-11-20",
    created_at: "2026-11-28T15:10:00Z",
    finished_at: null,
    data_mode: "full",
    biased: false,
    trades: 2,
    expectancy_r: null,
    total_pnl: null,
  },
  {
    id: 11,
    label: "Thanksgiving week (biased universe)",
    status: "completed",
    date_from: "2026-11-23",
    date_to: "2026-11-27",
    created_at: "2026-11-28T15:00:00Z",
    finished_at: "2026-11-28T15:04:30Z",
    data_mode: "full",
    biased: true,
    trades: 5,
    expectancy_r: "0.1250",
    total_pnl: "2.0000",
  },
  {
    id: 10,
    label: "replay 2026-11-02..2026-11-06",
    status: "failed",
    date_from: "2026-11-02",
    date_to: "2026-11-06",
    created_at: "2026-11-27T22:00:00Z",
    finished_at: "2026-11-27T22:01:00Z",
    data_mode: "offline",
    biased: false,
    trades: 0,
    expectancy_r: null,
    total_pnl: null,
  },
  {
    id: 9,
    label: "Cancelled test",
    status: "cancelled",
    date_from: "2026-10-26",
    date_to: "2026-10-30",
    created_at: "2026-11-27T21:00:00Z",
    finished_at: "2026-11-27T21:02:00Z",
    data_mode: "offline",
    biased: false,
    trades: 1,
    expectancy_r: "-1.0000",
    total_pnl: "-7.2000",
  },
];

// ---------------------------------------------------------------- weekly report (P5)
// The week 2026-11-23..2026-11-27; the commentary quotes only numbers from its facts.

const weeklyFacts = {
  week: { start: "2026-11-23", end: "2026-11-27", sessions: 4 },
  week_metrics: {
    trades: 4,
    wins: 2,
    losses: 2,
    win_rate: "0.5000",
    expectancy_r: "0.1250",
    avg_win_r: "1.2500",
    avg_loss_r: "-1.0000",
    profit_factor: "1.2500",
    total_pnl: "5.00",
    total_fees: "0.0412",
    avg_slippage: "0.0075",
    max_drawdown_pct: "0.0210",
    adherence_pct: "0.7500",
  },
  kill_switch_trips: [],
  expectancy_switch: { closed_trades: 12, min_trades: 50 },
};

export const weeklyReportOk: WeeklyReportOut = {
  week_start: "2026-11-23",
  week_ending: "2026-11-27",
  run_id: RUN_ID,
  created_at: "2026-11-28T14:00:20Z",
  updated_at: "2026-11-28T14:00:20Z",
  commentary:
    "A short holiday week with 4 sessions and 4 trades. Two were winners, a 50% win rate, and the expectancy " +
    "was 0.1250R per trade for a P&L of $5.00.\n\nRisk stayed small: the largest drawdown was 2.1% and no kill " +
    "switch tripped. You followed your rules on 75% of the answered days.",
  commentary_status: "ok",
  commentary_error: null,
  model: "claude-sonnet-5",
  cost_usd: "0.012300",
  facts: weeklyFacts,
  telegram_status: "sent",
};

export const weeklyReportBudget: WeeklyReportOut = {
  ...weeklyReportOk,
  commentary: null,
  commentary_status: "budget",
  commentary_error: "daily Claude budget used up",
  model: null,
  cost_usd: "0.000000",
};

// ---------------------------------------------------------------- decision log (P6-T12)

export const decisionSummaryText =
  "811 scanned · 20 ranked · 1 passed (NVDA) · 1 proposal, approved by you in 42 s · filled 10.27 vs 10.25 planned (+0.02)";

export const decisionSummary: DecisionSummaryOut = {
  text: decisionSummaryText,
  universe_size: 812,
  premarket_listed: 12,
  premarket_classified: 10,
  scanned: 811,
  rvol_passed: 21,
  ranked: 20,
  passed: 1,
  rejects_by_rule: [
    { rule: "rvol_below_min", count: 790 },
    { rule: "catalyst_low_quality", count: 6 },
    { rule: "doji", count: 3 },
  ],
  signals: 1,
  risk_rejections: [],
  proposals: 1,
  approvals: { manual: 1, auto: 0, declined: 0, expired: 0, blocked: 0 },
  median_decision_seconds: 42,
  fills: 2,
  avg_fill_diff_per_share: "0.0200",
  trades: 1,
  wins: 1,
  losses: 0,
  pnl: "12.5000",
  pnl_r: "0.8000",
  exits_by_reason: [{ rule: "flatten", count: 1 }],
  notes: ["no baseline for 2 symbols"],
};

function decisionRow(partial: Partial<DecisionRowOut> & Pick<DecisionRowOut, "seq" | "stage" | "outcome">): DecisionRowOut {
  return {
    strategy_key: null,
    symbol_id: null,
    ticker: null,
    rule: null,
    reason: null,
    ts: "2026-10-06T13:35:05Z",
    ref: {},
    checks: [],
    data: {},
    ...partial,
  };
}

export const decisionRows: DecisionRowOut[] = [
  decisionRow({ seq: 1, stage: "universe", outcome: "info", ts: "2026-10-05T22:00:10Z", data: { count: 812, source: "finviz" } }),
  decisionRow({
    seq: 2,
    stage: "premarket",
    outcome: "classified",
    ticker: "NVDA",
    symbol_id: 7,
    ts: "2026-10-06T12:05:00Z",
    reason: "<b>Guidance raised</b> after the close",
    data: { type: "earnings", quality: 80, sources: ["news", "gap"] },
  }),
  decisionRow({
    seq: 3,
    stage: "scan",
    outcome: "passed",
    strategy_key: "orb_sip",
    ticker: "NVDA",
    symbol_id: 7,
    ref: { candidate_id: 5 },
    checks: [
      { name: "rvol", value: "3.20", op: ">=", threshold: "1.00", passed: true },
      { name: "price", value: "22.40", op: "between", threshold: "5-50", passed: true },
      { name: "atr14", value: null, op: "present", threshold: null, passed: null },
    ],
    data: { rvol: "3.20", rank: 1, entry: "10.2500", stop_loss: "9.8000" },
  }),
  decisionRow({
    seq: 4,
    stage: "scan",
    outcome: "rejected",
    strategy_key: "orb_sip",
    ticker: "AMD",
    symbol_id: 8,
    rule: "rvol_below_min",
    checks: [{ name: "rvol", value: "0.80", op: ">=", threshold: "1.00", passed: false }],
    data: { rvol: "0.80" },
  }),
  decisionRow({
    seq: 5,
    stage: "fill",
    outcome: "filled",
    ticker: "NVDA",
    symbol_id: 7,
    ts: "2026-10-06T13:38:05Z",
    data: { planned_price: "10.2500", fill_price: "10.2700", diff_per_share: "0.0200" },
  }),
  decisionRow({
    seq: 6,
    stage: "exit",
    outcome: "exited",
    ticker: "NVDA",
    symbol_id: 7,
    rule: "flatten",
    reason: "flatten_close",
    ts: "2026-10-06T19:55:00Z",
    data: { pnl: "12.5000", pnl_r: "0.8000" },
  }),
  decisionRow({ seq: 7, stage: "day", outcome: "info", ts: "2026-10-06T20:30:00Z", data: { text: decisionSummaryText } }),
];

export const decisionDayOut: DecisionDayOut = {
  run_id: RUN_ID,
  run_mode: "live",
  session_date: SESSION_DATE,
  final: true,
  recorded_at: "2026-10-06T20:30:00Z",
  summary: decisionSummary,
  rows: decisionRows,
  total: decisionRows.length,
};

export const decisionDaysOut: DecisionDaysOut = {
  days: [
    { run_id: RUN_ID, session_date: SESSION_DATE, final: true, summary_text: decisionSummaryText, proposals: 1, trades: 1 },
    { run_id: RUN_ID, session_date: "2026-10-05", final: true, summary_text: "790 scanned · 0 passed", proposals: 0, trades: 0 },
  ],
};
