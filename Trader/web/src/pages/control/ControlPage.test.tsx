// DB-T9 acceptance tests for the Control page as a whole: every section from `controlOut` (1), Pause /
// Resume with refetch (2), approval mode and kill-switch reset through the reused components (3), the
// strategy toggle (4), part errors (7), the behaviours carried over from SystemPage.test.tsx (9), and touch
// targets, the 390 px layout and D9's flat style (10).
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/client";
import type { ControlOut, KillSwitchesOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { FakeApiClient } from "../../test/fakeApi";
import { renderWithProviders } from "../../test/render";
import ControlPage from "../Control";
import { TELEGRAM_OFF_TEXT, WORKER_DOWN_TEXT } from "../system/StatusCards";

const SECTIONS = ["Engine", "Kill switches", "Strategies", "Today's schedule and jobs", "Health", "Soak", "Error log"];

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

async function renderPage(api: FakeApiClient = new FakeApiClient()) {
  const result = renderWithProviders(<ControlPage />, { api, route: "/control" });
  expect(await screen.findByRole("heading", { name: "Control", level: 1 })).toBeInTheDocument();
  await screen.findByRole("region", { name: "Engine" });
  await settle();
  return result;
}

function region(name: string) {
  return screen.getByRole("region", { name });
}

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

describe("Control page", () => {
  it("1. renders every section from controlOut under the heading Control, in design order", async () => {
    const { api } = await renderPage();
    const regions = SECTIONS.map((name) => region(name));
    for (let i = 1; i < regions.length; i += 1) {
      // each section follows the one before it in the document
      expect(regions[i - 1]!.compareDocumentPosition(regions[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(within(region("Engine")).getByText("Running")).toBeInTheDocument();
    expect(within(region("Kill switches")).getByRole("list", { name: "Kill switch lights" })).toBeInTheDocument();
    expect(within(region("Strategies")).getByText("orb_sip")).toBeInTheDocument();
    expect(within(region("Strategies")).getByText("spy_overlay")).toBeInTheDocument();
    expect(within(region("Today's schedule and jobs")).getByText("Pre-market scan")).toBeInTheDocument();
    expect(within(region("Health")).getByText("543 of 543 bars in 31.2 s")).toBeInTheDocument();
    expect(within(region("Soak")).getByText("3 / 10 clean")).toBeInTheDocument();
    expect(within(region("Error log")).getByText("Candle archive failed for 1 symbol")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Watchlist upload" })).toBeInTheDocument();
    expect(api.callsTo("control")).toHaveLength(1);
    // the page reads only its aggregate and the reused components' own reads: never the old System route
    expect(api.callsTo("system")).toHaveLength(0);
    expect(api.callsTo("dashboard")).toHaveLength(0);
  });

  it("2. Pause: confirm, api.pause() once, the page refetches; cancelling calls nothing; Resume likewise when paused", async () => {
    const api = new FakeApiClient();
    await renderPage(api);
    const engine = region("Engine");
    await userEvent.click(within(engine).getByRole("button", { name: "Pause" }));
    await userEvent.click(within(engine).getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("pause")).toHaveLength(0);
    await userEvent.click(within(engine).getByRole("button", { name: "Pause" }));
    await userEvent.click(within(engine).getByRole("button", { name: "Pause new entries" }));
    await waitFor(() => expect(api.callsTo("pause")).toHaveLength(1));
    await waitFor(() => expect(api.callsTo("control")).toHaveLength(2));
    expect(api.callsTo("resume")).toHaveLength(0);
  });

  it("2. Resume when paused: confirm, api.resume() once, the page refetches; cancelling calls nothing", async () => {
    const paused = lfx.controlWith({ engine: { ...lfx.engineOut, trading: "paused", paused_at: "2026-10-06T14:01:00Z" } });
    const api = new FakeApiClient({ control: paused });
    await renderPage(api);
    const engine = region("Engine");
    expect(within(engine).queryByRole("button", { name: "Pause" })).toBeNull();
    await userEvent.click(within(engine).getByRole("button", { name: "Resume" }));
    await userEvent.click(within(engine).getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("resume")).toHaveLength(0);
    await userEvent.click(within(engine).getByRole("button", { name: "Resume" }));
    await userEvent.click(within(engine).getByRole("button", { name: "Resume new entries" }));
    await waitFor(() => expect(api.callsTo("resume")).toHaveLength(1));
    await waitFor(() => expect(api.callsTo("control")).toHaveLength(2));
    expect(api.callsTo("pause")).toHaveLength(0);
  });

  it("3. approval mode and kill-switch reset go through the reused components", async () => {
    const tripped: KillSwitchesOut = {
      switches: fx.killswitchStates.map((s) => (s.switch === "max_drawdown_pct" ? { ...s, tripped: true, tripped_at: "2026-10-06T14:00:00Z", value: "0.2100", threshold: "0.2000", automatic: true, needs_web_reset: true } : s)),
      history: fx.killswitchEvents,
    };
    const api = new FakeApiClient({ killswitches: tripped });
    await renderPage(api);
    const group = within(region("Engine")).getByRole("group", { name: "Approval mode" });
    await userEvent.click(within(group).getByRole("button", { name: "Auto" }));
    expect(api.callsTo("putSetting")).toHaveLength(0);
    await userEvent.click(within(region("Engine")).getByRole("button", { name: "Switch to Auto" }));
    await waitFor(() => expect(api.callsTo("putSetting")).toEqual([["approval_mode", "auto"]]));

    const ks = region("Kill switches");
    await userEvent.click(within(ks).getByRole("button", { name: "Reset Max drawdown" }));
    await userEvent.type(within(ks).getByLabelText("Reason for the reset"), "ok");
    expect(within(ks).getByRole("button", { name: "Reset…" })).toBeDisabled();
    await userEvent.type(within(ks).getByLabelText("Reason for the reset"), " now, books checked");
    await userEvent.click(within(ks).getByRole("button", { name: "Reset…" }));
    await userEvent.click(within(ks).getByRole("button", { name: "Confirm reset" }));
    await waitFor(() => expect(api.callsTo("resetKillSwitch")).toEqual([["max_drawdown_pct", { reason: "ok now, books checked" }]]));
  });

  it("4. the strategy toggle calls putStrategy(key, {enabled: false}) only after the confirm", async () => {
    const api = new FakeApiClient();
    await renderPage(api);
    const strategies = region("Strategies");
    await userEvent.click(within(strategies).getByRole("button", { name: "Turn off orb_sip" }));
    expect(api.callsTo("putStrategy")).toHaveLength(0);
    expect(within(strategies).getByRole("alertdialog")).toHaveTextContent("owns open positions");
    await userEvent.click(within(strategies).getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("putStrategy")).toHaveLength(0);
    await userEvent.click(within(strategies).getByRole("button", { name: "Turn off orb_sip" }));
    await userEvent.click(within(strategies).getByRole("button", { name: "Yes, turn off orb_sip" }));
    await waitFor(() => expect(api.callsTo("putStrategy")).toEqual([["orb_sip", { enabled: false }]]));
    await waitFor(() => expect(api.callsTo("control")).toHaveLength(2));
  });

  it("7. a null part with a part error shows that card's error and Retry; the rest renders", async () => {
    const failed: ControlOut = lfx.controlWith({ soak: null, part_errors: [{ part: "soak", message: "OperationalError: soak could not be read" }] });
    const api = new FakeApiClient({ control: failed });
    await renderPage(api);
    const soak = region("Soak");
    expect(within(soak).getByRole("alert")).toHaveTextContent("OperationalError: soak could not be read");
    for (const name of SECTIONS.filter((s) => s !== "Soak")) {
      expect(within(region(name)).queryByText("OperationalError: soak could not be read")).toBeNull();
    }
    expect(within(region("Health")).getByText("543 of 543 bars in 31.2 s")).toBeInTheDocument();
    api.set("control", lfx.controlOut);
    await userEvent.click(within(soak).getByRole("button", { name: "Retry" }));
    expect(await within(region("Soak")).findByText("3 / 10 clean")).toBeInTheDocument();
    expect(api.callsTo("control")).toHaveLength(2);
  });

  it("7. every part failed: each card shows its own error; the page frame stays", async () => {
    const parts = ["engine", "killswitches", "strategies", "schedule", "health", "soak", "errors"] as const;
    const failed = lfx.controlWith({
      engine: null,
      killswitches: null,
      killswitch_history: null,
      strategies: null,
      schedule: null,
      health: null,
      soak: null,
      errors: null,
      part_errors: parts.map((part) => ({ part, message: `OperationalError: ${part} could not be read` })),
    });
    await renderPage(new FakeApiClient({ control: failed }));
    const cards: [string, string][] = [
      ["Engine", "engine"],
      ["Kill switches", "killswitches"],
      ["Strategies", "strategies"],
      ["Today's schedule and jobs", "schedule"],
      ["Health", "health"],
      ["Soak", "soak"],
      ["Error log", "errors"],
    ];
    for (const [name, part] of cards) {
      expect(within(region(name)).getAllByRole("alert")[0]).toHaveTextContent(`OperationalError: ${part} could not be read`);
    }
    expect(screen.getByRole("heading", { name: "Watchlist upload" })).toBeInTheDocument();
  });

  it("a whole-request failure shows the error box with Retry; then the page", async () => {
    const api = new FakeApiClient().fail("control", new ApiError(503, "unavailable", "Database unavailable"));
    renderWithProviders(<ControlPage />, { api, route: "/control" });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable");
    expect(screen.getByRole("heading", { name: "Control", level: 1 })).toBeInTheDocument();
    api.succeed("control");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("region", { name: "Engine" })).toBeInTheDocument();
  });

  it("a failed refetch keeps the last good data under the error box", async () => {
    const api = new FakeApiClient();
    const { queryClient } = await renderPage(api);
    api.fail("control", new ApiError(503, "unavailable", "Database unavailable"));
    await queryClient.refetchQueries({ queryKey: ["system", "control"] }).catch(() => undefined);
    expect(await screen.findByText("Database unavailable")).toBeInTheDocument();
    expect(region("Engine")).toBeInTheDocument();
    expect(within(region("Soak")).getByText("3 / 10 clean")).toBeInTheDocument();
  });

  it("9. carried over from the System page: worker down, Telegram off, failed sends, the watchlist upload", async () => {
    const base = lfx.controlStaleWorker;
    const api = new FakeApiClient({ control: { ...base, health: { ...base.health!, telegram_configured: false } } });
    await renderPage(api);
    const healthRegion = region("Health");
    expect(within(healthRegion).getByText(WORKER_DOWN_TEXT)).toBeInTheDocument();
    expect(within(healthRegion).getByText(TELEGRAM_OFF_TEXT)).toBeInTheDocument();
    const sends = within(healthRegion).getByRole("table", { name: "Failed Telegram sends" });
    expect(within(sends).getByText("TimedOut")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Watchlist upload" })).toBeInTheDocument();
    expect(screen.getByLabelText("CSV file")).toBeInTheDocument();
  });

  it("free text everywhere renders as plain text", async () => {
    const { control } = lfx.withXssText();
    const { container } = await renderPage(new FakeApiClient({ control }));
    expect(within(region("Error log")).getByText(`HTTP 429 from Questrade, paused 1.2 s${lfx.XSS}`)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("10. 44 px touch targets, labelled controls and nothing wider than a 390 px phone", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    window.dispatchEvent(new Event("resize"));
    await renderPage();
    // open the optional parts too, so their controls are checked
    await userEvent.click(within(region("Error log")).getByRole("button", { name: "Older events" }));
    await userEvent.click(within(region("Today's schedule and jobs")).getByRole("button", { name: "Re-run Pre-market scan" }));
    await settle();
    const buttons = Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
    expect(buttons.length).toBeGreaterThan(10);
    for (const button of buttons) {
      const name = (button.getAttribute("aria-label") ?? button.textContent ?? "").trim();
      expect(name, "a button without a name").not.toBe("");
      expect(parseFloat(button.style.minHeight || "0"), `button "${name}" under 44 px`).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    }
    for (const control of Array.from(document.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("input, select, textarea"))) {
      const labelled = (control.labels?.length ?? 0) > 0 || control.hasAttribute("aria-label");
      expect(labelled, `unlabelled <${control.tagName.toLowerCase()} id=${control.id}>`).toBe(true);
      if (control instanceof HTMLSelectElement) {
        expect(parseFloat(control.style.minHeight || "0"), `select ${control.id} under 44 px`).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
      }
    }
    for (const a of Array.from(document.querySelectorAll("a"))) {
      expect(["link-touch", "btn"].some((c) => a.classList.contains(c)), `link "${a.textContent}" has no touch class`).toBe(true);
    }
    for (const table of Array.from(document.querySelectorAll("table"))) {
      expect(table.parentElement?.classList.contains("table-scroll"), "a table outside .table-scroll").toBe(true);
    }
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("[style]"))) {
      for (const prop of ["width", "minWidth"] as const) {
        const px = /^(\d+(?:\.\d+)?)px$/.exec(el.style[prop]);
        if (px) expect(Number(px[1]), `${el.tagName} ${prop}`).toBeLessThanOrEqual(390);
      }
    }
  });

  it("10. control.css: flat (no shadow, gradient or glow), colours only from tokens, no fixed width over 390 px, one column on a phone", () => {
    const css = readWebFile("src/pages/control/control.css");
    expect(css).not.toMatch(/box-shadow\s*:(?!\s*none)/);
    expect(css).not.toMatch(/gradient\(/);
    expect(css).not.toMatch(/text-shadow/);
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(css).not.toMatch(/rgba?\(/);
    expect(css).not.toMatch(/--money-/);
    for (const m of css.matchAll(/(?:^|[\s;{])(?:min-)?width\s*:\s*(\d+)px/g)) {
      expect(Number(m[1])).toBeLessThanOrEqual(390);
    }
    expect(css).toMatch(/\.control-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  });
});
