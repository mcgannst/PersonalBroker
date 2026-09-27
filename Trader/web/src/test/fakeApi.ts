// A fake `ApiClient` for web tests: returns the fixtures (deep copies), records every call, and can be told
// to fail or to answer differently per method. No network.
import { ApiError, queryString, type ApiClient, type ApiMethod } from "../api/client";
import type {
  EventOut,
  IsoDate,
  Items,
  JobRunIn,
  JournalDayOut,
  JournalIn,
  KillSwitchesOut,
  LoginIn,
  ManualJob,
  PasswordChangeIn,
  ProposalOut,
  ReplayIn,
  ReplayOut,
  ResetIn,
  SettingOut,
  StrategyIn,
  StrategyOut,
  TotpConfirmIn,
  TotpDisableIn,
  TotpSetupIn,
} from "../api/types";
import * as fx from "./fixtures";

/** Methods that return a Promise (every method except the two URL builders). */
export type AsyncApiMethod = Exclude<ApiMethod, "exportTradesUrl" | "streamUrl">;
type ArgsOf<M extends ApiMethod> = Parameters<ApiClient[M]>;
type ResultOf<M extends AsyncApiMethod> = Awaited<ReturnType<ApiClient[M]>>;
export type FakeResponses = { [M in AsyncApiMethod]: ResultOf<M> };
type Responder<M extends AsyncApiMethod> = (...args: ArgsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>;

export type RecordedCall = [ApiMethod, unknown[]];

function clone<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

function notFound(what: string): ApiError {
  return new ApiError(404, "not_found", `${what} not found`);
}

const KNOWN_PROPOSALS: ProposalOut[] = [fx.pendingProposal, fx.decidedProposal, fx.stopProposal];

/** The default answer of every async method (the fixtures). */
export function defaultResponses(): FakeResponses {
  return {
    login: fx.sessionOut,
    logout: fx.okOut,
    me: fx.sessionOut,
    changePassword: fx.okOut,
    totpSetup: fx.totpSetupOut,
    totpConfirm: fx.okOut,
    totpDisable: fx.okOut,
    meta: fx.metaOut,
    health: fx.healthOut,
    dashboard: fx.dashboardOut,
    proposals: { items: [fx.pendingProposal] },
    proposal: fx.pendingProposal,
    approve: fx.decisionOut,
    reject: { ...fx.decisionOut, proposal: { ...fx.decisionOut.proposal, status: "rejected", order_id: null }, message: "Rejected" },
    candidates: fx.candidatesOut,
    orders: { items: fx.orders },
    fills: { items: fx.fills },
    positions: { items: [fx.openPosition] },
    position: fx.positionDetail,
    trades: { items: fx.trades },
    metrics: fx.metricsOut,
    equity: fx.equityOut,
    journal: { items: fx.journalDays },
    putJournal: fx.journalDays[0] as JournalDayOut,
    killswitches: fx.killswitchesOut,
    resetKillSwitch: fx.killswitchesOut,
    pause: fx.killswitchesOut,
    resume: fx.killswitchesOut,
    settings: fx.settingsOut,
    putSetting: fx.settingsItems[0] as SettingOut,
    strategies: fx.strategies,
    putStrategy: fx.orbSipStrategy,
    system: fx.systemOut,
    events: { items: fx.events },
    telegramTest: fx.telegramTestOut,
    jobs: { items: fx.jobRuns },
    runJob: fx.jobLaunchOut,
    putQuestradeToken: fx.tokenOut,
    watchlist: fx.watchlistOut,
    uploadWatchlist: fx.watchlistUploadOut,
    deleteWatchlist: fx.okOut,
    replayOptions: fx.replayOptions,
    replays: { items: fx.replaySummaries },
    replay: fx.replayCompleted,
    startReplay: fx.replayQueued,
    cancelReplay: { ...fx.replayRunning, cancel_requested: true },
    weeklyReport: fx.weeklyReportOk,
  };
}

const KNOWN_REPLAYS: ReplayOut[] = [fx.replayCompleted, fx.replayRunning, fx.replayQueued];

export class FakeApiClient implements ApiClient {
  /** Every call in order, as `[method, args]`. */
  readonly calls: RecordedCall[] = [];
  private readonly responses: FakeResponses;
  private readonly responders = new Map<AsyncApiMethod, (...args: never[]) => unknown>();
  private readonly failures = new Map<AsyncApiMethod, unknown>();

  constructor(overrides: Partial<FakeResponses> = {}) {
    this.responses = { ...defaultResponses(), ...overrides };
    // Lookups by id answer from the fixtures unless a test overrides them.
    if (!("proposal" in overrides)) {
      this.respond("proposal", (id) => {
        const p = KNOWN_PROPOSALS.find((x) => x.id === id);
        if (!p) throw notFound(`Proposal ${id}`);
        return p;
      });
    }
    if (!("replay" in overrides)) {
      this.respond("replay", (id) => {
        const r = KNOWN_REPLAYS.find((x) => x.id === id);
        if (!r) throw notFound(`Replay ${id}`);
        return r;
      });
    }
    if (!("position" in overrides)) {
      this.respond("position", (id) => {
        if (id === fx.positionDetail.position.id) return fx.positionDetail;
        if (id === fx.openPosition.id) {
          return { ...fx.positionDetail, position: fx.openPosition, trade: null, proposals: [], orders: [], fills: [], candles: [] };
        }
        throw notFound(`Position ${id}`);
      });
    }
  }

  /** Sets the value `method` resolves with from now on. */
  set<M extends AsyncApiMethod>(method: M, value: ResultOf<M>): this {
    this.responders.delete(method);
    (this.responses as Record<AsyncApiMethod, unknown>)[method] = value;
    return this;
  }

  /** Answers `method` with a function of its arguments (it may throw, or return a promise). */
  respond<M extends AsyncApiMethod>(method: M, fn: Responder<M>): this {
    this.responders.set(method, fn as unknown as (...args: never[]) => unknown);
    return this;
  }

  /** Makes every later call of `method` reject with `error` (until `succeed(method)`). */
  fail(method: AsyncApiMethod, error: unknown): this {
    this.failures.set(method, error);
    return this;
  }

  succeed(method: AsyncApiMethod): this {
    this.failures.delete(method);
    return this;
  }

  /** The argument lists of every call to `method`, in order. */
  callsTo(method: ApiMethod): unknown[][] {
    return this.calls.filter(([m]) => m === method).map(([, args]) => args);
  }

  private async call<M extends AsyncApiMethod>(method: M, args: ArgsOf<M>): Promise<ResultOf<M>> {
    this.calls.push([method, args]);
    if (this.failures.has(method)) throw this.failures.get(method);
    const responder = this.responders.get(method) as Responder<M> | undefined;
    const value = responder ? await responder(...args) : this.responses[method];
    return clone(value) as ResultOf<M>;
  }

  // auth
  login(body: LoginIn) {
    return this.call("login", [body]);
  }
  logout() {
    return this.call("logout", []);
  }
  me() {
    return this.call("me", []);
  }
  changePassword(body: PasswordChangeIn) {
    return this.call("changePassword", [body]);
  }
  totpSetup(body: TotpSetupIn) {
    return this.call("totpSetup", [body]);
  }
  totpConfirm(body: TotpConfirmIn) {
    return this.call("totpConfirm", [body]);
  }
  totpDisable(body: TotpDisableIn) {
    return this.call("totpDisable", [body]);
  }
  // health and meta
  meta() {
    return this.call("meta", []);
  }
  health() {
    return this.call("health", []);
  }
  // dashboard, proposals, trading
  dashboard() {
    return this.call("dashboard", []);
  }
  proposals(q: Parameters<ApiClient["proposals"]>[0]) {
    return this.call("proposals", [q]);
  }
  proposal(id: number) {
    return this.call("proposal", [id]);
  }
  approve(id: number) {
    return this.call("approve", [id]);
  }
  reject(id: number) {
    return this.call("reject", [id]);
  }
  candidates(date?: IsoDate) {
    return this.call("candidates", [date]);
  }
  orders(q: Parameters<ApiClient["orders"]>[0]) {
    return this.call("orders", [q]);
  }
  fills(q: Parameters<ApiClient["fills"]>[0]) {
    return this.call("fills", [q]);
  }
  positions(q: Parameters<ApiClient["positions"]>[0]) {
    return this.call("positions", [q]);
  }
  position(id: number) {
    return this.call("position", [id]);
  }
  trades(q: Parameters<ApiClient["trades"]>[0]) {
    return this.call("trades", [q]);
  }
  // performance and journal
  metrics(q: Parameters<ApiClient["metrics"]>[0]) {
    return this.call("metrics", [q]);
  }
  equity(q: Parameters<ApiClient["equity"]>[0]) {
    return this.call("equity", [q]);
  }
  exportTradesUrl(q: Parameters<ApiClient["exportTradesUrl"]>[0]): string {
    this.calls.push(["exportTradesUrl", [q]]);
    return `/api/export/trades.csv${queryString(q)}`;
  }
  journal(q: Parameters<ApiClient["journal"]>[0]) {
    return this.call("journal", [q]);
  }
  putJournal(date: IsoDate, body: JournalIn) {
    return this.call("putJournal", [date, body]);
  }
  // kill switches
  killswitches(): Promise<KillSwitchesOut> {
    return this.call("killswitches", []);
  }
  resetKillSwitch(sw: string, body: ResetIn) {
    return this.call("resetKillSwitch", [sw, body]);
  }
  pause() {
    return this.call("pause", []);
  }
  resume() {
    return this.call("resume", []);
  }
  // settings and strategies
  settings() {
    return this.call("settings", []);
  }
  putSetting(key: string, value: unknown) {
    return this.call("putSetting", [key, value]);
  }
  strategies(): Promise<Items<StrategyOut>> {
    return this.call("strategies", []);
  }
  putStrategy(key: string, body: StrategyIn) {
    return this.call("putStrategy", [key, body]);
  }
  // system
  system() {
    return this.call("system", []);
  }
  events(q: Parameters<ApiClient["events"]>[0]): Promise<Items<EventOut>> {
    return this.call("events", [q]);
  }
  telegramTest() {
    return this.call("telegramTest", []);
  }
  jobs(q: Parameters<ApiClient["jobs"]>[0]) {
    return this.call("jobs", [q]);
  }
  runJob(job: ManualJob, body: JobRunIn) {
    return this.call("runJob", [job, body]);
  }
  putQuestradeToken(token: string) {
    return this.call("putQuestradeToken", [token]);
  }
  // watchlist
  watchlist(date?: IsoDate) {
    return this.call("watchlist", [date]);
  }
  uploadWatchlist(file: File, opts: Parameters<ApiClient["uploadWatchlist"]>[1]) {
    return this.call("uploadWatchlist", [file, opts]);
  }
  deleteWatchlist(date: IsoDate) {
    return this.call("deleteWatchlist", [date]);
  }
  // replays and reports (P5)
  replayOptions() {
    return this.call("replayOptions", []);
  }
  replays(q: Parameters<ApiClient["replays"]>[0]) {
    return this.call("replays", [q]);
  }
  replay(id: number) {
    return this.call("replay", [id]);
  }
  startReplay(body: ReplayIn) {
    return this.call("startReplay", [body]);
  }
  cancelReplay(id: number) {
    return this.call("cancelReplay", [id]);
  }
  weeklyReport(week: IsoDate) {
    return this.call("weeklyReport", [week]);
  }
  // live updates
  streamUrl(): string {
    this.calls.push(["streamUrl", []]);
    return "/api/stream";
  }
}
