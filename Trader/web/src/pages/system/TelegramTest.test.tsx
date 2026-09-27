import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { TelegramTest } from "./TelegramTest";

describe("TelegramTest", () => {
  it("calls telegramTest once and shows the result", async () => {
    const { api } = renderWithProviders(<TelegramTest />);
    await userEvent.click(screen.getByRole("button", { name: "Send a test message" }));
    await waitFor(() => expect(api.callsTo("telegramTest")).toHaveLength(1));
    expect(await screen.findByText("Test message sent: check your Telegram")).toBeInTheDocument();
  });

  it("a 409 shows 'Telegram is not configured'", async () => {
    const api = new FakeApiClient().fail("telegramTest", new ApiError(409, "conflict", "Telegram is not configured"));
    renderWithProviders(<TelegramTest />, { api });
    await userEvent.click(screen.getByRole("button", { name: "Send a test message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Telegram is not configured");
  });
});
