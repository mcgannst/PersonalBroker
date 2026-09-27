// P5-T13 acceptance tests 1-3 (component side): the weekly Claude commentary on the Reports page.
import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CommentaryStatus, WeeklyReportOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { Commentary, NO_REPORT_TEXT, STATUS_REASONS, commentaryParagraphs } from "./Commentary";

function renderCommentary(report: WeeklyReportOut | null) {
  const api = new FakeApiClient({ weeklyReport: report });
  return renderWithProviders(<Commentary monday="2026-11-23" />, { api });
}

describe("Commentary", () => {
  it("renders the paragraphs, the MT time, the model and the cost", async () => {
    const r = renderCommentary(fx.weeklyReportOk);
    const card = await screen.findByRole("region", { name: "Commentary" });
    expect(r.api.callsTo("weeklyReport")).toEqual([["2026-11-23"]]);
    const paragraphs = card.querySelectorAll("p.commentary-paragraph");
    expect(paragraphs).toHaveLength(2);
    expect(paragraphs[0]).toHaveTextContent(/^A short holiday week with 4 sessions and 4 trades\./);
    expect(paragraphs[1]).toHaveTextContent(/^Risk stayed small/);
    // 2026-11-28T14:00:20Z through format.ts (tests pin the display zone to a fixed UTC-6 "MT", test/setup.ts).
    expect(card).toHaveTextContent("Generated 2026-11-28 08:00 MT by claude-sonnet-5, US$0.0123");
  });

  it("a <script> inside the commentary renders as text, never as markup; links are not made clickable", async () => {
    const hostile = "<script>alert(1)</script> and <img src=x onerror=alert(2)>\n\nSee javascript:alert(3) or https://example.com";
    renderCommentary({ ...fx.weeklyReportOk, commentary: hostile });
    const card = await screen.findByRole("region", { name: "Commentary" });
    expect(card).toHaveTextContent("<script>alert(1)</script> and <img src=x onerror=alert(2)>");
    expect(card).toHaveTextContent("See javascript:alert(3) or https://example.com");
    expect(document.querySelectorAll("script, img, a")).toHaveLength(0);
  });

  it("with the budget fixture shows the budget reason and no commentary card body", async () => {
    renderCommentary(fx.weeklyReportBudget);
    expect(await screen.findByText("The daily Claude budget was used up")).toHaveClass("muted");
    expect(screen.queryByRole("region", { name: "Commentary" })).toBeNull();
    expect(document.querySelectorAll("p.commentary-paragraph")).toHaveLength(0);
    expect(document.body).not.toHaveTextContent("Generated");
  });

  it.each<[Exclude<CommentaryStatus, "ok">, string]>([
    ["budget", "The daily Claude budget was used up"],
    ["disabled", "Claude is not configured"],
    ["rejected", "The commentary quoted numbers that are not in the report, so it was withheld"],
    ["error", "Claude failed"],
  ])("status %s shows its one-line reason", async (status, reason) => {
    expect(STATUS_REASONS[status]).toBe(reason);
    renderCommentary({ ...fx.weeklyReportBudget, commentary_status: status });
    expect(await screen.findByText(reason)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Commentary" })).toBeNull();
  });

  it("status ok with an empty text is treated as a failure, not an empty card", async () => {
    renderCommentary({ ...fx.weeklyReportOk, commentary: "  \n\n " });
    expect(await screen.findByText("Claude failed")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Commentary" })).toBeNull();
  });

  it("with no report (404) says none was written yet", async () => {
    renderCommentary(null);
    expect(await screen.findByText(NO_REPORT_TEXT)).toBeInTheDocument();
    expect(NO_REPORT_TEXT).toBe("No weekly report for this week yet (it is written on Saturday morning).");
  });

  it("an API error shows the error box with a retry", async () => {
    const api = new FakeApiClient();
    api.fail("weeklyReport", new Error("boom"));
    renderWithProviders(<Commentary monday="2026-11-23" />, { api });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("the reports topic (the weeklyReport query prefix) refreshes it", async () => {
    const r = renderCommentary(null);
    await screen.findByText(NO_REPORT_TEXT);
    r.api.set("weeklyReport", fx.weeklyReportOk);
    await r.queryClient.invalidateQueries({ queryKey: ["weeklyReport"] });
    expect(await screen.findByRole("region", { name: "Commentary" })).toBeInTheDocument();
    await waitFor(() => expect(r.api.callsTo("weeklyReport")).toHaveLength(2));
  });
});

describe("commentaryParagraphs", () => {
  it("splits on blank lines, trims, and drops empty paragraphs", () => {
    expect(commentaryParagraphs("one\ntwo\n\n\n three \r\n\r\nfour\n \n")).toEqual(["one\ntwo", "three", "four"]);
    expect(commentaryParagraphs("")).toEqual([]);
    expect(commentaryParagraphs(null)).toEqual([]);
  });
});
