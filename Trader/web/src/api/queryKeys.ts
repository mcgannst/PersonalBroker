// TanStack Query keys. Every key is an array whose first element is its resource name, so
// `invalidateQueries({ queryKey: [name] })` refreshes every query of that resource.
import type {
  EventsQuery,
  JobsQuery,
  JournalQuery,
  MetricsQuery,
  EquityQuery,
  PositionsQuery,
  ProposalsQuery,
  ReplaysQuery,
  TradesQuery,
} from "./client";
import type { IsoDate, Topic } from "./types";

export const qk = {
  dashboard: () => ["dashboard"] as const,
  proposals: (q: ProposalsQuery = {}) => ["proposals", q] as const,
  proposal: (id: number) => ["proposal", id] as const,
  candidates: (date?: IsoDate) => ["candidates", date ?? null] as const,
  positions: (q: PositionsQuery = {}) => ["positions", q] as const,
  position: (id: number) => ["position", id] as const,
  trades: (q: TradesQuery = {}) => ["trades", q] as const,
  metrics: (q: MetricsQuery = {}) => ["metrics", q] as const,
  equity: (q: EquityQuery = {}) => ["equity", q] as const,
  journal: (q: JournalQuery = {}) => ["journal", q] as const,
  killswitches: () => ["killswitches"] as const,
  settings: () => ["settings"] as const,
  strategies: () => ["strategies"] as const,
  system: () => ["system"] as const,
  events: (q: EventsQuery = {}) => ["events", q] as const,
  jobs: (q: JobsQuery = {}) => ["jobs", q] as const,
  watchlist: (date?: IsoDate) => ["watchlist", date ?? null] as const,
  me: () => ["me"] as const,
  meta: () => ["meta"] as const,
  // Phase 5
  replayOptions: () => ["replayOptions"] as const,
  replays: (q: ReplaysQuery = {}) => ["replays", q] as const,
  replay: (id: number) => ["replay", id] as const,
  weeklyReport: (week: IsoDate) => ["weeklyReport", week] as const,
};

/** The resource-name prefixes each SSE `invalidate` topic refreshes. */
export const TOPIC_KEYS: Record<Topic, readonly string[]> = {
  proposals: ["dashboard", "proposals", "proposal", "position"],
  orders: ["dashboard", "positions", "position", "trades", "metrics", "equity"],
  fills: ["dashboard", "positions", "position", "trades", "metrics", "equity"],
  positions: ["dashboard", "positions", "position", "trades", "metrics", "equity"],
  // A closed trade also changes the journal day's trade count and realized P&L (`JournalDayOut`).
  trades: ["dashboard", "positions", "position", "trades", "metrics", "equity", "journal"],
  candidates: ["dashboard", "candidates"],
  killswitch: ["dashboard", "killswitches"],
  events: ["dashboard", "events", "system"],
  journal: ["journal", "metrics"],
  jobs: ["dashboard", "jobs", "system", "candidates"],
  settings: ["settings", "dashboard"],
  strategies: ["strategies", "dashboard"],
  system: ["system", "dashboard"],
  replays: ["replays", "replay", "replayOptions"],
  reports: ["weeklyReport"],
};

/** The distinct prefixes to invalidate for a set of topics. */
export function prefixesFor(topics: readonly Topic[]): string[] {
  const out = new Set<string>();
  for (const topic of topics) for (const prefix of TOPIC_KEYS[topic] ?? []) out.add(prefix);
  return [...out];
}
