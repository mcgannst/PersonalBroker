import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { TokenPaste } from "./TokenPaste";

const TOKEN = "pasted-refresh-token-value";

describe("TokenPaste", () => {
  it("is a password input, sends the pasted value, is empty after submit and never echoes the token", async () => {
    const { api, container } = renderWithProviders(<TokenPaste />);
    const input = screen.getByLabelText("New refresh token");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    await userEvent.type(input, `  ${TOKEN}  `);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    await waitFor(() => expect(api.callsTo("putQuestradeToken")).toEqual([[TOKEN]]));
    expect(input).toHaveValue("");
    expect(await screen.findByText("Token saved and working.")).toBeInTheDocument();
    expect(container.innerHTML).not.toContain(TOKEN);
  });

  it("shows the server's 422 message and still clears the input", async () => {
    const api = new FakeApiClient().fail("putQuestradeToken", new ApiError(422, "validation", "The refresh token was rejected"));
    const { container } = renderWithProviders(<TokenPaste />, { api });
    const input = screen.getByLabelText("New refresh token");
    await userEvent.type(input, TOKEN);
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The refresh token was rejected");
    expect(input).toHaveValue("");
    expect(container.innerHTML).not.toContain(TOKEN);
  });

  it("an empty paste makes no call", async () => {
    const { api } = renderWithProviders(<TokenPaste />);
    await userEvent.type(screen.getByLabelText("New refresh token"), "   ");
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(api.callsTo("putQuestradeToken")).toEqual([]);
  });
});
