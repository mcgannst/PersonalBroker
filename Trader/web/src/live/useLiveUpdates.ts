// Stub (P4-T2): T12 replaces it with the EventSource hook that invalidates queries (TOPIC_KEYS).
export interface LiveUpdatesState {
  /** True after the stream's `hello`, false while (re)connecting. */
  connected: boolean;
}

export function useLiveUpdates(): LiveUpdatesState {
  return { connected: false };
}
