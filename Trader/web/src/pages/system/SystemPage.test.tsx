import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import SystemPage from "../System";

describe("System page", () => {
  it("renders every section from /api/system", async () => {
    const { api } = renderWithProviders(<SystemPage />, { route: "/system" });
    expect(await screen.findByRole("heading", { name: "System", level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole("status", { name: "Token OK" })).toBeInTheDocument();
    for (const title of [
      "Questrade token",
      "Worker",
      "Telegram",
      "Version",
      "Time zone",
      "Rate limits",
      "Job runs",
      "Run a job",
      "Errors",
      "Event log",
      "Failed Telegram sends",
      "Watchlist upload",
    ]) {
      expect(screen.getByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByLabelText("New refresh token")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Send a test message" })).toBeInTheDocument();
    expect(api.callsTo("system")).toHaveLength(1);
  });

  it("shows an error box with Retry when /api/system fails", async () => {
    const api = new FakeApiClient().fail("system", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<SystemPage />, { api, route: "/system" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    api.succeed("system");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("status", { name: "Token OK" })).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("system")).toHaveLength(2));
  });
});
