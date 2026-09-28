// The web app's API contract: one method per /api route (P4 plan, task P4-T2).
// `createHttpClient` (src/api/http.ts, T12) is the real implementation; `FakeApiClient`
// (src/test/fakeApi.ts) is the one tests use.
import { createContext, createElement, useContext, type ReactNode } from "react";

import type {
  CandidatesOut,
  DashboardOut,
  DecisionDayOut,
  DecisionDaysOut,
  DecisionOut,
  DecisionOutcome,
  DecisionStage,
  EquityOut,
  EventOut,
  FieldError,
  FillOut,
  HealthOut,
  IsoDate,
  Items,
  JobLaunchOut,
  JobRunIn,
  JobRunOut,
  JournalDayOut,
  JournalIn,
  KillSwitchesOut,
  LoginIn,
  ManualJob,
  MetaOut,
  MetricsOut,
  OkOut,
  OrderOut,
  PasswordChangeIn,
  PositionDetailOut,
  PositionOut,
  ProposalOut,
  ReplayIn,
  ReplayOptionsOut,
  ReplayOut,
  ReplaySummaryOut,
  ResetIn,
  SessionOut,
  SettingOut,
  SettingsOut,
  StrategyIn,
  StrategyOut,
  SystemOut,
  TelegramTestOut,
  TokenOut,
  TotpConfirmIn,
  TotpDisableIn,
  TotpSetupIn,
  TotpSetupOut,
  TradeOut,
  WatchlistOut,
  WatchlistUploadOut,
  WeeklyReportOut,
} from "./types";

/**
 * An API failure. `status` is the HTTP status (0 for a network failure), `code` the server's error code
 * (`ErrorBody.code`, or `network`), `message` text that is safe to show to Stephen.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields?: FieldError[];
  readonly requestId?: string;

  constructor(status: number, code: string, message: string, fields?: FieldError[] | null, requestId?: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    if (fields) this.fields = fields;
    if (requestId) this.requestId = requestId;
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

// ---------------------------------------------------------------- query shapes (query-string parameters)

export interface ProposalsQuery {
  status?: "pending" | "all";
  date?: IsoDate;
  limit?: number;
}

export interface OrdersQuery {
  date?: IsoDate;
  status?: string;
  limit?: number;
}

export interface FillsQuery {
  date?: IsoDate;
  limit?: number;
}

export interface PositionsQuery {
  status?: "open" | "closed" | "all";
  date?: IsoDate;
}

/** `run` is `live` (the default) or a run id, as a string. */
export interface RunRangeQuery {
  run?: string;
  from?: IsoDate;
  to?: IsoDate;
}

export interface TradesQuery extends RunRangeQuery {
  limit?: number;
  offset?: number;
}

export type MetricsQuery = RunRangeQuery;
export type EquityQuery = RunRangeQuery;
export type ExportQuery = RunRangeQuery;

export interface JournalQuery {
  from?: IsoDate;
  to?: IsoDate;
}

export interface EventsQuery {
  since?: number;
  before?: number;
  level?: string;
  source?: string;
  limit?: number;
}

export interface JobsQuery {
  job?: string;
  limit?: number;
}

export interface WatchlistUploadOptions {
  date?: IsoDate;
  runNightly?: boolean;
}

export interface ReplaysQuery {
  limit?: number;
}

/** The decision log (P6-T12). Without `run_id` the server serves only a live run (never a replay's rows). */
export interface DecisionDaysQuery {
  limit?: number;
  run_id?: number;
}

export interface DecisionDayQuery {
  date: IsoDate;
  run_id?: number;
  stage?: DecisionStage;
  outcome?: DecisionOutcome;
  ticker?: string;
  limit?: number;
  offset?: number;
}

export interface DecisionsCsvQuery {
  date: IsoDate;
  run_id?: number;
}

/** One method per /api route (SPEC §11, P4 plan T3–T11). Every method rejects with an `ApiError`. */
export interface ApiClient {
  // auth (T4)
  login(body: LoginIn): Promise<SessionOut>;
  logout(): Promise<OkOut>;
  me(): Promise<SessionOut>;
  changePassword(body: PasswordChangeIn): Promise<OkOut>;
  totpSetup(body: TotpSetupIn): Promise<TotpSetupOut>;
  totpConfirm(body: TotpConfirmIn): Promise<OkOut>;
  totpDisable(body: TotpDisableIn): Promise<OkOut>;
  // health and meta (T3)
  meta(): Promise<MetaOut>;
  health(): Promise<HealthOut>;
  // dashboard and trading reads (T5)
  dashboard(): Promise<DashboardOut>;
  // proposals (T6)
  proposals(q: ProposalsQuery): Promise<Items<ProposalOut>>;
  proposal(id: number): Promise<ProposalOut>;
  approve(id: number): Promise<DecisionOut>;
  reject(id: number): Promise<DecisionOut>;
  // trading reads (T5)
  candidates(date?: IsoDate): Promise<CandidatesOut>;
  orders(q: OrdersQuery): Promise<Items<OrderOut>>;
  fills(q: FillsQuery): Promise<Items<FillOut>>;
  positions(q: PositionsQuery): Promise<Items<PositionOut>>;
  position(id: number): Promise<PositionDetailOut>;
  trades(q: TradesQuery): Promise<Items<TradeOut>>;
  // performance and journal (T7)
  metrics(q: MetricsQuery): Promise<MetricsOut>;
  equity(q: EquityQuery): Promise<EquityOut>;
  /** Same-origin URL of `GET /api/export/trades.csv` with the filters (a link, not a fetch). */
  exportTradesUrl(q: ExportQuery): string;
  journal(q: JournalQuery): Promise<Items<JournalDayOut>>;
  putJournal(date: IsoDate, body: JournalIn): Promise<JournalDayOut>;
  // kill switches (T6)
  killswitches(): Promise<KillSwitchesOut>;
  resetKillSwitch(sw: string, body: ResetIn): Promise<KillSwitchesOut>;
  pause(): Promise<KillSwitchesOut>;
  resume(): Promise<KillSwitchesOut>;
  // settings and strategies (T8)
  settings(): Promise<SettingsOut>;
  putSetting(key: string, value: unknown): Promise<SettingOut>;
  strategies(): Promise<Items<StrategyOut>>;
  putStrategy(key: string, body: StrategyIn): Promise<StrategyOut>;
  // system, events, jobs, credentials (T9)
  system(): Promise<SystemOut>;
  events(q: EventsQuery): Promise<Items<EventOut>>;
  telegramTest(): Promise<TelegramTestOut>;
  jobs(q: JobsQuery): Promise<Items<JobRunOut>>;
  runJob(job: ManualJob, body: JobRunIn): Promise<JobLaunchOut>;
  putQuestradeToken(token: string): Promise<TokenOut>;
  // watchlist (T10); `watchlist` resolves null when the server answers 404 (none stored)
  watchlist(date?: IsoDate): Promise<WatchlistOut | null>;
  uploadWatchlist(file: File, opts: WatchlistUploadOptions): Promise<WatchlistUploadOut>;
  deleteWatchlist(date: IsoDate): Promise<OkOut>;
  // replays (P5-T7)
  replayOptions(): Promise<ReplayOptionsOut>;
  replays(q: ReplaysQuery): Promise<Items<ReplaySummaryOut>>;
  replay(id: number): Promise<ReplayOut>;
  startReplay(body: ReplayIn): Promise<ReplayOut>;
  cancelReplay(id: number): Promise<ReplayOut>;
  // reports (P5-T12); `weeklyReport` resolves null when the server answers 404 (no report for that week)
  weeklyReport(week: IsoDate): Promise<WeeklyReportOut | null>;
  // decision log (P6-T12); `decisionDay` resolves null when the server answers 404 (no rows that day)
  decisionDays(q: DecisionDaysQuery): Promise<DecisionDaysOut>;
  decisionDay(q: DecisionDayQuery): Promise<DecisionDayOut | null>;
  /** Same-origin URL of `GET /api/export/decisions.csv` (a download link, not a fetch). */
  decisionsCsvUrl(q: DecisionsCsvQuery): string;
  // live updates (T11)
  /** Same-origin URL of `GET /api/stream` (for `EventSource`). */
  streamUrl(): string;
}

/** Every `ApiClient` method name (kept in step with the interface by a type check in `fakeApi.ts`). */
export const API_METHODS = [
  "login",
  "logout",
  "me",
  "changePassword",
  "totpSetup",
  "totpConfirm",
  "totpDisable",
  "meta",
  "health",
  "dashboard",
  "proposals",
  "proposal",
  "approve",
  "reject",
  "candidates",
  "orders",
  "fills",
  "positions",
  "position",
  "trades",
  "metrics",
  "equity",
  "exportTradesUrl",
  "journal",
  "putJournal",
  "killswitches",
  "resetKillSwitch",
  "pause",
  "resume",
  "settings",
  "putSetting",
  "strategies",
  "putStrategy",
  "system",
  "events",
  "telegramTest",
  "jobs",
  "runJob",
  "putQuestradeToken",
  "watchlist",
  "uploadWatchlist",
  "deleteWatchlist",
  "replayOptions",
  "replays",
  "replay",
  "startReplay",
  "cancelReplay",
  "weeklyReport",
  "decisionDays",
  "decisionDay",
  "decisionsCsvUrl",
  "streamUrl",
] as const satisfies readonly (keyof ApiClient)[];

export type ApiMethod = (typeof API_METHODS)[number];

/**
 * Builds a query string from defined values only (`undefined`, `null` and `""` are dropped), in key order.
 * Returns "" or "?a=1&b=2". Shared by the HTTP client and the fake so both build the same URLs.
 */
export function queryString(q: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(q)) {
    if (value === undefined || value === null || value === "") continue;
    params.append(key, String(value));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

export const ApiContext = createContext<ApiClient | null>(null);

export function ApiProvider({ client, children }: { client: ApiClient; children?: ReactNode }) {
  return createElement(ApiContext.Provider, { value: client }, children);
}

/** The `ApiClient` from the nearest `ApiProvider`; throws when there is none (a wiring bug). */
export function useApi(): ApiClient {
  const client = useContext(ApiContext);
  if (!client) throw new Error("useApi() needs an <ApiProvider> above it");
  return client;
}
