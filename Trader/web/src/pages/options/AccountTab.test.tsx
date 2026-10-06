// OPTSIM-T15: the Account tab's headline and its warnings.
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FakeOptionsApiClient } from "../../test/optionsFakeApi";
import { OPT_NOW, optAccount } from "../../test/optionsFixtures";
import { AccountTab, AccountView, accountWarnings } from "./AccountTab";
import { renderOptions } from "./testRender";

const NOW = Date.parse(OPT_NOW);

describe("AccountTab", () => {
  it("the headline is the account value; cash figures, premium (secondary), sources and the benchmark follow", async () => {
    const { opt } = renderOptions(<AccountTab />);
    expect(await screen.findByTestId("opt-account-value")).toHaveTextContent("$5,082.45");
    expect(opt.callsTo("optAccount").length).toBeGreaterThanOrEqual(1);
    const stat = (label: string) => screen.getByText(label, { selector: ".stat-label" }).nextSibling;
    expect(stat("Cash")).toHaveTextContent("$5,123.45");
    expect(stat("Reserved")).toHaveTextContent("$1,200.00");
    expect(stat("Free cash")).toHaveTextContent("$3,923.45");
    expect(screen.getByText(/Premium collected: \$164\.00/)).toHaveClass("small");
    const rows = within(screen.getByRole("table")).getAllByRole("row");
    expect(rows.map((r) => r.children[0]?.textContent)).toEqual(["Source", "manual", "toy_call"]);
    expect(screen.getByText("SOFI shares").nextSibling).toHaveTextContent("+1.23%");
    expect(screen.getByText("Account").nextSibling).toHaveTextContent("+1.65%");
  });

  it("no warning when the marks are complete and the worker reported within 2 minutes", () => {
    expect(accountWarnings(optAccount, NOW)).toEqual([]);
    renderOptions(<AccountView account={optAccount} nowMs={NOW} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("warns when the marks are incomplete", () => {
    renderOptions(<AccountView account={{ ...optAccount, marks_complete: false }} nowMs={NOW} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/no fresh mark/);
  });

  it.each([
    ["older than 2 minutes", "2026-10-06T13:55:00Z", /last reported at 2026-10-06 07:55 MT/],
    ["never", null, /has not reported yet/],
  ])("warns when the worker heartbeat is %s", (_name, beat, text) => {
    renderOptions(<AccountView account={{ ...optAccount, worker_beat_at: beat }} nowMs={NOW} />);
    expect(screen.getByRole("alert")).toHaveTextContent(text);
  });

  it("an account without a run explains how to start one", async () => {
    const opt = new FakeOptionsApiClient({ optAccount: { ...optAccount, run_id: null } });
    renderOptions(<AccountTab />, { opt });
    expect(await screen.findByText(/No options run yet/)).toBeInTheDocument();
  });
});
