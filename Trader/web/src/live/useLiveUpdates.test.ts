// P4-T12 tests 6-7: the live-update stream with a fake EventSource (no network).
import type { QueryClient } from "@tanstack/react-query";
import { QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, ApiProvider } from "../api/client";
import { qk } from "../api/queryKeys";
import { FakeApiClient } from "../test/fakeApi";
import { createTestQueryClient } from "../test/render";
import { DISCONNECTED_REFETCH_MS, LiveUpdatesProvider, useLiveUpdates, type EventSourceLike } from "./useLiveUpdates";

type Listener = (ev: MessageEvent) => void;

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly url: string;
  readyState = FakeEventSource.CONNECTING;
  closed = false;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((l) => l !== listener),
    );
  }

  close(): void {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(type: string, data: unknown): void {
    if (type === "hello") this.readyState = FakeEventSource.OPEN;
    const ev = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const l of this.listeners.get(type) ?? []) l(ev);
  }

  fail(readyState: number): void {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }
}

function latest(): FakeEventSource {
  const es = FakeEventSource.instances.at(-1);
  if (!es) throw new Error("no EventSource was opened");
  return es;
}

let queryClient: QueryClient;
let api: FakeApiClient;
let onUnauthorized: ReturnType<typeof vi.fn<() => void>>;

function wrapper({ children }: { children?: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(ApiProvider, { client: api }, createElement(LiveUpdatesProvider, { onUnauthorized }, children)),
  );
}

function seed(): void {
  queryClient.setQueryData(qk.dashboard(), { any: 1 });
  queryClient.setQueryData(qk.proposals({ status: "pending" }), { items: [] });
  queryClient.setQueryData(qk.proposal(3), { id: 3 });
  queryClient.setQueryData(qk.trades({}), { items: [] });
  queryClient.setQueryData(qk.settings(), { items: [] });
  queryClient.setQueryData(qk.events({}), { items: [] });
  queryClient.setQueryData(qk.journal({}), { items: [] });
}

function invalidated(key: readonly unknown[]): boolean {
  return queryClient.getQueryState(key)?.isInvalidated ?? false;
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  queryClient = createTestQueryClient();
  api = new FakeApiClient();
  onUnauthorized = vi.fn();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  queryClient.clear();
});

describe("useLiveUpdates (test 6)", () => {
  it("opens one EventSource on the stream URL", () => {
    renderHook(() => useLiveUpdates(), { wrapper });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(latest().url).toBe("/api/stream");
  });

  it("invalidate {topics: [proposals]} invalidates the dashboard and proposal queries only", () => {
    renderHook(() => useLiveUpdates(), { wrapper });
    seed();
    act(() => latest().emit("invalidate", { topics: ["proposals"] }));
    expect(invalidated(qk.dashboard())).toBe(true);
    expect(invalidated(qk.proposals({ status: "pending" }))).toBe(true);
    expect(invalidated(qk.proposal(3))).toBe(true);
    expect(invalidated(qk.trades({}))).toBe(false);
    expect(invalidated(qk.settings())).toBe(false);
    expect(invalidated(qk.events({}))).toBe(false);
    expect(invalidated(qk.journal({}))).toBe(false);
  });

  it("an events message invalidates the events and dashboard queries", () => {
    renderHook(() => useLiveUpdates(), { wrapper });
    seed();
    act(() => latest().emit("events", { items: [] }));
    expect(invalidated(qk.events({}))).toBe(true);
    expect(invalidated(qk.dashboard())).toBe(true);
    expect(invalidated(qk.proposals({ status: "pending" }))).toBe(false);
  });

  it("ignores unreadable messages and unknown topics", () => {
    renderHook(() => useLiveUpdates(), { wrapper });
    seed();
    act(() => {
      latest().emit("invalidate", { topics: ["nonsense"] });
      for (const l of (latest() as unknown as { listeners: Map<string, Listener[]> }).listeners.get("invalidate") ?? []) {
        l(new MessageEvent("invalidate", { data: "{not json" }));
      }
    });
    expect(invalidated(qk.dashboard())).toBe(false);
  });

  it("hello sets connected; an error sets not connected", () => {
    const { result } = renderHook(() => useLiveUpdates(), { wrapper });
    expect(result.current.connected).toBe(false);
    act(() => latest().emit("hello", { server_time: "2026-10-06T13:35:00Z" }));
    expect(result.current.connected).toBe(true);
    act(() => latest().fail(FakeEventSource.CONNECTING));
    expect(result.current.connected).toBe(false);
    // The browser reconnects by itself: no new EventSource, no session check.
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(api.callsTo("me")).toHaveLength(0);
    act(() => latest().emit("hello", { server_time: "2026-10-06T13:35:05Z" }));
    expect(result.current.connected).toBe(true);
  });

  it("a failed reconnect checks the session and a 401 logs out", async () => {
    api.fail("me", new ApiError(401, "unauthorized", "Please log in"));
    renderHook(() => useLiveUpdates(), { wrapper });
    await act(async () => latest().fail(FakeEventSource.CLOSED));
    expect(api.callsTo("me")).toHaveLength(1);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("a failed reconnect with a valid session reopens the stream later", async () => {
    vi.useFakeTimers();
    renderHook(() => useLiveUpdates(), { wrapper });
    await act(async () => latest().fail(FakeEventSource.CLOSED));
    expect(api.callsTo("me")).toHaveLength(1);
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(FakeEventSource.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[0]!.closed).toBe(true);
  });

  it("the dashboard query defaults to a 15 s refetch while disconnected, cleared while connected", () => {
    const { unmount } = renderHook(() => useLiveUpdates(), { wrapper });
    const interval = () => queryClient.getQueryDefaults(qk.dashboard()).refetchInterval;
    expect(interval()).toBe(DISCONNECTED_REFETCH_MS);
    expect(queryClient.getQueryDefaults(qk.trades({})).refetchInterval).toBeUndefined();

    act(() => latest().emit("hello", { server_time: "2026-10-06T13:35:00Z" }));
    expect(interval()).toBe(false);

    seed();
    act(() => latest().fail(FakeEventSource.CONNECTING));
    expect(interval()).toBe(DISCONNECTED_REFETCH_MS);
    // Losing the stream refetches the dashboard once (updates may have been missed).
    expect(invalidated(qk.dashboard())).toBe(true);
    expect(invalidated(qk.trades({}))).toBe(false);

    unmount();
    expect(interval()).toBe(false);
  });

  it("a mounted dashboard query polls every 15 s while disconnected and stops after reconnecting", async () => {
    vi.useFakeTimers();
    renderHook(
      // Like the Dashboard page: it does not read the live state itself.
      () => useQuery({ queryKey: qk.dashboard(), queryFn: () => api.dashboard() }),
      { wrapper },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const first = api.callsTo("dashboard").length;
    expect(first).toBeGreaterThanOrEqual(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCONNECTED_REFETCH_MS * 2 + 10);
    });
    expect(api.callsTo("dashboard").length).toBeGreaterThanOrEqual(first + 2);

    // The server's first messages after connecting: hello, then a full invalidate.
    await act(async () => {
      latest().emit("hello", { server_time: "2026-10-06T13:35:00Z" });
      latest().emit("invalidate", { topics: ["proposals"] });
      await vi.advanceTimersByTimeAsync(10);
    });
    const afterReconnect = api.callsTo("dashboard").length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCONNECTED_REFETCH_MS * 3);
    });
    expect(api.callsTo("dashboard").length).toBe(afterReconnect);
  });

  it("without an EventSource in the browser it stays disconnected and does not throw", () => {
    vi.stubGlobal("EventSource", undefined);
    const { result } = renderHook(() => useLiveUpdates(), { wrapper });
    expect(result.current.connected).toBe(false);
  });

  it("outside a LiveUpdatesProvider it reports not connected and opens nothing", () => {
    const { result } = renderHook(() => useLiveUpdates());
    expect(result.current.connected).toBe(false);
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});

describe("useLiveUpdates closing (test 7)", () => {
  it("closes the EventSource on unmount", () => {
    const { unmount } = renderHook(() => useLiveUpdates(), { wrapper });
    const es = latest();
    expect(es.closed).toBe(false);
    unmount();
    expect(es.closed).toBe(true);
  });
});
