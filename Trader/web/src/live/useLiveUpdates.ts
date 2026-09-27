// Live updates (P4-T12, SPEC §12 "Live updates use SSE"). `LiveUpdatesProvider` (mounted once by the shell
// while logged in) opens ONE `EventSource` on `/api/stream` and turns its messages into query invalidations:
//   - `invalidate` {topics}  -> invalidate every prefix in `TOPIC_KEYS[topic]`
//   - `events` {items}       -> invalidate `events` and `dashboard`
//   - `hello`                -> connected
// On an error it shows "not connected" and lets the browser reconnect by itself (the server sends
// `retry: 3000`). When the browser gives up (readyState CLOSED, for example after a 401 on reconnect) it asks
// `/auth/me`: a 401 logs out, anything else reopens the stream after a short delay. While not connected the
// dashboard query defaults to `refetchInterval` 15 s (`setQueryDefaults`), so the page stays fresh without
// the stream; the default is cleared on reconnect.
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

/** How often the dashboard is refetched while the stream is down. */
export const DISCONNECTED_REFETCH_MS = 15_000;
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

/** Sets the dashboard query's `refetchInterval` default and applies it to the dashboard queries mounted now. */
function setDashboardPolling(queryClient: QueryClient, refetchInterval: number | false): void {
  queryClient.setQueryDefaults(qk.dashboard(), { refetchInterval });
  for (const query of queryClient.getQueryCache().findAll({ queryKey: qk.dashboard() })) {
    for (const observer of query.observers) {
      if (observer.options.refetchInterval !== refetchInterval) observer.setOptions({ ...observer.options, refetchInterval });
    }
  }
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

  useEffect(() => {
    const factory = createEventSource ?? defaultFactory();
    if (!factory) return undefined;
    let active = true;
    let reopenTimer: ReturnType<typeof setTimeout> | undefined;
    const es = factory(api.streamUrl());

    const invalidate = (prefixes: readonly string[]) => {
      for (const prefix of prefixes) void queryClient.invalidateQueries({ queryKey: [prefix] });
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
  }, [api, queryClient, createEventSource, generation]);

  // While the stream is down the dashboard polls: a query default, so the Dashboard page needs no code for
  // it. Observers already mounted only read defaults when their page re-renders, so they are updated too.
  // Losing the stream also refetches the dashboard once (it may have missed updates).
  const wasConnected = useRef(false);
  useEffect(() => {
    setDashboardPolling(queryClient, connected ? false : DISCONNECTED_REFETCH_MS);
    if (wasConnected.current && !connected) void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    wasConnected.current = connected;
  }, [connected, queryClient]);

  useEffect(() => () => setDashboardPolling(queryClient, false), [queryClient]);

  const value = useMemo(() => ({ connected }), [connected]);
  return createElement(LiveContext.Provider, { value }, children);
}

/** The live-stream state from the nearest `LiveUpdatesProvider` (not connected outside one). */
export function useLiveUpdates(): LiveUpdatesState {
  return useContext(LiveContext);
}
