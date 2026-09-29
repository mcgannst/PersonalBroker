// Live updates (P4-T12, SPEC §12 "Live updates use SSE"; live dashboard plan S9, DB-T11). `LiveUpdatesProvider`
// (mounted once by the shell while logged in) opens ONE `EventSource` on `/api/stream` and turns its messages
// into query invalidations:
//   - `invalidate` {topics}  -> invalidate every prefix in `TOPIC_KEYS[topic]`
//   - `events` {items}       -> invalidate `events` and `dashboard`
//   - `hello`                -> connected
// Invalidations of the `dashboard` and `system` prefixes (the Dashboard's `live` and the Control page's
// `control` queries live under them; the `marks` topic changes every 2 s in the session) are throttled to at
// most one per `LIVE_THROTTLE_MS`: the first runs at once, the rest of the window collapse into ONE trailing
// invalidation at its end (never dropped). Other prefixes are invalidated at once.
// On an error it shows "not connected" and lets the browser reconnect by itself (the server sends
// `retry: 3000`). When the browser gives up (readyState CLOSED, for example after a 401 on reconnect) it asks
// `/auth/me`: a 401 logs out, anything else reopens the stream after a short delay. While not connected the
// `dashboard` and `system` queries default to `refetchInterval` 15 s (`setQueryDefaults`), so the pages stay
// fresh without the stream; the default is cleared on reconnect, and losing the stream refetches both once.
//
// `useLiveUpdates()` only reads the provider's state, so any page may call it (for example to show the live
// dot) without opening a second stream. Outside a provider it reports `connected: false`.
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { createContext, createElement, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { isApiError, useApi } from "../api/client";
import { prefixesFor, qk, TOPIC_KEYS } from "../api/queryKeys";
import type { StreamInvalidate, Topic } from "../api/types";

export interface LiveUpdatesState {
  /** True after the stream's `hello`, false while (re)connecting. */
  connected: boolean;
}

/** The part of the browser's `EventSource` the provider uses (a fake in tests). */
export interface EventSourceLike {
  readonly readyState: number;
  onerror: ((ev: Event) => void) | null;
  addEventListener(type: string, listener: (ev: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

/** How often the dashboard and control queries are refetched while the stream is down. */
export const DISCONNECTED_REFETCH_MS = 15_000;
/** At most one invalidation of a throttled prefix per this many ms (plan S9). */
export const LIVE_THROTTLE_MS = 2_000;
/** The query prefixes whose SSE invalidations are throttled and which poll while the stream is down. */
export const LIVE_PREFIXES: readonly string[] = ["dashboard", "system"];
/** How long to wait before reopening a stream the browser gave up on. */
export const REOPEN_DELAY_MS = 5_000;
const CLOSED = 2;

const LiveContext = createContext<LiveUpdatesState>({ connected: false });

function defaultFactory(): EventSourceFactory | null {
  const ES = (globalThis as { EventSource?: new (url: string) => EventSourceLike }).EventSource;
  if (typeof ES !== "function") return null;
  return (url) => new ES(url);
}

function isTopic(value: unknown): value is Topic {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(TOPIC_KEYS, value);
}

function parse(ev: MessageEvent): unknown {
  try {
    return typeof ev.data === "string" ? JSON.parse(ev.data) : null;
  } catch {
    return null;
  }
}

/** Sets the `refetchInterval` default of the `dashboard` and `system` queries and applies it to those mounted now. */
function setLivePolling(queryClient: QueryClient, refetchInterval: number | false): void {
  for (const prefix of LIVE_PREFIXES) {
    const queryKey = [prefix];
    queryClient.setQueryDefaults(queryKey, { refetchInterval });
    for (const query of queryClient.getQueryCache().findAll({ queryKey })) {
      for (const observer of query.observers) {
        if (observer.options.refetchInterval !== refetchInterval) observer.setOptions({ ...observer.options, refetchInterval });
      }
    }
  }
}

export interface Throttle {
  /** Runs `key` now when its window is closed, else once at the end of the window. */
  call(key: string): void;
  /** Forgets every window and pending call (the provider unmounted). */
  cancel(): void;
}

/**
 * A per-key leading-and-trailing throttle: the first call runs at once and opens a `waitMs` window; calls inside
 * the window mark it pending; when the window ends a pending call runs (and opens a new window), so the last
 * update is never dropped and a key runs at most once per window.
 */
export function createThrottle(run: (key: string) => void, waitMs: number): Throttle {
  const windows = new Map<string, { timer: ReturnType<typeof setTimeout>; pending: boolean }>();
  const open = (key: string) => {
    windows.set(key, { timer: setTimeout(() => close(key), waitMs), pending: false });
  };
  const close = (key: string) => {
    const w = windows.get(key);
    windows.delete(key);
    if (w?.pending) {
      run(key);
      open(key);
    }
  };
  return {
    call(key) {
      const w = windows.get(key);
      if (w) {
        w.pending = true;
        return;
      }
      run(key);
      open(key);
    },
    cancel() {
      for (const w of windows.values()) clearTimeout(w.timer);
      windows.clear();
    },
  };
}

export interface LiveUpdatesProviderProps {
  children?: ReactNode;
  /** Called when the stream failed and `/auth/me` answered 401 (the shell clears the session). */
  onUnauthorized?: () => void;
  /** Opens the stream; the browser's `EventSource` by default. */
  createEventSource?: EventSourceFactory;
}

export function LiveUpdatesProvider({ children, onUnauthorized, createEventSource }: LiveUpdatesProviderProps) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  // Bumped to reopen the stream after the browser gave up on it.
  const [generation, setGeneration] = useState(0);
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  // One throttle for the provider's life (a reopened stream keeps a pending trailing invalidation).
  const throttle = useMemo(
    () => createThrottle((prefix) => void queryClient.invalidateQueries({ queryKey: [prefix] }), LIVE_THROTTLE_MS),
    [queryClient],
  );
  useEffect(() => () => throttle.cancel(), [throttle]);

  useEffect(() => {
    const factory = createEventSource ?? defaultFactory();
    if (!factory) return undefined;
    let active = true;
    let reopenTimer: ReturnType<typeof setTimeout> | undefined;
    const es = factory(api.streamUrl());

    const invalidate = (prefixes: readonly string[]) => {
      for (const prefix of prefixes) {
        if (LIVE_PREFIXES.includes(prefix)) throttle.call(prefix);
        else void queryClient.invalidateQueries({ queryKey: [prefix] });
      }
    };

    es.addEventListener("hello", () => {
      if (active) setConnected(true);
    });
    es.addEventListener("invalidate", (ev) => {
      const data = parse(ev) as Partial<StreamInvalidate> | null;
      const topics = Array.isArray(data?.topics) ? data.topics.filter(isTopic) : [];
      if (active && topics.length) invalidate(prefixesFor(topics));
    });
    es.addEventListener("events", () => {
      if (active) invalidate(["events", "dashboard"]);
    });
    es.onerror = () => {
      if (!active) return;
      setConnected(false);
      if (es.readyState !== CLOSED) return; // the browser is reconnecting by itself
      es.close();
      api.me().then(
        () => {
          if (active) reopenTimer = setTimeout(() => setGeneration((g) => g + 1), REOPEN_DELAY_MS);
        },
        (err: unknown) => {
          if (!active) return;
          if (isApiError(err) && err.status === 401) onUnauthorizedRef.current?.();
          else reopenTimer = setTimeout(() => setGeneration((g) => g + 1), REOPEN_DELAY_MS);
        },
      );
    };

    return () => {
      active = false;
      if (reopenTimer !== undefined) clearTimeout(reopenTimer);
      es.onerror = null;
      es.close();
      setConnected(false);
    };
  }, [api, queryClient, createEventSource, generation, throttle]);

  // While the stream is down the Dashboard and Control queries poll: a query default, so the pages need no code
  // for it. Observers already mounted only read defaults when their page re-renders, so they are updated too.
  // Losing the stream also refetches both once (they may have missed updates).
  const wasConnected = useRef(false);
  useEffect(() => {
    setLivePolling(queryClient, connected ? false : DISCONNECTED_REFETCH_MS);
    if (wasConnected.current && !connected) {
      for (const queryKey of [qk.dashboard(), qk.system()]) void queryClient.invalidateQueries({ queryKey });
    }
    wasConnected.current = connected;
  }, [connected, queryClient]);

  useEffect(() => () => setLivePolling(queryClient, false), [queryClient]);

  const value = useMemo(() => ({ connected }), [connected]);
  return createElement(LiveContext.Provider, { value }, children);
}

/** The live-stream state from the nearest `LiveUpdatesProvider` (not connected outside one). */
export function useLiveUpdates(): LiveUpdatesState {
  return useContext(LiveContext);
}
