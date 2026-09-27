// The real `ApiClient` (P4-T12): `fetch` with same-origin cookies, JSON bodies, the CSRF header on writes,
// and every failure turned into an `ApiError` whose message is safe to show.
//
// The client keeps the session's CSRF token itself: it is taken from every `SessionOut` the server returns
// (login and `/auth/me`) and forgotten on logout. A 403 `csrf` answer refreshes it through `/auth/me` once
// and retries the request once.
import { ApiError, queryString, type ApiClient } from "./client";
import type {
  CandidatesOut,
  DashboardOut,
  DecisionOut,
  EquityOut,
  EventOut,
  FieldError,
  FillOut,
  HealthOut,
  IsoDate,
  Items,
  JobLaunchOut,
  JobRunOut,
  JournalDayOut,
  KillSwitchesOut,
  MetaOut,
  MetricsOut,
  OkOut,
  OrderOut,
  PositionDetailOut,
  PositionOut,
  ProposalOut,
  SessionOut,
  SettingOut,
  SettingsOut,
  StrategyOut,
  SystemOut,
  TelegramTestOut,
  TokenOut,
  TotpSetupOut,
  TradeOut,
  WatchlistOut,
  WatchlistUploadOut,
} from "./types";

export interface HttpClientOptions {
  /** Prefix for every request; "" (same origin) by default. */
  baseUrl?: string;
  /** Called on any 401 except a failed login (the shell clears auth and goes to /login?next=...). */
  onUnauthorized?: () => void;
}

/** The HTTP client, plus access to the CSRF token it holds (for the auth context and tests). */
export interface HttpApiClient extends ApiClient {
  csrfToken(): string | null;
  setCsrfToken(token: string | null): void;
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

interface RequestSpec {
  method: Method;
  path: string;
  /** A JSON body, or a `FormData` sent as multipart. */
  body?: unknown;
  /** The login request: a 401 is a wrong password, not an expired session. */
  isLogin?: boolean;
}

const NETWORK_MESSAGE = "Can't reach the server";
const WRITE_METHODS: ReadonlySet<Method> = new Set(["POST", "PUT", "DELETE"]);

/** One path segment, percent-encoded (`checkPath` refuses the dot segments this leaves as they are). */
function seg(value: string | number): string {
  return encodeURIComponent(String(value));
}

// A dot segment, also percent-encoded: URL parsers resolve "." and ".." (and "%2e", ".%2E", ...) as "this"
// and "the parent" directory, so `/journal/..` would request `/api/` instead.
const DOT_SEGMENT = /^(?:\.|%2e){1,2}$/i;

/** Refuses a path with an empty or dot segment (from an id or date such as "", "." or ".."). */
function checkPath(path: string): void {
  const segments = (path.split("?", 1)[0] ?? "").split("/").slice(1);
  if (segments.some((s) => s === "" || DOT_SEGMENT.test(s))) {
    throw new ApiError(0, "bad_request", "That item can't be requested.");
  }
}

function genericMessage(status: number): string {
  if (status >= 500) return `The server had a problem (HTTP ${status}). Try again.`;
  return `The request failed (HTTP ${status}).`;
}

function isErrorEnvelope(value: unknown): value is { error: { code?: unknown; message?: unknown; fields?: unknown; request_id?: unknown } } {
  return typeof value === "object" && value !== null && typeof (value as { error?: unknown }).error === "object" && (value as { error?: unknown }).error !== null;
}

async function toApiError(res: Response): Promise<ApiError> {
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    parsed = null;
  }
  if (isErrorEnvelope(parsed)) {
    const e = parsed.error;
    const code = typeof e.code === "string" && e.code ? e.code : `http_${res.status}`;
    const message = typeof e.message === "string" && e.message ? e.message : genericMessage(res.status);
    const fields = Array.isArray(e.fields) ? (e.fields as FieldError[]) : null;
    const requestId = typeof e.request_id === "string" ? e.request_id : null;
    return new ApiError(res.status, code, message, fields, requestId);
  }
  // Never show a body we do not understand (it could be a proxy page or anything else).
  return new ApiError(res.status, `http_${res.status}`, genericMessage(res.status), null, res.headers.get("X-Request-ID"));
}

function isSession(value: unknown): value is SessionOut {
  return typeof value === "object" && value !== null && typeof (value as SessionOut).csrf_token === "string";
}

export function createHttpClient(opts: HttpClientOptions = {}): HttpApiClient {
  const base = (opts.baseUrl ?? "").replace(/\/+$/, "");
  let csrf: string | null = null;

  const url = (path: string): string => `${base}/api${path}`;

  async function send(spec: RequestSpec): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    let body: BodyInit | undefined;
    if (spec.body instanceof FormData) {
      body = spec.body;
    } else if (spec.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(spec.body);
    }
    if (WRITE_METHODS.has(spec.method) && !spec.isLogin && csrf) headers["X-CSRF-Token"] = csrf;
    const init: RequestInit = { method: spec.method, headers, credentials: "same-origin" };
    if (body !== undefined) init.body = body;
    try {
      return await fetch(url(spec.path), init);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      throw new ApiError(0, "network", NETWORK_MESSAGE);
    }
  }

  async function parse<T>(res: Response): Promise<T> {
    if (res.status === 204) return undefined as T;
    try {
      return (await res.json()) as T;
    } catch {
      throw new ApiError(res.status, "bad_response", "The server sent an unreadable answer.");
    }
  }

  async function request<T>(spec: RequestSpec, retried = false): Promise<T> {
    checkPath(spec.path);
    const res = await send(spec);
    if (res.ok) {
      const data = await parse<T>(res);
      if (isSession(data)) csrf = data.csrf_token;
      return data;
    }
    const err = await toApiError(res);
    if (err.status === 401 && !spec.isLogin) {
      csrf = null;
      opts.onUnauthorized?.();
    } else if (err.status === 403 && err.code === "csrf" && !retried && WRITE_METHODS.has(spec.method)) {
      await request<SessionOut>({ method: "GET", path: "/auth/me" }, true);
      return request<T>(spec, true);
    }
    throw err;
  }

  const get = <T>(path: string) => request<T>({ method: "GET", path });
  const post = <T>(path: string, body?: unknown) => request<T>({ method: "POST", path, body });
  const put = <T>(path: string, body?: unknown) => request<T>({ method: "PUT", path, body });
  const del = <T>(path: string) => request<T>({ method: "DELETE", path });

  return {
    csrfToken: () => csrf,
    setCsrfToken: (token) => {
      csrf = token;
    },

    // auth
    login: (body) => request<SessionOut>({ method: "POST", path: "/auth/login", body, isLogin: true }),
    logout: async () => {
      try {
        return await post<OkOut>("/auth/logout");
      } finally {
        csrf = null;
      }
    },
    me: () => get<SessionOut>("/auth/me"),
    changePassword: (body) => put<OkOut>("/auth/password", body),
    totpSetup: (body) => post<TotpSetupOut>("/auth/totp/setup", body),
    totpConfirm: (body) => post<OkOut>("/auth/totp/confirm", body),
    totpDisable: (body) => post<OkOut>("/auth/totp/disable", body),

    // health and meta
    meta: () => get<MetaOut>("/meta"),
    health: () => get<HealthOut>("/health"),

    // dashboard and proposals
    dashboard: () => get<DashboardOut>("/dashboard"),
    proposals: (q) => get<Items<ProposalOut>>(`/proposals${queryString(q)}`),
    proposal: (id) => get<ProposalOut>(`/proposals/${seg(id)}`),
    approve: (id) => post<DecisionOut>(`/proposals/${seg(id)}/approve`),
    reject: (id) => post<DecisionOut>(`/proposals/${seg(id)}/reject`),

    // trading reads
    candidates: (date?: IsoDate) => get<CandidatesOut>(`/candidates${queryString({ date })}`),
    orders: (q) => get<Items<OrderOut>>(`/orders${queryString(q)}`),
    fills: (q) => get<Items<FillOut>>(`/fills${queryString(q)}`),
    positions: (q) => get<Items<PositionOut>>(`/positions${queryString(q)}`),
    position: (id) => get<PositionDetailOut>(`/positions/${seg(id)}`),
    trades: (q) => get<Items<TradeOut>>(`/trades${queryString(q)}`),

    // performance and journal
    metrics: (q) => get<MetricsOut>(`/metrics${queryString(q)}`),
    equity: (q) => get<EquityOut>(`/equity${queryString(q)}`),
    exportTradesUrl: (q) => url(`/export/trades.csv${queryString(q)}`),
    journal: (q) => get<Items<JournalDayOut>>(`/journal${queryString(q)}`),
    putJournal: (date, body) => put<JournalDayOut>(`/journal/${seg(date)}`, body),

    // kill switches
    killswitches: () => get<KillSwitchesOut>("/killswitch"),
    resetKillSwitch: (sw, body) => post<KillSwitchesOut>(`/killswitch/${seg(sw)}/reset`, body),
    pause: () => post<KillSwitchesOut>("/killswitch/pause"),
    resume: () => post<KillSwitchesOut>("/killswitch/resume"),

    // settings and strategies
    settings: () => get<SettingsOut>("/settings"),
    putSetting: (key, value) => put<SettingOut>(`/settings/${seg(key)}`, { value }),
    strategies: () => get<Items<StrategyOut>>("/strategies"),
    putStrategy: (key, body) => put<StrategyOut>(`/strategies/${seg(key)}`, body),

    // system, events, jobs, credentials
    system: () => get<SystemOut>("/system"),
    events: (q) => get<Items<EventOut>>(`/events${queryString(q)}`),
    telegramTest: () => post<TelegramTestOut>("/system/telegram-test"),
    jobs: (q) => get<Items<JobRunOut>>(`/jobs${queryString(q)}`),
    runJob: (job, body) => post<JobLaunchOut>(`/jobs/${seg(job)}/run`, body),
    putQuestradeToken: (token) => post<TokenOut>("/credentials/questrade", { refresh_token: token }),

    // watchlist
    watchlist: async (date?: IsoDate) => {
      try {
        return await get<WatchlistOut>(`/watchlist${queryString({ date })}`);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    uploadWatchlist: (file, o) => {
      const form = new FormData();
      form.append("file", file, file.name);
      if (o.date) form.append("date", o.date);
      if (o.runNightly !== undefined) form.append("run_nightly", o.runNightly ? "true" : "false");
      return post<WatchlistUploadOut>("/watchlist", form);
    },
    deleteWatchlist: (date) => del<OkOut>(`/watchlist/${seg(date)}`),

    // live updates
    streamUrl: () => url("/stream"),
  };
}
