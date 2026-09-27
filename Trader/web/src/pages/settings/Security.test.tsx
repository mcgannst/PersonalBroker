import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import { sessionOut, totpSetupOut } from "../../test/fixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import { Security } from "./Security";

const totpOn = { ...sessionOut, user: { ...sessionOut.user, totp_enabled: true } };

async function fillPassword(current: string, next: string, again: string) {
  const form = await screen.findByRole("form", { name: "Change password" });
  await userEvent.type(within(form).getByLabelText("Current password"), current);
  await userEvent.type(within(form).getByLabelText("New password"), next);
  await userEvent.type(within(form).getByLabelText("New password again"), again);
  await userEvent.click(within(form).getByRole("button", { name: "Change password" }));
  return form;
}

describe("Security: password change (acceptance test 7)", () => {
  it("mismatched passwords make no call", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<Security />, { api });
    await fillPassword("old-password", "abcdefgh", "abcdefgX");
    expect(screen.getByText("The new passwords don't match.")).toBeInTheDocument();
    expect(api.callsTo("changePassword")).toEqual([]);
  });

  it("a 7-character new password makes no call", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<Security />, { api });
    await fillPassword("old-password", "abcdefg", "abcdefg");
    expect(screen.getByText("The new password needs at least 8 characters.")).toBeInTheDocument();
    expect(api.callsTo("changePassword")).toEqual([]);
  });

  it("a matching 8-character one calls changePassword and clears the fields", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<Security />, { api });
    const form = await fillPassword("old-password", "abcdefgh", "abcdefgh");
    expect(api.callsTo("changePassword")).toEqual([[{ current_password: "old-password", new_password: "abcdefgh" }]]);
    expect(await screen.findByText("Password changed. Your other sessions were signed out.")).toBeInTheDocument();
    expect(within(form).getByLabelText("Current password")).toHaveValue("");
    expect(within(form).getByLabelText("New password")).toHaveValue("");
    expect(within(form).getByLabelText("New password again")).toHaveValue("");
  });

  it("with two-step on, the code is sent too; a server error is shown", async () => {
    const api = new FakeApiClient({ me: totpOn });
    api.fail("changePassword", new ApiError(401, "bad_credentials", "Wrong password or code."));
    renderWithProviders(<Security />, { api });
    const form = await screen.findByRole("form", { name: "Change password" });
    await userEvent.type(within(form).getByLabelText("Two-step code"), "123456");
    await fillPassword("old-password", "abcdefgh", "abcdefgh");
    expect(api.callsTo("changePassword")).toEqual([[{ current_password: "old-password", new_password: "abcdefgh", totp: "123456" }]]);
    expect(await screen.findByText("Wrong password or code.")).toBeInTheDocument();
  });
});

describe("Security: two-step (acceptance test 8)", () => {
  it("setup shows the secret and the link, and confirm sends the 6-digit code", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<Security />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Set up two-step" }));
    await userEvent.type(screen.getByLabelText("Password"), "my-password");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(api.callsTo("totpSetup")).toEqual([[{ password: "my-password" }]]);
    expect(await screen.findByText(totpSetupOut.secret)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in an authenticator app" })).toHaveAttribute("href", totpSetupOut.otpauth_uri);
    const confirm = screen.getByRole("button", { name: "Confirm" });
    const code = screen.getByLabelText("Code from the app");
    await userEvent.type(code, "12345");
    expect(confirm).toBeDisabled();
    await userEvent.type(code, "6");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(api.callsTo("totpConfirm")).toEqual([[{ code: "123456" }]]);
    expect(await screen.findByText("Two-step sign-in is on.")).toBeInTheDocument();
    expect(screen.queryByText(totpSetupOut.secret)).toBeNull();
  });

  it("cancelling the setup forgets the secret", async () => {
    renderWithProviders(<Security />);
    await userEvent.click(await screen.findByRole("button", { name: "Set up two-step" }));
    await userEvent.type(screen.getByLabelText("Password"), "my-password");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText(totpSetupOut.secret);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(totpSetupOut.secret)).toBeNull();
    expect(screen.getByRole("button", { name: "Set up two-step" })).toBeInTheDocument();
  });

  it("a non-otpauth link is shown as text only", async () => {
    const api = new FakeApiClient({ totpSetup: { secret: "ABC", otpauth_uri: "javascript:alert(1)" } });
    renderWithProviders(<Security />, { api });
    await userEvent.click(await screen.findByRole("button", { name: "Set up two-step" }));
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("ABC");
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("disable needs the password and a code", async () => {
    const api = new FakeApiClient({ me: totpOn });
    renderWithProviders(<Security />, { api });
    const form = await screen.findByRole("form", { name: "Turn off two-step" });
    const button = within(form).getByRole("button", { name: "Turn off two-step" });
    expect(button).toBeDisabled();
    await userEvent.type(within(form).getByLabelText("Password"), "my-password");
    await userEvent.type(within(form).getByLabelText("Code from the app"), "654321");
    await userEvent.click(button);
    expect(api.callsTo("totpDisable")).toEqual([[{ password: "my-password", code: "654321" }]]);
    expect(await screen.findByText("Two-step sign-in is off.")).toBeInTheDocument();
  });
});
