// The real `OptionsApiClient` (OPTSIM-T15): `fetch` with same-origin cookies, JSON bodies, the CSRF header on
// writes, and every failure turned into an `ApiError` whose message is safe to show. Same shape as `http.ts`
// (the route-contract test reads both files the same way).
//
// The session's CSRF token is held by the main HTTP client (`http.ts`), so this client is given two functions:
// `csrfToken()` reads it and `refreshCsrf()` asks the main client for a new one (`/auth/me`). A 403 `csrf`
// answer refreshes once and retries the request once.
import { ApiError, queryString } from "./client";
import type { OptionsApiClient } from "./optionsClient";
import type {
  FieldError,
  Items,
  OptAccountOut,
  OptActivityOut,
  OptChainOut,
  OptChainQuotesOut,
  OptOrderOut,
  OptPanelActionResultOut,
  OptPanelOut,
  OptPreviewOut,
  OptPromptOut,
  OptStrategyOut,
  OptStructureOut,
  SettingOut,
  SettingsOut,
} from "./types";

export interface OptionsHttpClientOptions {
  /** Prefix for every request; "" (same origin) by default. */
  baseUrl?: string;
  /** Called on any 401 (the shell clears auth and goes to /login?next=...). */
  onUnauthorized?: () => void;
  /** The session's CSRF token, from the main HTTP client. */
  csrfToken?: () => string | null;
  /** Fetches a fresh CSRF token into the main HTTP client (its `me()`). */
  refreshCsrf?: () => Promise<unknown>;
}

type Method = "GET" | "POST" | "PUT";

interface RequestSpec {
  method: Method;
  path: string;
  body?: unknown;
}

const NETWORK_MESSAGE = "Can't reach the server";
const WRITE_METHODS: ReadonlySet<Method> = new Set(["POST", "PUT"]);

/** One path segment, percent-encoded (`checkPath` refuses the dot segments this leaves as they are). */
function seg(value: string | number): string {
  return encodeURIComponent(String(value));
}

const DOT_SEGMENT = /^(?:\.|%2e){1,2}$/i;

/** Refuses a path with an empty or dot segment (from an id or key such as "", "." or ".."). */
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

export function createOptionsHttpClient(opts: OptionsHttpClientOptions = {}): OptionsApiClient {
  const base = (opts.baseUrl ?? "").replace(/\/+$/, "");

  const url = (path: string): string => `${base}/api${path}`;

  async function send(spec: RequestSpec): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    let body: BodyInit | undefined;
    if (spec.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(spec.body);
    }
    const csrf = opts.csrfToken?.() ?? null;
    if (WRITE_METHODS.has(spec.method) && csrf) headers["X-CSRF-Token"] = csrf;
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
    if (res.ok) return parse<T>(res);
    const err = await toApiError(res);
    if (err.status === 401) {
      opts.onUnauthorized?.();
    } else if (err.status === 403 && err.code === "csrf" && !retried && WRITE_METHODS.has(spec.method) && opts.refreshCsrf) {
      await opts.refreshCsrf();
      return request<T>(spec, true);
    }
    throw err;
  }

  const get = <T>(path: string) => request<T>({ method: "GET", path });
  const post = <T>(path: string, body?: unknown) => request<T>({ method: "POST", path, body });
  const put = <T>(path: string, body?: unknown) => request<T>({ method: "PUT", path, body });

  return {
    // account and positions
    optAccount: () => get<OptAccountOut>("/options/account"),
    optPositions: (q) => get<Items<OptStructureOut>>(`/options/positions${queryString(q)}`),

    // chain
    optChain: (q) => get<OptChainOut>(`/options/chain${queryString(q)}`),
    optChainQuotes: (q) => get<OptChainQuotesOut>(`/options/chain/quotes${queryString(q)}`),

    // orders
    optPreview: (body) => post<OptPreviewOut>("/options/orders/preview", body),
    optSubmit: (body) => post<OptOrderOut>("/options/orders", body),
    optOrders: (q) => get<Items<OptOrderOut>>(`/options/orders${queryString(q)}`),
    optCancel: (id) => post<OptOrderOut>(`/options/orders/${seg(id)}/cancel`),
    optReprice: (id, body) => post<OptOrderOut>(`/options/orders/${seg(id)}/reprice`, body),

    // activity and prompts
    optActivity: (q) => get<Items<OptActivityOut>>(`/options/activity${queryString(q)}`),
    optPrompts: (q) => get<Items<OptPromptOut>>(`/options/prompts${queryString(q)}`),
    optAnswerPrompt: (id, body) => post<OptPromptOut>(`/options/prompts/${seg(id)}/answer`, body),

    // strategies and their panels
    optStrategies: () => get<Items<OptStrategyOut>>("/options/strategies"),
    optPutStrategy: (key, body) => put<OptStrategyOut>(`/options/strategies/${seg(key)}`, body),
    optPanel: (key) => get<OptPanelOut>(`/options/strategies/${seg(key)}/panel`),
    optPanelAction: (key, body) => post<OptPanelActionResultOut>(`/options/strategies/${seg(key)}/actions`, body),

    // settings
    optSettings: () => get<SettingsOut>("/options/settings"),
    optPutSetting: (key, value) => put<SettingOut>(`/options/settings/${seg(key)}`, { value }),
  };
}
