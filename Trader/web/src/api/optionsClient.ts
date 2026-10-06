// The options simulation's API contract (OPTSIM-T15): one method per /api/options route (task plan §3.9).
// It is a separate client with its own provider, so the stock `ApiClient` and its fake stay as they are.
// `createOptionsHttpClient` (src/api/optionsHttp.ts) is the real implementation; `FakeOptionsApiClient`
// (src/test/optionsFakeApi.ts) is the one tests use. Every method rejects with an `ApiError`.
import { createContext, createElement, useContext, type ReactNode } from "react";

import type {
  IsoDate,
  IsoTime,
  Items,
  OptAccountOut,
  OptActivityOut,
  OptChainOut,
  OptChainQuotesOut,
  OptOrderIn,
  OptOrderOut,
  OptPanelActionIn,
  OptPanelActionResultOut,
  OptPanelOut,
  OptPreviewOut,
  OptPromptAnswerIn,
  OptPromptOut,
  OptRepriceIn,
  OptStrategyOut,
  OptStructureOut,
  SettingOut,
  SettingsOut,
  StrategyIn,
} from "./types";

// ---------------------------------------------------------------- query shapes (query-string parameters)

export interface OptPositionsQuery {
  state?: "open" | "closed";
  limit?: number;
}

export interface OptChainQuery {
  underlying: string;
}

export interface OptChainQuotesQuery {
  underlying: string;
  expiry: IsoDate;
}

export interface OptOrdersQuery {
  status?: "working" | "history";
  limit?: number;
}

export interface OptActivityQuery {
  limit?: number;
  before?: IsoTime;
}

export interface OptPromptsQuery {
  status?: "pending" | "all";
}

export interface OptionsApiClient {
  // account and positions
  optAccount(): Promise<OptAccountOut>;
  optPositions(q: OptPositionsQuery): Promise<Items<OptStructureOut>>;
  // chain
  optChain(q: OptChainQuery): Promise<OptChainOut>;
  optChainQuotes(q: OptChainQuotesQuery): Promise<OptChainQuotesOut>;
  // orders
  optPreview(body: OptOrderIn): Promise<OptPreviewOut>;
  optSubmit(body: OptOrderIn): Promise<OptOrderOut>;
  optOrders(q: OptOrdersQuery): Promise<Items<OptOrderOut>>;
  optCancel(id: number): Promise<OptOrderOut>;
  optReprice(id: number, body: OptRepriceIn): Promise<OptOrderOut>;
  // activity and prompts
  optActivity(q: OptActivityQuery): Promise<Items<OptActivityOut>>;
  optPrompts(q: OptPromptsQuery): Promise<Items<OptPromptOut>>;
  optAnswerPrompt(id: number, body: OptPromptAnswerIn): Promise<OptPromptOut>;
  // strategies and their panels
  optStrategies(): Promise<Items<OptStrategyOut>>;
  optPutStrategy(key: string, body: StrategyIn): Promise<OptStrategyOut>;
  optPanel(key: string): Promise<OptPanelOut>;
  optPanelAction(key: string, body: OptPanelActionIn): Promise<OptPanelActionResultOut>;
  // settings
  optSettings(): Promise<SettingsOut>;
  optPutSetting(key: string, value: unknown): Promise<SettingOut>;
}

/** Every `OptionsApiClient` method name (kept in step with the interface by a type check in `optionsFakeApi.ts`). */
export const OPTIONS_API_METHODS = [
  "optAccount",
  "optPositions",
  "optChain",
  "optChainQuotes",
  "optPreview",
  "optSubmit",
  "optOrders",
  "optCancel",
  "optReprice",
  "optActivity",
  "optPrompts",
  "optAnswerPrompt",
  "optStrategies",
  "optPutStrategy",
  "optPanel",
  "optPanelAction",
  "optSettings",
  "optPutSetting",
] as const satisfies readonly (keyof OptionsApiClient)[];

export type OptionsApiMethod = (typeof OPTIONS_API_METHODS)[number];

// ---------------------------------------------------------------- query keys and refresh intervals

/** Query keys of the Options page. Everything is under `["options"]`, so one invalidation refreshes the page. */
export const oqk = {
  all: () => ["options"] as const,
  account: () => ["options", "account"] as const,
  positions: (q: OptPositionsQuery) => ["options", "positions", q] as const,
  chain: (underlying: string) => ["options", "chain", underlying] as const,
  chainQuotes: (q: OptChainQuotesQuery) => ["options", "chainQuotes", q] as const,
  orders: (q: OptOrdersQuery) => ["options", "orders", q] as const,
  activity: (q: OptActivityQuery) => ["options", "activity", q] as const,
  prompts: (q: OptPromptsQuery) => ["options", "prompts", q] as const,
  strategies: () => ["options", "strategies"] as const,
  panel: (key: string) => ["options", "panel", key] as const,
  settings: () => ["options", "settings"] as const,
};

/** Account, positions, orders and the open chain's quotes. react-query pauses the polling while the page is hidden. */
export const OPT_FAST_MS = 5_000;
/** Everything else. */
export const OPT_SLOW_MS = 30_000;

// ---------------------------------------------------------------- provider

export const OptionsApiContext = createContext<OptionsApiClient | null>(null);

export function OptionsApiProvider({ client, children }: { client: OptionsApiClient; children?: ReactNode }) {
  return createElement(OptionsApiContext.Provider, { value: client }, children);
}

/** The `OptionsApiClient` from the nearest `OptionsApiProvider`; throws when there is none (a wiring bug). */
export function useOptionsApi(): OptionsApiClient {
  const client = useContext(OptionsApiContext);
  if (!client) throw new Error("useOptionsApi() needs an <OptionsApiProvider> above it");
  return client;
}
