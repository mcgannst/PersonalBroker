// The app (P4-T12; live dashboard plan S12, DB-T11): providers, routes and deep links.
//   /                      -> /dashboard
//   /dashboard, /options, /control, /candidates, /trades, /performance, /journal, /reports, /replay, /settings
//                          -> behind RequireAuth, inside the Layout
//   /options               -> the options simulation (OPTSIM-T15); it has its own API client and provider
//   /system                -> /control, keeping the query string (the System page merged into Control)
//   /login                 -> the login page
//   anything else          -> Not found
// Query strings survive the login redirect, so the Telegram links (/dashboard?proposal=12,
// /trades?position=3, /journal?date=2026-10-06, /reports?week=2026-10-09, /system) open the right thing.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { ApiProvider, isApiError, type ApiClient } from "./api/client";
import { createHttpClient } from "./api/http";
import { OptionsApiProvider, type OptionsApiClient } from "./api/optionsClient";
import { createOptionsHttpClient } from "./api/optionsHttp";
import { AuthProvider, notifyUnauthorized } from "./layout/AuthContext";
import { Layout } from "./layout/Layout";
import { NotFound } from "./layout/NotFound";
import { RequireAuth } from "./layout/RequireAuth";
import { ROUTER_FUTURE } from "./layout/routerFuture";
import CandidatesPage from "./pages/Candidates";
import ControlPage from "./pages/Control";
import DashboardPage from "./pages/Dashboard";
import JournalPage from "./pages/Journal";
import LoginPage from "./pages/Login";
import OptionsPage from "./pages/Options";
import PerformancePage from "./pages/Performance";
import ReplayPage from "./pages/Replay";
import ReportsPage from "./pages/Reports";
import SettingsPage from "./pages/Settings";
import TradesPage from "./pages/Trades";

/** `/system` (a Telegram link) lands on `/control` with the same query string and hash. */
function SystemRedirect() {
  const { search, hash } = useLocation();
  return <Navigate to={`/control${search}${hash}`} replace />;
}

/** The routes, with the auth provider (the router and the other providers come from the caller). */
export function AppRoutes() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/options" element={<OptionsPage />} />
            <Route path="/control" element={<ControlPage />} />
            <Route path="/candidates" element={<CandidatesPage />} />
            <Route path="/trades" element={<TradesPage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/replay" element={<ReplayPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/system" element={<SystemRedirect />} />
          </Route>
        </Route>
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AuthProvider>
  );
}

/** Retries network and server errors twice; a 4xx answer (401, 404, 422, ...) is final. */
export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (count, error) => !(isApiError(error) && error.status >= 400 && error.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
}

/** The two API clients. The options client reads the session's CSRF token from the main HTTP client. */
function createClients(api?: ApiClient, optionsApi?: OptionsApiClient): { client: ApiClient; options: OptionsApiClient } {
  const http = api ? null : createHttpClient({ onUnauthorized: notifyUnauthorized });
  const client: ApiClient = api ?? http!;
  const options =
    optionsApi ??
    createOptionsHttpClient({
      onUnauthorized: notifyUnauthorized,
      csrfToken: () => http?.csrfToken() ?? null,
      refreshCsrf: () => client.me(),
    });
  return { client, options };
}

export function App({ api, optionsApi }: { api?: ApiClient; optionsApi?: OptionsApiClient } = {}) {
  const [queryClient] = useState(createAppQueryClient);
  const [clients] = useState(() => createClients(api, optionsApi));
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={clients.client}>
        <OptionsApiProvider client={clients.options}>
          <BrowserRouter future={ROUTER_FUTURE}>
            <AppRoutes />
          </BrowserRouter>
        </OptionsApiProvider>
      </ApiProvider>
    </QueryClientProvider>
  );
}

export default App;
