// P6-GD web gauntlet, attempt 1 (Breaker): the Reports page "Day" view (P6-T12, the decision log). Hostile
// text in every server string the view shows (reasons, Claude's catalyst text, notes, rules, checks, data,
// the run mode), filters and pagination against a server-sized day, the live-only default (no run id is ever
// sent unless the URL has one), and the empty and error states with 44 px targets. FakeApiClient only.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError, type DecisionDayQuery } from "../api/client";
import type { DecisionDayOut, DecisionRowOut } from "../api/types";
import ReportsPage from "../pages/Reports";
import { DAY_PAGE_SIZE } from "../pages/reports/DayDecisions";
import { FakeApiClient } from "../test/fakeApi";
import * as fx from "../test/fixtures";
import { renderWithProviders } from "../test/render";

// ---------------------------------------------------------------- helpers

const XSS = `<img src=x onerror="alert(1)"><script>alert(1)</script><a href="javascript:alert(1)">x</a><svg onload=alert(1)>`;
const tagged = (label: string): string => `${label}${XSS}`;
const URL_ATTRS = new Set(["href", "src", "action", "formaction", "xlink:href"]);

function expectInert(root: ParentNode = document.body): void {
  expect(root.querySelectorAll("script, iframe, object, embed, img, svg[onload]")).toHaveLength(0);
  for (const el of Array.from(root.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      const name = attr.name.toLowerCase();
      expect(name.startsWith("on"), `<${el.tagName.toLowerCase()}> has ${name}`).toBe(false);
      if (URL_ATTRS.has(name)) expect(attr.value.replace(/[\u0000- ]/g, "")).not.toMatch(/^(javascript|data|vbscript):/i);
    }
  }
}

/** Every button has a name and an inline 44 px min-height; every link a touch class. */
function expectTouchTargets(where: string): void {
  for (const b of Array.from(document.querySelectorAll<HTMLButtonElement>("button"))) {
    const name = (b.getAttribute("aria-label") ?? b.textContent ?? "").trim();
    expect(name, `${where}: a button without a name`).not.toBe("");
    expect(parseFloat(b.style.minHeight || "0"), `${where}: button "${name}" under 44 px`).toBeGreaterThanOrEqual(44);
  }
  for (const a of Array.from(document.querySelectorAll("a"))) {
    expect(["link-touch", "btn"].some((c) => a.classList.contains(c)), `${where}: link "${a.textContent}"`).toBe(true);
  }
}

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

/** No request and no link carries a run id (the live-only default). */
function expectNoRunId(api: FakeApiClient): void {
  for (const method of ["decisionDays", "decisionDay", "decisionsCsvUrl"] as const) {
    for (const [q] of api.callsTo(method) as [Record<string, unknown>][]) {
      expect(q, `${method} sent a run id`).not.toHaveProperty("run_id");
    }
  }
  for (const a of Array.from(document.querySelectorAll("a"))) expect(a.getAttribute("href") ?? "").not.toMatch(/run/);
}

/** A server that answers like the real one: `total` rows, sliced by limit/offset. */
function pagedDay(total: number): (q: DecisionDayQuery) => DecisionDayOut {
  const base = fx.decisionRows[3] as DecisionRowOut;
  return (q) => {
    const offset = q.offset ?? 0;
    const limit = q.limit ?? 2000;
    const n = Math.max(0, Math.min(limit, total - offset));
    const rows = Array.from({ length: n }, (_, i) => ({ ...base, seq: offset + i + 1, ticker: `T${offset + i + 1}` }));
    return { ...fx.decisionDayOut, rows, total };
  };
}

// ---------------------------------------------------------------- hostile text

describe("hostile decision-log text renders as inert text", () => {
  it("reasons, catalyst text, notes, rules, checks, data, refs and the run mode", async () => {
    const hostileRow: DecisionRowOut = {
      seq: 1,
      stage: "scan",
      outcome: "rejected",
      strategy_key: tagged("strat"),
      symbol_id: 9,
      ticker: tagged("TKR"),
      rule: tagged("rule"),
      reason: tagged("claude reason"),
      ts: "2026-10-06T13:35:05Z",
      ref: { [tagged("refkey")]: 5 },
      checks: [{ name: tagged("check"), value: tagged("value"), op: ">=", threshold: tagged("thr"), passed: false }],
      data: { catalyst: { type: tagged("ctype"), reason: tagged("catalyst reason") }, [XSS]: XSS },
    };
    const day: DecisionDayOut = {
      ...fx.decisionDayOut,
      run_id: 42,
      run_mode: tagged("replay"),
      summary: {
        ...fx.decisionSummary,
        text: tagged("summary text"),
        notes: [tagged("note")],
        rejects_by_rule: [{ rule: tagged("reject rule"), count: 3 }],
        risk_rejections: [{ rule: tagged("risk rule"), count: 1 }],
        exits_by_reason: [{ rule: tagged("exit rule"), count: 1 }],
        approvals: { [tagged("approval")]: 1 },
      },
      rows: [hostileRow],
      total: 1,
    };
    const api = new FakeApiClient({
      decisionDay: day,
      decisionDays: { days: [{ run_id: 42, session_date: "2026-10-06", final: false, summary_text: tagged("day item"), proposals: 1, trades: 0 }] },
    });
    renderWithProviders(<ReportsPage />, { api, route: "/reports?day=2026-10-06&run=42" });
    const table = await screen.findByRole("table", { name: "Decisions" });
    await settle();
    expect(table).toHaveTextContent("claude reason<img");
    expect(table).toHaveTextContent("TKR<img");
    expect(document.body).toHaveTextContent("summary text<img");
    expect(document.body).toHaveTextContent("note<img");
    expect(document.body).toHaveTextContent("replay<img");
    await userEvent.click(within(table).getByRole("button", { name: "Show details of decision 1" }));
    const detail = screen.getByRole("region", { name: "Details of decision 1" });
    expect(within(detail).getByRole("table", { name: "Checks" })).toHaveTextContent("check<img");
    expect(detail).toHaveTextContent("catalyst reason<img");
    expectInert();
    // a hostile ticker typed into the filter goes to the server as text only
    await userEvent.type(screen.getByRole("searchbox", { name: "Ticker" }), "<b>x</b>");
    await waitFor(() => expect((api.callsTo("decisionDay").at(-1) as [DecisionDayQuery])[0].ticker).toBe("<B>X</B>"));
    expectInert();
  });
});

// ---------------------------------------------------------------- filters and pages

describe("filters and pages against a server-sized day", () => {
  it("pages forward and back, the last page ends Next, a filter goes back to the first page, no run id is sent", async () => {
    const api = new FakeApiClient();
    api.respond("decisionDay", pagedDay(450));
    const r = renderWithProviders(<ReportsPage />, { api, route: "/reports?day=2026-10-06" });
    await screen.findByRole("table", { name: "Decisions" });
    const last = (): DecisionDayQuery => (r.api.callsTo("decisionDay").at(-1) as [DecisionDayQuery])[0];
    expect(screen.getByText(/Showing 1–200 of 450/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(screen.getByText(/Showing 201–400 of 450/)).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(screen.getByText(/Showing 401–450 of 450/)).toBeInTheDocument());
    expect(last().offset).toBe(2 * DAY_PAGE_SIZE);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(within(screen.getByRole("table", { name: "Decisions" })).getAllByRole("row")).toHaveLength(51);

    await userEvent.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => expect(last().offset).toBe(DAY_PAGE_SIZE));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Outcome" }), "rejected");
    await waitFor(() => expect(last()).toEqual({ date: "2026-10-06", outcome: "rejected", limit: DAY_PAGE_SIZE, offset: 0 }));

    // blanks only: no ticker filter; padded lower case: trimmed and upper-cased
    const box = screen.getByRole("searchbox", { name: "Ticker" });
    await userEvent.type(box, "   ");
    await settle();
    expect(last()).not.toHaveProperty("ticker");
    await userEvent.clear(box);
    await userEvent.type(box, " nvda ");
    await waitFor(() => expect(last().ticker).toBe("NVDA"));
    expect(last().offset).toBe(0);
    expectNoRunId(r.api);
  });
});

// ---------------------------------------------------------------- empty and error states

describe("empty and error states", () => {
  it("a failing day list, a failing day, an empty day: a message each, retries work, 44 px targets, live only", async () => {
    const api = new FakeApiClient().fail("decisionDays", new ApiError(500, "internal", "The server had a problem"));
    const first = renderWithProviders(<ReportsPage />, { api, route: "/reports?day=" });
    expect(await screen.findByRole("alert")).toHaveTextContent("The server had a problem");
    expect(api.callsTo("decisionDay")).toEqual([]);
    expectTouchTargets("days failed");
    api.succeed("decisionDays");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(fx.decisionSummaryText)).toBeInTheDocument();
    await settle();
    expectTouchTargets("day loaded");
    expectNoRunId(api);
    first.unmount();

    const failing = new FakeApiClient().fail("decisionDay", new ApiError(503, "unavailable", "Database unavailable"));
    const second = renderWithProviders(<ReportsPage />, { api: failing, route: "/reports?day=2026-10-06" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    expect(screen.queryByRole("table", { name: "Decisions" })).toBeNull();
    expectTouchTargets("day failed");
    expectNoRunId(failing);
    second.unmount();

    const empty = new FakeApiClient({ decisionDay: null });
    renderWithProviders(<ReportsPage />, { api: empty, route: "/reports?day=2026-10-03" });
    expect(await screen.findByText("No decisions recorded for 2026-10-03.")).toBeInTheDocument();
    await settle();
    expectTouchTargets("empty day");
    expectNoRunId(empty);
  });
});
