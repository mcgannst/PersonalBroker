// The app (P4-T12): providers, routes and deep links.
//   /                      -> /dashboard
//   /dashboard, /candidates, /trades, /performance, /journal, /reports, /settings, /system
//                          -> behind RequireAuth, inside the Layout
//   /login                 -> the login page
//   anything else          -> Not found
// Query strings survive the login redirect, so the Telegram links (/dashboard?proposal=12,
// /trades?position=3, /journal?date=2026-10-06, /reports?week=2026-10-09, /system) open the right thing.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { ApiProvider, isApiError, type ApiClient } from "./api/client";
import { createHttpClient } from "./api/http";
import { AuthProvider, notifyUnauthorized } from "./layout/AuthContext";
import { Layout } from "./layout/Layout";
import { NotFound } from "./layout/NotFound";
import { RequireAuth } from "./layout/RequireAuth";
import CandidatesPage from "./pages/Candidates";
import DashboardPage from "./pages/Dashboard";
import JournalPage from "./pages/Journal";
import LoginPage from "./pages/Login";
import PerformancePage from "./pages/Performance";
import ReportsPage from "./pages/Reports";
import SettingsPage from "./pages/Settings";
import SystemPage from "./pages/System";
import TradesPage from "./pages/Trades";

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
            <Route path="/candidates" element={<CandidatesPage />} />
            <Route path="/trades" element={<TradesPage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/system" element={<SystemPage />} />
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

export function App({ api }: { api?: ApiClient } = {}) {
  const [queryClient] = useState(createAppQueryClient);
  const [client] = useState<ApiClient>(() => api ?? createHttpClient({ onUnauthorized: notifyUnauthorized }));
  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={client}>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </ApiProvider>
    </QueryClientProvider>
  );
}

export default App;
