import { useQuery } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { useParams, useSearchParams } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import Candidates from "../pages/Candidates";
import Dashboard from "../pages/Dashboard";
import Journal from "../pages/Journal";
import Login from "../pages/Login";
import Performance from "../pages/Performance";
import Reports from "../pages/Reports";
import Settings from "../pages/Settings";
import System from "../pages/System";
import Trades from "../pages/Trades";
import { FakeApiClient } from "./fakeApi";
import { renderWithProviders } from "./render";

function DashboardProbe() {
  const api = useApi();
  const [params] = useSearchParams();
  const q = useQuery({ queryKey: qk.dashboard(), queryFn: () => api.dashboard() });
  return (
    <p>
      {params.get("proposal")}:{q.data ? q.data.pending[0]?.ticker : "loading"}
    </p>
  );
}

function ParamProbe() {
  const { id } = useParams();
  return <p>id {id}</p>;
}

describe("renderWithProviders (acceptance test 6)", () => {
  it("renders the Dashboard stub title", () => {
    renderWithProviders(<Dashboard />);
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
  });

  it("renders every page stub", () => {
    const pages = { Login, Dashboard, Candidates, Trades, Performance, Journal, Reports, Settings, System };
    for (const [name, Page] of Object.entries(pages)) {
      const { unmount } = renderWithProviders(<Page />);
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
      unmount();
    }
  });

  it("provides the API, a QueryClient and the router at `route`", async () => {
    const api = new FakeApiClient();
    const r = renderWithProviders(<DashboardProbe />, { api, route: "/dashboard?proposal=12" });
    expect(await screen.findByText("12:AAA")).toBeInTheDocument();
    expect(api.callsTo("dashboard")).toHaveLength(1);
    expect(r.api).toBe(api);
    expect(r.location().pathname).toBe("/dashboard");
    expect(r.location().search).toBe("?proposal=12");
  });

  it("matches `path` so route params work", () => {
    renderWithProviders(<ParamProbe />, { route: "/things/7", path: "/things/:id" });
    expect(screen.getByText("id 7")).toBeInTheDocument();
  });
});
