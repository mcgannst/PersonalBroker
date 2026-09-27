// Renders a component with the app's providers for tests: a QueryClient with retries off, a MemoryRouter at
// `route`, and an ApiProvider with a FakeApiClient (or the client given).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation, type Location } from "react-router-dom";

import { ApiProvider, type ApiClient } from "../api/client";
import { ROUTER_FUTURE } from "../layout/routerFuture";
import { FakeApiClient } from "./fakeApi";

export interface RenderWithProvidersOptions extends Omit<RenderOptions, "wrapper"> {
  /** The client the tree gets from `useApi()`; a new `FakeApiClient` by default. */
  api?: ApiClient;
  /** The initial URL (path and query), `/` by default. */
  route?: string;
  /** A route pattern: when given, `ui` is rendered as that route's element (so `useParams` works). */
  path?: string;
  /** A QueryClient to use instead of a fresh one. */
  queryClient?: QueryClient;
}

export interface RenderWithProvidersResult<A extends ApiClient> extends RenderResult {
  api: A;
  queryClient: QueryClient;
  /** The router's current location (updated as the test navigates). */
  location: () => Location;
}

export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: 0, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

function LocationProbe({ onLocation }: { onLocation: (l: Location) => void }) {
  onLocation(useLocation());
  return null;
}

export function renderWithProviders<A extends ApiClient = FakeApiClient>(
  ui: ReactElement,
  options: RenderWithProvidersOptions & { api?: A } = {},
): RenderWithProvidersResult<A> {
  const { api = new FakeApiClient() as unknown as A, route = "/", path, queryClient = createTestQueryClient(), ...rest } = options;
  let current: Location | null = null;
  const probe = <LocationProbe onLocation={(l) => (current = l)} />;
  const content: ReactNode = path ? (
    <Routes>
      <Route path={path} element={ui} />
      <Route path="*" element={null} />
    </Routes>
  ) : (
    ui
  );
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={api}>
        <MemoryRouter initialEntries={[route]} future={ROUTER_FUTURE}>
          {content}
          {probe}
        </MemoryRouter>
      </ApiProvider>
    </QueryClientProvider>,
    rest,
  );
  return {
    ...result,
    api,
    queryClient,
    location: () => {
      if (!current) throw new Error("the router has not rendered yet");
      return current;
    },
  };
}
