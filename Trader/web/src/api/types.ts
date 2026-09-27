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
  | "reports";
export type ManualJob = "nightly" | "premarket" | "preopen" | "postclose" | "token-refresh" | "weekly";
/** `trader.replay.types.ReplayStatus` (re-exported by the schemas). */
export type ReplayStatus = "queued" | "running" | "completed" | "failed" | "cancelled";
export type DataMode = "full" | "offline";
export type CatalystMode = "stored" | "unknown";
/** The weekly report's commentary outcome. */
export type CommentaryStatus = "ok" | "disabled" | "budget" | "rejected" | "error";

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
