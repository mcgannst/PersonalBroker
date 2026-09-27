import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { TelegramTest } from "./TelegramTest";

describe("TelegramTest", () => {
  it("calls telegramTest and shows the result", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<TelegramTest />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Send a test message" }));
    expect(api.callsTo("telegramTest")).toEqual([[]]);
    expect(await screen.findByText("Test message sent: check your Telegram")).toBeInTheDocument();
  });

  it("shows 'Telegram is not configured' on a 409", async () => {
    const api = new FakeApiClient();
    api.fail("telegramTest", new ApiError(409, "conflict", ""));
    renderWithProviders(<TelegramTest />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Send a test message" }));
    expect(await screen.findByText("Telegram is not configured")).toBeInTheDocument();
  });

  it("shows another server error's message", async () => {
    const api = new FakeApiClient();
    api.fail("telegramTest", new ApiError(500, "internal", "Server error"));
    renderWithProviders(<TelegramTest />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Send a test message" }));
    expect(await screen.findByText("Server error")).toBeInTheDocument();
  });
});
