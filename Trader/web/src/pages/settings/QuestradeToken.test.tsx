import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { tokenOut } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { QuestradeToken } from "./QuestradeToken";

const PASTED = "  aBcD1234efGH5678ijKL  ";

describe("QuestradeToken (acceptance test 6)", () => {
  it("is a password input, sends the pasted value, is empty after submit and shows the status", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<QuestradeToken />, { api });
    const input = screen.getByLabelText("Refresh token");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    const save = screen.getByRole("button", { name: "Save token" });
    expect(save).toBeDisabled();
    await userEvent.click(input);
    await userEvent.paste(PASTED);
    await userEvent.click(save);
    expect(api.callsTo("putQuestradeToken")).toEqual([["aBcD1234efGH5678ijKL"]]);
    expect(input).toHaveValue("");
    expect(await screen.findByText(/Token OK/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`expires 2026-10-06 08:30 MT`))).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("aBcD1234efGH5678ijKL");
  });

  it("shows the server's 422 message and still clears the input", async () => {
    const api = new FakeApiClient();
    api.fail("putQuestradeToken", new ApiError(422, "questrade_auth", "Questrade rejected the token: it may be used or expired."));
    renderWithProviders(<QuestradeToken />, { api });
    const input = screen.getByLabelText("Refresh token");
    await userEvent.type(input, "bad-token");
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("Questrade rejected the token: it may be used or expired.")).toBeInTheDocument();
    expect(input).toHaveValue("");
  });

  it("shows a not-OK status with its error", async () => {
    const api = new FakeApiClient({ putQuestradeToken: { ...tokenOut, ok: false, error: "refresh failed" } });
    renderWithProviders(<QuestradeToken />, { api });
    await userEvent.type(screen.getByLabelText("Refresh token"), "tok");
    await userEvent.click(screen.getByRole("button", { name: "Save token" }));
    expect(await screen.findByText("Token not OK: refresh failed")).toBeInTheDocument();
  });
});
