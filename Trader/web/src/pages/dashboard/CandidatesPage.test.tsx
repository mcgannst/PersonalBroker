import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { CatalystOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Candidates from "../Candidates";
import { safeHttpUrl } from "./labels";

async function renderCandidates(api = new FakeApiClient(), route = "/candidates") {
  const r = renderWithProviders(<Candidates />, { api, route });
  await screen.findByRole("table");
  return r;
}

describe("Candidates page (acceptance test 8)", () => {
  it("shows the brief as preformatted text", async () => {
    await renderCandidates();
    expect(screen.getByRole("heading", { name: "Candidates" })).toBeInTheDocument();
    const brief = screen.getByText(/Pre-market brief 2026-10-06/);
    expect(brief.tagName).toBe("PRE");
    expect(brief.textContent).toContain("2. FFF +3.1% analyst upgrade");
  });

  it("shows catalyst cards with safe headline links only", async () => {
    await renderCandidates();
    const card = screen.getByRole("article", { name: /catalyst AAA/i });
    const c = within(card);
    expect(c.getByText("earnings")).toBeInTheDocument();
    expect(c.getByText(/positive/)).toBeInTheDocument();
    expect(c.getByText(/82/)).toBeInTheDocument();
    expect(c.getByText(/Confirmed: yes/)).toBeInTheDocument();
    expect(c.getByText("Q3 revenue and EPS beat; guidance raised")).toBeInTheDocument();
    expect(c.getByText(/\+6\.40%/)).toBeInTheDocument();
    expect(c.getByText(/2026-10-05/)).toBeInTheDocument();

    const good = c.getByRole("link", { name: "AAA beats on revenue, raises full-year outlook" });
    expect(good).toHaveAttribute("href", "https://www.businesswire.com/news/aaa-q3");
    expect(good).toHaveAttribute("target", "_blank");
    expect(good).toHaveAttribute("rel", "noopener noreferrer");

    const bad = c.getByText("AAA shares jump premarket");
    expect(bad.closest("a")).toBeNull();
    expect(bad).not.toHaveAttribute("href");
    expect(card.innerHTML).not.toContain("javascript:");
    expect(c.getAllByRole("link")).toHaveLength(1);
  });

  it("shows at most 5 headlines per catalyst", async () => {
    const headlines = Array.from({ length: 8 }, (_, i) => ({ ts: null, title: `Headline ${i}`, source: null, url: `https://example.test/${i}` }));
    const catalyst: CatalystOut = { ...fx.catalysts[0]!, headlines };
    await renderCandidates(new FakeApiClient({ candidates: { ...fx.candidatesOut, catalysts: [catalyst] } }));
    const card = screen.getByRole("article", { name: /catalyst AAA/i });
    expect(within(card).getAllByRole("link")).toHaveLength(5);
  });

  it("shows the ranking rows in rank order with reject reasons", async () => {
    const shuffled = [fx.candidatesRanking[2]!, fx.candidatesRanking[0]!, fx.candidatesRanking[1]!];
    await renderCandidates(new FakeApiClient({ candidates: { ...fx.candidatesOut, ranking: shuffled } }));
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[1]?.textContent)).toEqual(["AAA", "FFF", "GGG"]);
    expect(rows[0]).toHaveTextContent("1");
    expect(rows[0]).toHaveTextContent("3.10");
    expect(rows[0]).toHaveTextContent("✓");
    expect(rows[1]).toHaveTextContent("doji opening candle");
    expect(rows[2]).toHaveTextContent("no confirmed catalyst");
    expect(rows[0]).toHaveTextContent("orb_sip");
  });

  it("defaults the date picker to the served session and refetches when it changes", async () => {
    const api = new FakeApiClient();
    const r = await renderCandidates(api);
    expect(api.callsTo("candidates")).toEqual([[undefined]]);
    const picker = screen.getByLabelText("Session date") as HTMLInputElement;
    expect(picker.value).toBe("2026-10-06");

    fireEvent.change(picker, { target: { value: "2026-10-05" } });
    await waitFor(() => expect(api.callsTo("candidates")).toEqual([[undefined], ["2026-10-05"]]));
    expect(r.location().search).toBe("?date=2026-10-05");
  });

  it("reads ?date= from the URL", async () => {
    const api = new FakeApiClient();
    await renderCandidates(api, "/candidates?date=2026-10-02");
    expect(api.callsTo("candidates")).toEqual([["2026-10-02"]]);
    expect((screen.getByLabelText("Session date") as HTMLInputElement).value).toBe("2026-10-02");
  });

  it("shows empty states and errors", async () => {
    const api = new FakeApiClient({ candidates: { session_date: "2026-10-06", brief: null, catalysts: [], ranking: [] } });
    renderWithProviders(<Candidates />, { api });
    expect(await screen.findByText("No brief for this session")).toBeInTheDocument();
    expect(screen.getByText("No catalysts for this session")).toBeInTheDocument();
    expect(screen.getByText("No ranking for this session")).toBeInTheDocument();

    const failing = new FakeApiClient();
    failing.fail("candidates", new ApiError(422, "validation", "Invalid date"));
    renderWithProviders(<Candidates />, { api: failing, route: "/candidates?date=nope" });
    expect(await screen.findByText("Invalid date")).toBeInTheDocument();
    failing.succeed("candidates");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(failing.callsTo("candidates")).toHaveLength(2));
  });
});

describe("safeHttpUrl", () => {
  it("keeps only http and https URLs", () => {
    expect(safeHttpUrl("https://a.test/x")).toBe("https://a.test/x");
    expect(safeHttpUrl("http://a.test/x")).toBe("http://a.test/x");
    expect(safeHttpUrl("HTTPS://A.TEST/")).toBe("HTTPS://A.TEST/");
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl(" javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("data:text/html,x")).toBeNull();
    expect(safeHttpUrl("//evil.test")).toBeNull();
    expect(safeHttpUrl(" https://a.test")).toBeNull();
    expect(safeHttpUrl("")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
  });
});
