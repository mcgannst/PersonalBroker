// TypeScript mirror of trader/api/schemas.py (P4 plan, task P4-T1 "trader.api.schemas").
// One exported type per pydantic model, same name, same (snake_case) field names.
// Decimal -> Money (a JSON string), datetime -> IsoTime (ISO 8601 UTC), date -> IsoDate (YYYY-MM-DD),
// `X | None` -> `X | null`. T18 checks this file against the Python schemas.

/** A pydantic `Decimal`, serialised as a JSON string (for example "21.5608"). Never do money maths on it. */
export type Money = string;
/** A pydantic `datetime`, serialised as an ISO 8601 UTC string. */
export type IsoTime = string;
/** A pydantic `date`, serialised as `YYYY-MM-DD`. */
export type IsoDate = string;
/** An arbitrary JSON object (a Python `dict`). */
export type JsonObject = Record<string, unknown>;

// ---------------------------------------------------------------- literals

/** `trader.market.sessions.SessionPhase`. */
export type SessionPhase = "closed_day" | "pre_market" | "open" | "after_close";
export type TimelineStatus = "done" | "failed" | "missed" | "running" | "skipped" | "next" | "upcoming";
export type FieldKind =
  | "decimal"
  | "integer"
  | "number"
  | "boolean"
  | "string"
  | "enum"
  | "string_list"
  | "enum_list";
export type Topic =
  | "proposals"
  | "orders"
  | "fills"
  | "positions"
  | "trades"
  | "candidates"
  | "killswitch"
  | "events"
  | "journal"
  | "jobs"
  | "settings"
  | "strategies"
  | "system"
  | "replays"
  | "reports"
  | "marks"
  | "activity";
export type ManualJob = "nightly" | "premarket" | "preopen" | "postclose" | "token-refresh" | "weekly";
/** `trader.replay.types.ReplayStatus` (re-exported by the schemas). */
export type ReplayStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type DataMode = "full" | "offline";
export type CatalystMode = "stored" | "unknown";
/** The weekly report's commentary outcome. */
export type CommentaryStatus = "ok" | "disabled" | "budget" | "rejected" | "error";
/** `trader.decisions.types.DecisionStage` (re-exported by the schemas), in `STAGE_ORDER`. */
export type DecisionStage =
  | "universe"
  | "premarket"
  | "scan"
  | "signal"
  | "risk"
  | "proposal"
  | "approval"
  | "order"
  | "fill"
  | "exit"
  | "overlay"
  | "kill_switch"
  | "day";
/** `trader.decisions.types.DecisionOutcome`. */
export type DecisionOutcome =
  | "info"
  | "listed"
  | "classified"
  | "passed"
  | "rejected"
  | "proposed"
  | "approved"
  | "auto_approved"
  | "declined"
  | "expired"
  | "blocked"
  | "submitted"
  | "filled"
  | "cancelled"
  | "exited"
  | "tripped"
  | "reset"
  | "error";
/** `trader.decisions.types.CheckOp`. */
export type CheckOp = ">=" | "<=" | "between" | "==" | "!=" | "present" | "absent";
/** Live dashboard and Control page literals (DB-T1). */
export type PeriodKey = "today" | "week" | "run";
export type LiveRange = "today" | "run";
export type MarkState = "live" | "stale" | "missing";
/** `paused`: a manual pause; `blocked`: an automatic kill switch. */
export type TradingState = "running" | "paused" | "blocked";
export type ActivityKind =
  | "order_placed"
  | "order_cancelled"
  | "fill"
  | "exit"
  | "proposal_created"
  | "proposal_approved"
  | "proposal_rejected"
  | "proposal_expired"
  | "kill_switch_tripped"
  | "kill_switch_reset"
  | "job_failed"
  | "alert"
  | "scan";
export type ActivityChip = "trades" | "proposals" | "alerts" | "scan";
export type ActivityTone = "neutral" | "up" | "down" | "warn";
export type EquitySource = "snapshot" | "marks" | "now";
export type BarSource = "candle" | "marks";
export type KillSwitchUnit = "pct" | "r" | "none";
export type RejectionSource = "decision_log" | "candidates" | "none";

/** Every `DecisionStage`, in the journal's stage order. */
export const DECISION_STAGES: readonly DecisionStage[] = [
  "universe",
  "premarket",
  "scan",
  "signal",
  "risk",
  "proposal",
  "approval",
  "order",
  "fill",
  "exit",
  "overlay",
  "kill_switch",
  "day",
];

/** Every `Topic`, in the schema's order (handy for exhaustive loops and tests). */
export const TOPICS: readonly Topic[] = [
  "proposals",
  "orders",
  "fills",
  "positions",
  "trades",
  "candidates",
  "killswitch",
  "events",
  "journal",
  "jobs",
  "settings",
  "strategies",
  "system",
  "replays",
  "reports",
  "marks",
  "activity",
];

/** Every `ManualJob`, in the schema's order. */
export const MANUAL_JOBS: readonly ManualJob[] = [
  "nightly",
  "premarket",
  "preopen",
  "postclose",
  "token-refresh",
  "weekly",
];

// Value sets the backend uses in string fields (from trunk: trader.engine.risk, trader.engine.proposals,
// trader.broker.types, trader.engine.killswitch). The schema fields themselves are plain strings.
export type ProposalKind = "entry" | "stop" | "exit" | "cancel";
export type ProposalStatus = "pending" | "approved" | "auto_approved" | "submitted" | "rejected" | "expired" | "failed";
export type Via = "telegram" | "web" | "auto";
export type Side = "buy" | "sell";
export type OrderType = "market" | "limit" | "stop" | "stop_limit";
export type Purpose = "entry" | "stop" | "exit";
export type KillSwitchName = "daily_loss_pct" | "max_drawdown_pct" | "expectancy" | "manual_pause";
export const KILL_SWITCHES: readonly KillSwitchName[] = ["daily_loss_pct", "max_drawdown_pct", "expectancy", "manual_pause"];

/** `Items[T]`. */
export interface Items<T> {
  items: T[];
}

// ---------------------------------------------------------------- errors and misc

export interface FieldError {
  loc: (string | number)[];
  msg: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  fields: FieldError[] | null;
  request_id: string | null;
}

export interface ErrorOut {
  error: ErrorBody;
}

export interface OkOut {
  ok: boolean;
  message: string | null;
}

// ---------------------------------------------------------------- auth

export interface LoginIn {
  username: string;
  password: string;
  totp?: string | null;
}

export interface UserOut {
  username: string;
  totp_enabled: boolean;
}

export interface SessionOut {
  user: UserOut;
  csrf_token: string;
  expires_at: IsoTime;
}

export interface PasswordChangeIn {
  current_password: string;
  new_password: string;
  totp?: string | null;
}

export interface TotpSetupIn {
  password: string;
}

export interface TotpSetupOut {
  secret: string;
  otpauth_uri: string;
}

export interface TotpConfirmIn {
  code: string;
}

export interface TotpDisableIn {
  password: string;
  code: string;
}

// ---------------------------------------------------------------- health and meta

export interface HealthOut {
  status: "ok" | "degraded" | "down";
  time: IsoTime;
  version: string;
  db_ok: boolean;
  db_latency_ms: number | null;
  token_ok: boolean;
  token_age_hours: number | null;
  worker_ok: boolean;
  worker_phase: string | null;
  worker_age_seconds: number | null;
}

export interface MetaOut {
  server_time: IsoTime;
  app_env: "dev" | "prod";
  version: string;
  tz_display: string;
  tz_offset_minutes: number;
  tz_iana_version: string | null;
  public_base_url: string;
}

// ---------------------------------------------------------------- status parts

export interface TokenOut {
  ok: boolean;
  seeded: boolean;
  age_hours: number | null;
  expires_at: IsoTime | null;
  last_refresh_at: IsoTime | null;
  error: string | null;
}

export interface WorkerOut {
  ok: boolean;
  phase: string | null;
  beat_at: IsoTime | null;
  age_seconds: number | null;
  session_date: IsoDate | null;
  pid: number | null;
  host: string | null;
  detail: JsonObject | null;
}

export interface EventOut {
  id: number;
  ts: IsoTime;
  level: string;
  source: string;
  message: string;
  data: JsonObject | null;
}

export interface KillSwitchOut {
  switch: string;
  label: string;
  tripped: boolean;
  tripped_at: IsoTime | null;
  value: Money | null;
  threshold: Money | null;
  automatic: boolean;
  needs_web_reset: boolean;
  clears: string;
}

export interface KillSwitchEventOut {
  id: number;
  switch: string;
  session_date: IsoDate;
  tripped_at: IsoTime;
  value: Money | null;
  threshold: Money | null;
  reset_at: IsoTime | null;
  reset_reason: string | null;
  reset_by: string | null;
}

// ---------------------------------------------------------------- trading

export interface ProposalOut {
  id: number;
  kind: string;
  status: string;
  ticker: string;
  side: string;
  order_type: string;
  qty: number;
  stop: Money | null;
  limit: Money | null;
  stop_loss: Money | null;
  risk_usd: Money | null;
  reason: string;
  strategy_key: string;
  created_at: IsoTime;
  expires_at: IsoTime;
  decided_at: IsoTime | null;
  decided_via: string | null;
  decided_by: string | null;
  decision_latency_ms: number | null;
  error: string | null;
  order_id: number | null;
  position_id: number | null;
}

export interface PositionOut {
  id: number;
  symbol_id: number;
  ticker: string;
  strategy_key: string;
  status: "open" | "closed";
  qty: number;
  entry: Money;
  last: Money | null;
  stop: Money | null;
  stop_working: boolean;
  unrealized_pnl: Money | null;
  unprotected_seconds: number;
  opened_at: IsoTime;
  closed_at: IsoTime | null;
}

export interface OrderOut {
  id: number;
  proposal_id: number | null;
  position_id: number | null;
  symbol_id: number;
  ticker: string;
  side: string;
  order_type: string;
  purpose: string;
  qty: number;
  stop_price: Money | null;
  limit_price: Money | null;
  stop_loss: Money | null;
  tif: string;
  status: string;
  reason: string;
  session_date: IsoDate;
  submitted_at: IsoTime;
  closed_at: IsoTime | null;
  cancel_reason: string | null;
}

export interface FillOut {
  id: number;
  order_id: number;
  ticker: string;
  side: string;
  purpose: string;
  ts: IsoTime;
  qty: number;
  price: Money;
  fees: JsonObject;
  quote_snapshot: JsonObject;
  slippage: Money;
}

export interface TradeOut {
  id: number;
  position_id: number;
  symbol_id: number;
  ticker: string;
  strategy_key: string;
  session_date: IsoDate;
  entry_price: Money;
  exit_price: Money;
  qty: number;
  pnl: Money;
  pnl_r: Money | null;
  planned_risk: Money | null;
  exit_reason: string;
  slippage_total: Money;
  fees_total: Money;
  opened_at: IsoTime;
  closed_at: IsoTime;
}

export interface SignalOut {
  id: number;
  strategy_key: string;
  config_revision: number;
  config_version: string;
  event_key: string;
  ts: IsoTime;
  intent: JsonObject;
  evidence: JsonObject;
}

export interface CandleOut {
  start: IsoTime;
  open: Money;
  high: Money;
  low: Money;
  close: Money;
  volume: number;
}

export interface PositionDetailOut {
  position: PositionOut;
  trade: TradeOut | null;
  signal: SignalOut | null;
  proposals: ProposalOut[];
  orders: OrderOut[];
  fills: FillOut[];
  candles: CandleOut[];
  chart_error: string | null;
}

export interface CandidateOut {
  id: number;
  session_date: IsoDate;
  strategy_key: string;
  symbol_id: number;
  ticker: string;
  rvol: Money | null;
  rank: number | null;
  passed: boolean;
  reject_reason: string | null;
  candle: JsonObject | null;
  data: JsonObject | null;
}

export interface HeadlineOut {
  ts: IsoTime | null;
  title: string;
  source: string | null;
  url: string | null;
}

export interface CatalystOut {
  symbol_id: number;
  ticker: string;
  session_date: IsoDate;
  catalyst_type: string;
  direction: string;
  quality: number | null;
  confirmed: boolean | null;
  reason: string | null;
  gap_pct: Money | null;
  earnings_date: IsoDate | null;
  headlines: HeadlineOut[];
  model: string | null;
  classified_at: IsoTime | null;
}

export interface CandidatesOut {
  session_date: IsoDate;
  brief: string | null;
  catalysts: CatalystOut[];
  ranking: CandidateOut[];
}

// ---------------------------------------------------------------- dashboard

export interface SessionInfoOut {
  date: IsoDate;
  phase: SessionPhase;
  is_session: boolean;
  open_at: IsoTime | null;
  close_at: IsoTime | null;
}

export interface TimelineItemOut {
  key: string;
  label: string;
  kind: "job" | "event";
  at: IsoTime;
  status: TimelineStatus;
  detail: string | null;
}

export interface PnlOut {
  session_date: IsoDate;
  realized_today: Money;
  unrealized: Money | null;
  unrealized_partial: boolean;
  week_to_date: Money;
  equity: Money;
  peak_equity: Money;
  drawdown_pct: Money;
}

export interface DashboardOut {
  server_time: IsoTime;
  run_id: number;
  session: SessionInfoOut;
  approval_mode: "manual" | "auto";
  telegram_configured: boolean;
  timeline: TimelineItemOut[];
  pending: ProposalOut[];
  positions: PositionOut[];
  pnl: PnlOut;
  killswitches: KillSwitchOut[];
  events: EventOut[];
  token: TokenOut;
  worker: WorkerOut;
  candidates_top: CandidateOut[];
  candidates_count: number;
}

// ---------------------------------------------------------------- decisions

export interface DecisionOut {
  proposal: ProposalOut;
  already_decided: boolean;
  blocked: string | null;
  message: string;
}

export interface KillSwitchesOut {
  switches: KillSwitchOut[];
  history: KillSwitchEventOut[];
}

export interface ResetIn {
  reason: string;
}

// ---------------------------------------------------------------- performance and journal

export interface HistogramBinOut {
  lo: Money;
  hi: Money;
  count: number;
}

export interface MetricsOut {
  run_id: number;
  from_date: IsoDate | null;
  to_date: IsoDate | null;
  trades: number;
  wins: number;
  win_rate: Money | null;
  avg_win_r: Money | null;
  avg_loss_r: Money | null;
  expectancy_r: Money | null;
  profit_factor: Money | null;
  avg_slippage: Money | null;
  max_drawdown_pct: Money | null;
  adherence_pct: Money | null;
  total_pnl: Money;
  r_histogram: HistogramBinOut[];
  // Phase 5 (trader.reports.metrics); always sent by the server.
  losses: number;
  total_fees: Money;
  avg_slippage_per_share: Money | null;
  trades_without_r: number;
}

export interface EquityPointOut {
  ts: IsoTime;
  equity: Money;
  cash: Money;
  settled_cash: Money;
  peak_equity: Money;
  drawdown_pct: Money;
}

export interface EquityOut {
  run_id: number;
  points: EquityPointOut[];
}

export interface JournalDayOut {
  session_date: IsoDate;
  rules_followed: boolean | null;
  notes: string | null;
  answered_via: string | null;
  updated_at: IsoTime | null;
  trades: number;
  realized_pnl: Money | null;
}

/** Fields not sent are left unchanged by the server (it reads `model_fields_set`). */
export interface JournalIn {
  rules_followed?: boolean | null;
  notes?: string | null;
}

// ---------------------------------------------------------------- settings and strategies

export interface FieldOut {
  name: string;
  kind: FieldKind;
  title: string;
  description: string | null;
  default: unknown;
  minimum: string | null;
  maximum: string | null;
  exclusive_minimum: boolean;
  exclusive_maximum: boolean;
  enum: string[] | null;
  item_enum: string[] | null;
  pattern: string | null;
  nullable: boolean;
}

export interface SettingOut {
  key: string;
  value: unknown;
  default: unknown;
  is_default: boolean;
  group: string;
  field: FieldOut;
  updated_at: IsoTime | null;
  updated_by: string | null;
}

export interface SettingsOut {
  items: SettingOut[];
}

export interface SettingIn {
  value: unknown;
}

export interface StrategyOut {
  key: string;
  version: string;
  kind: "entry" | "overlay";
  enabled: boolean;
  revision: number;
  params: JsonObject;
  schema: JsonObject;
  fields: FieldOut[];
  updated_at: IsoTime;
  updated_by: string | null;
  owns_open_positions: boolean;
}

export interface StrategyIn {
  params?: JsonObject | null;
  enabled?: boolean | null;
}

// ---------------------------------------------------------------- system and jobs

export interface JobRunOut {
  id: number;
  job: string;
  session_date: IsoDate;
  started_at: IsoTime;
  finished_at: IsoTime | null;
  status: string;
  error: string | null;
  detail: JsonObject | null;
  duration_seconds: number | null;
}

export interface JobRunIn {
  date?: IsoDate | null;
  force?: boolean;
}

export interface JobLaunchOut {
  job: string;
  /** null when the run has no date (token-refresh, or the command's own default session). */
  session_date: IsoDate | null;
  launched: boolean;
  message: string;
}

export interface NotificationOut {
  id: number;
  kind: string;
  status: string;
  created_at: IsoTime;
  sent_at: IsoTime | null;
  attempts: number;
  error: string | null;
}

export interface SystemOut {
  server_time: IsoTime;
  version: string;
  app_env: "dev" | "prod";
  alembic_revision: string | null;
  tz_iana_version: string | null;
  telegram_configured: boolean;
  token: TokenOut;
  worker: WorkerOut;
  rate_limit: Record<string, number> | null;
  last_runs: JobRunOut[];
  errors: EventOut[];
  notifications_failed: NotificationOut[];
  manual_jobs: ManualJob[];
}

export interface CredentialIn {
  refresh_token: string;
}

export interface TelegramTestOut {
  sent: boolean;
  message: string;
}

// ---------------------------------------------------------------- watchlist

export interface WatchlistOut {
  session_date: IsoDate;
  tickers: string[];
  filename: string | null;
  uploaded_at: IsoTime;
  uploaded_by: string;
}

export interface RejectedRowOut {
  row: number;
  value: string;
  reason: string;
}

export interface WatchlistUploadOut {
  watchlist: WatchlistOut;
  rejected: RejectedRowOut[];
  launched: JobLaunchOut | null;
}

// ---------------------------------------------------------------- replays

/** One strategy's override in a replay request; only what is sent changes. */
export interface ReplayStrategyIn {
  enabled?: boolean | null;
  params?: JsonObject | null;
}

export interface ReplayIn {
  date_from: IsoDate;
  date_to: IsoDate;
  label?: string | null;
  /** DB setting keys from `ReplayOptionsOut.override_keys`; only changed values are sent. */
  overrides?: JsonObject;
  strategies?: Record<string, ReplayStrategyIn>;
  offline?: boolean;
}

export interface ReplayStrategyOut {
  key: string;
  config_id: number;
  revision: number;
  version: string;
  scope: "live" | "replay";
  enabled: boolean;
  params: JsonObject;
}

export interface ReplayProgressOut {
  sessions_total: number;
  sessions_done: number;
  current_date: IsoDate | null;
  trades: number;
  forced_closes: number;
  biased_days: IsoDate[];
  missing_opening_bars: number;
  missing_minute_bars: number;
  questrade_requests: number;
}

export interface ReplaySummaryOut {
  id: number;
  label: string | null;
  status: ReplayStatus;
  date_from: IsoDate;
  date_to: IsoDate;
  created_at: IsoTime;
  finished_at: IsoTime | null;
  data_mode: DataMode;
  biased: boolean;
  trades: number;
  expectancy_r: Money | null;
  total_pnl: Money | null;
}

export interface ReplayOut {
  id: number;
  label: string | null;
  status: ReplayStatus;
  date_from: IsoDate;
  date_to: IsoDate;
  created_at: IsoTime;
  finished_at: IsoTime | null;
  data_mode: DataMode;
  catalyst_mode: CatalystMode;
  half_spread_bps: Money;
  overrides: JsonObject;
  strategies: ReplayStrategyOut[];
  progress: ReplayProgressOut;
  biased: boolean;
  cancel_requested: boolean;
  error: string | null;
  metrics: MetricsOut | null;
  /** The live run over the same dates. */
  live_metrics: MetricsOut | null;
  events: EventOut[];
}

export interface ReplayOptionsOut {
  override_keys: string[];
  max_sessions: number;
  latest_allowed: IsoDate;
  questrade_from: IsoDate;
  archive_from: IsoDate | null;
  snapshots_from: IsoDate | null;
  busy: boolean;
  offline_now: boolean;
}

// ---------------------------------------------------------------- reports

export interface WeeklyReportOut {
  week_start: IsoDate;
  week_ending: IsoDate;
  run_id: number;
  created_at: IsoTime;
  updated_at: IsoTime;
  commentary: string | null;
  commentary_status: CommentaryStatus;
  commentary_error: string | null;
  model: string | null;
  cost_usd: Money;
  facts: JsonObject;
  telegram_status: string | null;
}

// ---------------------------------------------------------------- decision log (P6-T9)

export interface CheckOut {
  name: string;
  value: string | null;
  op: CheckOp;
  threshold: string | null;
  passed: boolean | null;
}

export interface RuleCountOut {
  rule: string;
  count: number;
}

export interface DecisionRowOut {
  seq: number;
  stage: DecisionStage;
  strategy_key: string | null;
  symbol_id: number | null;
  ticker: string | null;
  outcome: DecisionOutcome;
  rule: string | null;
  reason: string | null;
  ts: IsoTime;
  ref: Record<string, number>;
  checks: CheckOut[];
  data: JsonObject;
}

export interface DecisionSummaryOut {
  text: string;
  universe_size: number | null;
  premarket_listed: number;
  premarket_classified: number;
  scanned: number;
  rvol_passed: number;
  ranked: number;
  passed: number;
  rejects_by_rule: RuleCountOut[];
  signals: number;
  risk_rejections: RuleCountOut[];
  proposals: number;
  approvals: Record<string, number>;
  median_decision_seconds: number | null;
  fills: number;
  avg_fill_diff_per_share: Money | null;
  trades: number;
  wins: number;
  losses: number;
  pnl: Money;
  pnl_r: Money | null;
  exits_by_reason: RuleCountOut[];
  notes: string[];
}

export interface DecisionDayOut {
  run_id: number;
  run_mode: string;
  session_date: IsoDate;
  final: boolean;
  recorded_at: IsoTime | null;
  summary: DecisionSummaryOut | null;
  rows: DecisionRowOut[];
  total: number;
}

export interface DecisionDayItemOut {
  run_id: number;
  session_date: IsoDate;
  final: boolean;
  summary_text: string | null;
  proposals: number;
  trades: number;
}

export interface DecisionDaysOut {
  days: DecisionDayItemOut[];
}

// ---------------------------------------------------------------- live dashboard and control (DB-T1)
// GET /api/live -> LiveOut, GET /api/control -> ControlOut. A null part failed; `part_errors` says why.

export interface PartErrorOut {
  part: string;
  message: string;
}

export interface PeriodPnlOut {
  period: PeriodKey;
  date_from: IsoDate;
  date_to: IsoDate;
  realized: Money;
  unrealized: Money | null;
  unrealized_partial: boolean;
  pnl_after_fees: Money;
  fees: Money;
  claude_usd: Money;
  net_after_ai: Money;
  trades: number;
  wins: number;
  losses: number;
  win_rate: Money | null;
  expectancy_r: Money | null;
  trades_without_r: number;
}

export interface ClaudeTodayOut {
  date: IsoDate;
  spent_usd: Money;
  cap_usd: Money;
  used_fraction: Money | null;
}

export interface BooksCheckOut {
  ok: boolean;
  cash: Money;
  positions_at_cost: Money;
  actual: Money;
  starting_cash: Money;
  realized_gross: Money;
  fees_paid: Money;
  expected: Money;
  difference: Money;
  realized_recorded: Money;
  open_positions: number;
}

export interface EquityPointLiveOut {
  ts: IsoTime;
  equity: Money;
  source: EquitySource;
}

export interface FillMarkerOut {
  fill_id: number;
  ts: IsoTime;
  ticker: string;
  side: string;
  purpose: string;
  qty: number;
  price: Money;
  position_id: number | null;
}

export interface EquitySeriesOut {
  range: LiveRange;
  start_equity: Money | null;
  points: EquityPointLiveOut[];
  fills: FillMarkerOut[];
  downsampled: boolean;
}

export interface KillSwitchLightOut {
  switch: string;
  label: string;
  tripped: boolean;
  tripped_at: IsoTime | null;
  value: Money | null;
  threshold: Money | null;
  unit: KillSwitchUnit;
  count: number | null;
  count_min: number | null;
  trip_value: Money | null;
  trip_threshold: Money | null;
  automatic: boolean;
  needs_web_reset: boolean;
  clears: string;
}

export interface RiskOut {
  killswitches: KillSwitchLightOut[];
  equity: Money;
  open_risk: Money;
  open_risk_cap: Money | null;
  slots_used: number;
  slots_max: number;
  open_positions: number;
}

export interface SparkPointOut {
  ts: IsoTime;
  price: Money;
}

export interface BarOut {
  start: IsoTime;
  open: Money;
  high: Money;
  low: Money;
  close: Money;
  source: BarSource;
}

export interface LivePositionOut {
  id: number;
  symbol_id: number;
  ticker: string;
  strategy_key: string;
  side: "long";
  qty: number;
  entry: Money;
  mark: Money | null;
  bid: Money | null;
  ask: Money | null;
  mark_at: IsoTime | null;
  mark_state: MarkState;
  stop: Money | null;
  stop_working: boolean;
  target: Money | null;
  planned_risk: Money | null;
  unrealized: Money | null;
  unrealized_r: Money | null;
  distance_to_stop_r: Money | null;
  near_stop: boolean;
  opened_at: IsoTime;
  held_seconds: number;
  unprotected_seconds: number;
  spark: SparkPointOut[];
  /** Only for the expanded positions (`expand`), else null. */
  bars: BarOut[] | null;
  fills: FillMarkerOut[];
  link: string;
}

export interface ActivityItemOut {
  id: string;
  ts: IsoTime;
  kind: ActivityKind;
  chip: ActivityChip;
  ticker: string | null;
  text: string;
  amount: Money | null;
  tone: ActivityTone;
  link: string | null;
}

export interface RejectionRuleOut {
  stage: DecisionStage;
  rule: string;
  count: number;
  tickers: string[];
  truncated: boolean;
  link: string;
}

export interface RejectionsOut {
  session_date: IsoDate;
  source: RejectionSource;
  total: number;
  rules: RejectionRuleOut[];
  final: boolean;
  recorded_at: IsoTime | null;
}

export interface LiveOut {
  server_time: IsoTime;
  run_id: number;
  run_started_at: IsoTime;
  session: SessionInfoOut;
  session_day: IsoDate;
  approval_mode: "manual" | "auto";
  trading: TradingState;
  telegram_configured: boolean;
  worker: WorkerOut;
  worker_stale: boolean;
  marks_stale_seconds: number;
  closed_today: number;
  periods: PeriodPnlOut[] | null;
  claude_today: ClaudeTodayOut | null;
  books: BooksCheckOut | null;
  equity: EquitySeriesOut | null;
  risk: RiskOut | null;
  positions: LivePositionOut[] | null;
  activity: ActivityItemOut[] | null;
  rejections: RejectionsOut | null;
  timeline: TimelineItemOut[] | null;
  pending: ProposalOut[] | null;
  part_errors: PartErrorOut[];
}

export interface EngineOut {
  approval_mode: "manual" | "auto";
  trading: TradingState;
  paused_at: IsoTime | null;
  run_id: number;
  run_started_at: IsoTime;
  run_start_date: IsoDate;
  version: string;
  app_env: string;
  alembic_revision: string | null;
}

export interface StrategyCardOut {
  key: string;
  kind: "entry" | "overlay";
  enabled: boolean;
  revision: number;
  version: string;
  updated_at: IsoTime;
  updated_by: string | null;
  owns_open_positions: boolean;
  max_positions: number | null;
  settings_link: string;
}

export interface ScheduleItemOut {
  key: string;
  label: string;
  kind: "job" | "event";
  at: IsoTime;
  status: TimelineStatus;
  detail: string | null;
  started_at: IsoTime | null;
  finished_at: IsoTime | null;
  duration_seconds: number | null;
  attempts: number;
  summary: string | null;
  rerun: ManualJob | null;
}

export interface QuestradeCountsOut {
  requests: number;
  http_429: number;
  pause_s: number;
  http_5xx: number;
  transport_errors: number;
}

export interface QuestradeStatsOut {
  day: IsoDate;
  since: IsoTime;
  market: QuestradeCountsOut;
  account: QuestradeCountsOut;
  rate_limit: Record<string, number> | null;
}

export interface OpeningBarsOut {
  session_date: IsoDate;
  started_at: IsoTime;
  symbols: number;
  completed: number;
  errors: number;
  outstanding: number;
  elapsed_s: number;
  deadline_s: number | null;
  http_429: number;
  pause_s: number;
  complete: boolean;
  raised: string | null;
}

export interface MarksHealthOut {
  written_at: IsoTime | null;
  symbols: number;
  failing: boolean;
}

export interface HealthPanelOut {
  worker: WorkerOut;
  worker_stale: boolean;
  token: TokenOut;
  db_ok: boolean;
  db_latency_ms: number | null;
  telegram_configured: boolean;
  questrade: QuestradeStatsOut | null;
  opening_bars: OpeningBarsOut | null;
  marks: MarksHealthOut | null;
  notifications_failed: NotificationOut[];
  tz_iana_version: string | null;
}

export interface SoakTodayOut {
  session_date: IsoDate;
  verdict: string;
  failed: string[];
  provisional: boolean;
}

export interface SoakSummaryOut {
  target: number;
  consecutive_clean: number;
  total_clean: number;
  day_one: IsoDate | null;
  earliest_finish: IsoDate | null;
  last_final: IsoDate | null;
  today: SoakTodayOut | null;
  generated_at: IsoTime;
}

export interface ControlOut {
  server_time: IsoTime;
  session: SessionInfoOut;
  manual_jobs: ManualJob[];
  engine: EngineOut | null;
  killswitches: KillSwitchLightOut[] | null;
  killswitch_history: KillSwitchEventOut[] | null;
  strategies: StrategyCardOut[] | null;
  schedule: ScheduleItemOut[] | null;
  health: HealthPanelOut | null;
  soak: SoakSummaryOut | null;
  errors: EventOut[] | null;
  part_errors: PartErrorOut[];
}

// ---------------------------------------------------------------- stream (SSE `data:` payloads)

export interface StreamHello {
  server_time: IsoTime;
}

export interface StreamInvalidate {
  topics: Topic[];
}

export interface StreamEvents {
  items: EventOut[];
}

// ---------------------------------------------------------------- options (OPTSIM; routes under /api/options)
// Net prices (net_limit, fill_net, entry_net, take_profit_net, net_at_market) are per share, credit positive:
// "0.45" is a credit of 0.45, "-1.20" a debit of 1.20. Contract ids are the server's option contract ids.

export type OptRight = "call" | "put";
export type OptEffect = "open" | "close";
export type OptInstrument = "option" | "shares";
export type OptOrderType = "market" | "limit";
export type OptTif = "day" | "gtc";
export type OptOrderIntent = "open" | "close" | "roll";
export type OptOrderStatus = "working" | "filled" | "cancelled" | "expired" | "rejected";
export type OptStructureKind =
  | "long_call"
  | "long_put"
  | "csp"
  | "covered_call"
  | "debit_spread"
  | "credit_spread"
  | "iron_condor"
  | "calendar"
  | "diagonal"
  | "shares"
  | "custom";
export type OptStructureState = "open" | "closed";
export type OptCloseReason = "closed" | "expired" | "assigned" | "exercised" | "called_away" | "rolled" | "sold";
export type OptRejectReason =
  | "naked_short"
  | "insufficient_cash"
  | "position_cap"
  | "shares_committed"
  | "not_covered_after_close"
  | "nothing_to_close"
  | "unknown_contract"
  | "expired_contract"
  | "invalid_order"
  | "no_quote"
  | "structure_frozen"
  | "strategies_paused";
export type OptPromptStatus = "pending" | "answered" | "expired" | "cancelled";
export type OptAnsweredVia = "telegram" | "web";
export type OptActivityKind = "fill" | "lifecycle" | "decision" | "alert" | "prompt" | "order";
export type OptTone = "ok" | "warn" | "bad";
export type OptPanelColumnKind = "text" | "money" | "number" | "date" | "badge" | "bool";
export type OptPanelActionKind = "button" | "toggle" | "text" | "choice";

export const OPT_REJECT_REASONS: readonly OptRejectReason[] = [
  "naked_short",
  "insufficient_cash",
  "position_cap",
  "shares_committed",
  "not_covered_after_close",
  "nothing_to_close",
  "unknown_contract",
  "expired_contract",
  "invalid_order",
  "no_quote",
  "structure_frozen",
  "strategies_paused",
];

export interface OptContractOut {
  id: number;
  underlying: string;
  expiry: IsoDate;
  strike: Money;
  right: OptRight;
  multiplier: number;
  is_monthly: boolean;
  dte: number;
  label: string;
}

export interface OptQuoteOut {
  contract_id: number;
  bid: Money | null;
  ask: Money | null;
  last: Money | null;
  bid_size: number | null;
  ask_size: number | null;
  volume: number | null;
  open_interest: number | null;
  iv: string | null;
  delta: string | null;
  gamma: string | null;
  theta: string | null;
  vega: string | null;
  fetched_at: IsoTime;
  stale: boolean;
}

export interface OptExpiryOut {
  expiry: IsoDate;
  dte: number;
  is_monthly: boolean;
  strikes: number;
}

export interface OptChainOut {
  underlying: string;
  underlying_price: Money | null;
  price_time: IsoTime | null;
  market_open: boolean;
  expiries: OptExpiryOut[];
}

export interface OptChainRowOut {
  strike: Money;
  call_contract_id: number | null;
  put_contract_id: number | null;
  call: OptQuoteOut | null;
  put: OptQuoteOut | null;
}

export interface OptChainQuotesOut {
  underlying: string;
  expiry: IsoDate;
  underlying_price: Money | null;
  fetched_at: IsoTime;
  market_open: boolean;
  rows: OptChainRowOut[];
}

export interface OptLegIn {
  instrument: OptInstrument;
  contract_id?: number | null;
  side: Side;
  effect: OptEffect;
  ratio: number;
}

export interface OptOrderIn {
  underlying: string;
  intent: OptOrderIntent;
  structure_id?: number | null;
  legs: OptLegIn[];
  qty: number;
  order_type: OptOrderType;
  net_limit?: Money | null;
  tif: OptTif;
  walk?: boolean;
}

export interface OptPreviewOut {
  accepted: boolean;
  reject_reason: OptRejectReason | null;
  detail: string;
  kind: OptStructureKind;
  net_at_market: Money | null;
  max_loss: Money | null;
  max_profit: Money | null;
  breakevens: Money[];
  fees: Money;
  reserve_cash: Money;
  cash_after: Money;
  free_cash_after: Money;
  exposure_after: Money;
  cap_limit: Money;
}

export interface OptLegOut {
  leg_no: number;
  instrument: OptInstrument;
  contract: OptContractOut | null;
  side: Side;
  effect: OptEffect;
  ratio: number;
  fill_price: Money | null;
  fill_quote: JsonObject | null;
}

export interface OptOrderOut {
  id: number;
  source: string;
  intent: OptOrderIntent;
  structure_id: number | null;
  underlying: string;
  legs: OptLegOut[];
  qty: number;
  order_type: OptOrderType;
  net_limit: Money | null;
  tif: OptTif;
  status: OptOrderStatus;
  walk: boolean;
  reject_reason: OptRejectReason | null;
  reject_detail: string | null;
  reason: string;
  reserved_cash: Money;
  submitted_at: IsoTime;
  closed_at: IsoTime | null;
  fill_net: Money | null;
  fees: Money | null;
}

export interface OptRepriceIn {
  net_limit: Money;
}

export interface OptPositionOut {
  id: number;
  instrument: OptInstrument;
  contract: OptContractOut | null;
  qty: number;
  avg_price: Money;
  mark: Money | null;
  unrealized_pnl: Money | null;
  delta: string | null;
}

export interface OptStructureOut {
  id: number;
  source: string;
  kind: OptStructureKind;
  underlying: string;
  state: OptStructureState;
  close_reason: OptCloseReason | null;
  frozen: boolean;
  qty: number;
  entry_net: Money;
  reserved_cash: Money;
  take_profit_net: Money | null;
  realized_pnl: Money;
  unrealized_pnl: Money | null;
  fees_total: Money;
  opened_at: IsoTime;
  closed_at: IsoTime | null;
  dte: number | null;
  positions: OptPositionOut[];
}

export interface OptSourceResultOut {
  source: string;
  open_structures: number;
  reserved: Money;
  realized_pnl: Money;
  unrealized_pnl: Money;
  premium_collected: Money;
}

export interface OptBenchmarkOut {
  ticker: string;
  since: IsoDate;
  benchmark_return: string | null;
  account_return: string | null;
}

export interface OptAccountOut {
  run_id: number | null;
  started_at: IsoTime | null;
  starting_cash: Money;
  cash: Money;
  reserved: Money;
  free_cash: Money;
  positions_value: Money;
  account_value: Money;
  premium_collected: Money;
  realized_pnl: Money;
  unrealized_pnl: Money;
  fees_total: Money;
  max_position_pct: string;
  marks_as_of: IsoTime | null;
  marks_complete: boolean;
  worker_beat_at: IsoTime | null;
  by_source: OptSourceResultOut[];
  benchmark: OptBenchmarkOut | null;
}

export interface OptActivityOut {
  id: string;
  ts: IsoTime;
  kind: OptActivityKind;
  source: string;
  underlying: string | null;
  title: string;
  detail: string;
  level: string;
  structure_id: number | null;
}

export interface OptPromptChoiceOut {
  code: string;
  label: string;
}

export interface OptPromptOut {
  id: number;
  source: string;
  kind: string;
  scope_key: string;
  title: string;
  body: string;
  choices: OptPromptChoiceOut[];
  needs_text: boolean;
  status: OptPromptStatus;
  asked_at: IsoTime;
  answered_at: IsoTime | null;
  answer: string | null;
  answer_text: string | null;
  answered_via: OptAnsweredVia | null;
  data: JsonObject;
}

export interface OptPromptAnswerIn {
  choice: string;
  text?: string | null;
}

export interface OptStrategyOut {
  key: string;
  version: string;
  enabled: boolean;
  revision: number;
  params: JsonObject;
  schema: JsonObject;
  fields: FieldOut[];
  updated_at: IsoTime;
  updated_by: string | null;
  open_structures: number;
  manual_events: string[];
}

export interface OptKeyValueOut {
  label: string;
  value: string;
  tone: OptTone | null;
}

export interface OptPanelColumnOut {
  key: string;
  label: string;
  kind: OptPanelColumnKind;
}

export interface OptPanelRowOut {
  id: string;
  cells: JsonObject;
  actions: string[];
  detail: OptKeyValueOut[];
}

export interface OptPanelTableOut {
  key: string;
  title: string;
  columns: OptPanelColumnOut[];
  rows: OptPanelRowOut[];
  empty_text: string;
}

export interface OptPanelActionOut {
  key: string;
  label: string;
  kind: OptPanelActionKind;
  confirm: boolean;
  choices: string[];
}

export interface OptPanelOut {
  strategy_key: string;
  summary: OptKeyValueOut[];
  tables: OptPanelTableOut[];
  actions: OptPanelActionOut[];
}

export interface OptPanelActionIn {
  action: string;
  row_id?: string | null;
  value?: string | boolean | null;
}

export interface OptPanelActionResultOut {
  ok: boolean;
  message: string;
}
