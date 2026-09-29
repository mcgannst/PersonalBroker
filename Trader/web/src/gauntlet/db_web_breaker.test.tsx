// DB-GWEB gauntlet (attempt 1): breaker tests for the live dashboard components (DB-T7, DB-T8) and the
// Control page (DB-T9), written against the design (2026-09-28-live-dashboard-design.md D1-D10, §3, §4, §7)
// and the plan (S9-S16). FakeApiClient + liveFixtures only; no network.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { ActivityItemOut, KillSwitchesOut, LiveOut, LivePositionOut, LiveRange, RiskOut } from "../api/types";
import { DEFAULT_DISPLAY_ZONE, setDisplayZone } from "../lib/format";
import ControlPage from "../pages/Control";
import { ActivityFeed } from "../pages/live/ActivityFeed";
import { EquityChart } from "../pages/live/EquityChart";
import { Money, PeriodPnl } from "../pages/live/PeriodPnl";
import { PositionRow } from "../pages/live/PositionRow";
import { PositionsTable } from "../pages/live/PositionsTable";
import { RejectionsPanel } from "../pages/live/RejectionsPanel";
import { RiskPanel } from "../pages/live/RiskPanel";
import { TodayTimeline } from "../pages/live/TodayTimeline";
import { TopBar, partError } from "../pages/live/TopBar";
import * as fx from "../test/fixtures";
import * as lfx from "../test/liveFixtures";
import { FakeApiClient } from "../test/fakeApi";
import { renderWithProviders } from "../test/render";

const NOW = Date.parse(fx.SERVER_TIME);

afterEach(() => {
  setDisplayZone({ zone: DEFAULT_DISPLAY_ZONE, label: "MT" });
});

function readWebFile(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return readFileSync(found, "utf8");
}

/** The DB-T7 and DB-T8 components composed as DB-T11's page layout says (TopBar embeds CostBar and
 * BooksCheck; PeriodPnl is not in the layout), each with its own expand/range state. */
function Dash({ live, onRetry = () => undefined, initialExpanded = [] }: { live: LiveOut; onRetry?: () => void; initialExpanded?: number[] }) {
  const [expanded, setExpanded] = useState<number[]>(initialExpanded);
  const [range, setRange] = useState<LiveRange>("today");
  const err = (part: string) => partError(live, part);
  return (
    <div data-testid="dash">
      <TopBar live={live} connected updatedAt={NOW} nowMs={NOW} />
      <EquityChart equity={live.equity} range={range} onRange={setRange} error={err("equity")} onRetry={onRetry} />
      <RiskPanel risk={live.risk} error={err("risk")} onRetry={onRetry} />
      <PositionsTable
        positions={live.positions}
        closedToday={live.closed_today}
        staleAfterSeconds={live.marks_stale_seconds}
        expanded={expanded}
        onExpand={setExpanded}
        error={err("positions")}
        onRetry={onRetry}
      />
      <ActivityFeed items={live.activity} error={err("activity")} onRetry={onRetry} />
      <RejectionsPanel rejections={live.rejections} error={err("rejections")} onRetry={onRetry} />
      <TodayTimeline timeline={live.timeline} session={live.session} error={err("timeline")} onRetry={onRetry} />
    </div>
  );
}

/** A controlled PositionsTable that records every `onExpand`. */
function Positions({ positions, initial = [], record }: { positions: LivePositionOut[]; initial?: number[]; record?: (ids: number[]) => void }) {
  const [expanded, setExpanded] = useState<number[]>(initial);
  return (
    <PositionsTable
      positions={positions}
      closedToday={2}
      staleAfterSeconds={30}
      expanded={expanded}
      onExpand={(ids) => {
        record?.(ids);
        setExpanded(ids);
      }}
    />
  );
}

function rowIds(root: ParentNode = document): number[] {
  return Array.from(root.querySelectorAll<HTMLLIElement>("li.pos-row")).map((li) => Number(li.dataset.id));
}

function summaryOf(id: number): HTMLButtonElement {
  const b = document.querySelector<HTMLButtonElement>(`li.pos-row[data-id="${id}"] > button.pos-summary`);
  if (!b) throw new Error(`no row ${id}`);
  return b;
}

async function settle(): Promise<void> {
  await waitFor(() => expect(screen.queryAllByRole("status", { name: "Loading" })).toHaveLength(0));
}

async function renderControl(api: FakeApiClient = new FakeApiClient()) {
  const r = renderWithProviders(<ControlPage />, { api, route: "/control" });
  await screen.findByRole("heading", { name: "Control", level: 1 });
  await screen.findByRole("region", { name: "Engine" });
  await settle();
  return r;
}

function region(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

const MONEY_TEXT = /^[+-]?-?\$[\d,]+\.\d{2}$/;

/** Touch-target problems under `root`: buttons, selects and text inputs need an inline min-height ≥ 44 px
 * (the shared Button sets it), links a touch class, checkboxes a `.check-row` label. */
function touchProblems(root: ParentNode): string[] {
  const bad: string[] = [];
  const minH = (el: Element) => parseFloat((el as HTMLElement).style.minHeight || "0");
  for (const b of Array.from(root.querySelectorAll("button"))) if (!(minH(b) >= 44)) bad.push(`button "${b.textContent?.trim()}"`);
  for (const a of Array.from(root.querySelectorAll("a"))) {
    if (!["link-touch", "btn", "nav-link"].some((c) => a.classList.contains(c))) bad.push(`link "${a.textContent?.trim()}"`);
  }
  for (const s of Array.from(root.querySelectorAll("select, input[type=date], input[type=text], input:not([type])"))) {
    if (!(minH(s) >= 44)) bad.push(`${s.tagName.toLowerCase()} #${s.id}`);
  }
  for (const box of Array.from(root.querySelectorAll<HTMLInputElement>("input[type=checkbox]"))) {
    if (!box.closest("label")?.classList.contains("check-row")) bad.push(`checkbox ${box.id}`);
  }
  return bad;
}

/** Fixed widths wider than a 390 px phone in a stylesheet's declarations (media query conditions ignored). */
function wideDeclarations(css: string): string[] {
  const out: string[] = [];
  const decl = /(?:^|[;{])\s*((?:min-|max-)?width|flex-basis|grid-template-columns)\s*:\s*([^;}]+)/g;
  for (const m of css.matchAll(decl)) {
    if (m[1] === "max-width") continue;
    const fixed = Array.from(m[2]!.matchAll(/(\d+(?:\.\d+)?)px/g)).reduce((s, x) => s + Number(x[1]), 0);
    if (fixed > 390) out.push(`${m[1]}: ${m[2]}`);
  }
  return out;
}

describe("DB-GWEB breaker: positions (D2)", () => {
  it("B1. 0, 1 and 20 positions: empty state, default and stop sort (missing last), near-stop highlight, stale badges never hide rows", async () => {
    // 0: the empty state with today's closed count
    const zero = renderWithProviders(<Positions positions={lfx.positionsN(0)} />);
    expect(screen.getByText("No open positions")).toBeInTheDocument();
    expect(screen.getByText("2 trades closed today")).toBeInTheDocument();
    zero.unmount();

    // 1
    const one = renderWithProviders(<Positions positions={lfx.positionsN(1)} />);
    expect(rowIds()).toEqual([100]);
    one.unmount();

    // 20 live: sparklines, unrealised $ descending by default
    const twenty = lfx.positionsN(20);
    const r20 = renderWithProviders(<Positions positions={twenty} />);
    expect(rowIds()).toHaveLength(20);
    expect(document.querySelectorAll("li.pos-row svg.sparkline")).toHaveLength(20);
    const byUnrealised = [...twenty].sort((a, b) => Number(b.unrealized) - Number(a.unrealized) || a.id - b.id).map((p) => p.id);
    expect(rowIds()).toEqual(byUnrealised);
    expect(screen.getByRole("button", { name: "Unrealised $" })).toHaveAttribute("aria-pressed", "true");
    r20.unmount();

    // 20 stale (last one missing): every row stays, each has its badge, the panel has one; stop sort puts missing last
    const stale = lfx.positionsN(20, { staleMarks: true });
    const rs = renderWithProviders(<Positions positions={stale} />);
    expect(rowIds()).toHaveLength(20);
    const rowBadges = document.querySelectorAll("li.pos-row .live-badge");
    expect(rowBadges).toHaveLength(20);
    expect(screen.getAllByText("prices stale")).toHaveLength(21);
    await userEvent.click(screen.getByRole("button", { name: "Distance to stop" }));
    const ids = rowIds();
    expect(ids[ids.length - 1]).toBe(119); // the missing mark
    const withDistance = stale.filter((p) => p.distance_to_stop_r !== null);
    const ascending = [...withDistance].sort((a, b) => Number(a.distance_to_stop_r) - Number(b.distance_to_stop_r) || a.id - b.id).map((p) => p.id);
    expect(ids.slice(0, 19)).toEqual(ascending);
    // the missing row still shows its ticker and a dash, never a $0.00
    const missingRow = document.querySelector('li.pos-row[data-id="119"]')!;
    expect(missingRow.textContent).toContain(stale[19]!.ticker);
    expect(missingRow.querySelector(".money")).toBeNull();
    rs.unmount();

    // near stop: only the near-stop row is highlighted and announced
    const mixed = [...lfx.positionsN(3).filter((p) => !p.near_stop), { ...lfx.positionsN(1, { nearStop: true })[0]!, id: 999, ticker: "NEAR" }];
    expect(mixed.length).toBeGreaterThanOrEqual(3);
    renderWithProviders(<Positions positions={mixed} />);
    const near = document.querySelectorAll("li.pos-row.near-stop");
    expect(near).toHaveLength(1);
    expect((near[0] as HTMLElement).dataset.id).toBe("999");
    expect(within(near[0] as HTMLElement).getByText("near stop")).toHaveClass("live-sr-only");
  });

  it("B2. expand: at most 3 open, a 4th tap drops the oldest, a tap collapses, a re-sort keeps them, one chart each", async () => {
    const calls: number[][] = [];
    renderWithProviders(<Positions positions={lfx.positionsN(5)} record={(ids) => calls.push(ids)} />);
    for (const id of [100, 101, 102, 103]) await userEvent.click(summaryOf(id));
    expect(calls.map((c) => c.length).every((n) => n <= 3)).toBe(true);
    expect(calls[calls.length - 1]).toEqual([101, 102, 103]);
    expect(summaryOf(100)).toHaveAttribute("aria-expanded", "false");
    for (const id of [101, 102, 103]) expect(summaryOf(id)).toHaveAttribute("aria-expanded", "true");
    expect(document.querySelectorAll(".pos-expanded")).toHaveLength(3);
    expect(document.querySelectorAll(".pos-chart")).toHaveLength(3);
    await userEvent.click(summaryOf(102));
    expect(calls[calls.length - 1]).toEqual([101, 103]);
    await userEvent.click(screen.getByRole("button", { name: "Distance to stop" }));
    expect(document.querySelectorAll(".pos-expanded")).toHaveLength(2);
    // aria-controls points at the details it opens
    const controls = summaryOf(101).getAttribute("aria-controls");
    expect(controls && document.getElementById(controls)).toBeTruthy();
  });

  it("B3. an expanded list longer than 3 (a hand-edited ?expand=) never shows more than 3 charts", () => {
    // The API refuses more than 3 expand ids (pattern {0,2}); the table is the last place that can hold the rule.
    renderWithProviders(<Positions positions={lfx.positionsN(5)} initial={[100, 101, 102, 103, 104]} />);
    expect(document.querySelectorAll(".pos-expanded").length).toBeLessThanOrEqual(3);
  });

  it("B4. phone (390 px): a row shows ticker, unrealised and sparkline only; nothing fixed wider than the phone; every target ≥ 44 px on both pages", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    const liveB = readWebFile("src/pages/live/liveB.css");
    const phone = /@media\s*\(max-width:\s*(\d+)px\)\s*\{([\s\S]*)\}\s*$/.exec(liveB);
    expect(phone, "liveB.css has a phone media block").toBeTruthy();
    expect(Number(phone![1])).toBeGreaterThanOrEqual(390);
    expect(phone![2]).toMatch(/\.pos-detail\s*\{[^}]*display:\s*none/);

    const live = lfx.liveWith({ positions: lfx.positionsN(20, { staleMarks: true, nearStop: false }), activity: [lfx.exitActivity, ...lfx.activity] });
    const r = renderWithProviders(<Dash live={live} initialExpanded={[100, 101, 102]} />);
    for (const summary of Array.from(document.querySelectorAll<HTMLButtonElement>("button.pos-summary"))) {
      const visible = Array.from(summary.children).filter((c) => !c.classList.contains("pos-detail"));
      expect(visible.map((c) => Array.from(c.classList).find((k) => k !== "pos-cell"))).toEqual(["pos-name", "pos-pnl", "pos-spark"]);
      // inside the name cell only the ticker (and badges / screen-reader text) is visible on a phone
      const nameVisible = Array.from(visible[0]!.children).filter((c) => !c.classList.contains("pos-detail"));
      for (const c of nameVisible) expect(["pos-ticker", "live-badge", "live-sr-only"].some((k) => c.classList.contains(k))).toBe(true);
      const pnlVisible = Array.from(visible[1]!.children).filter((c) => !c.classList.contains("pos-detail"));
      expect(pnlVisible.every((c) => c.classList.contains("money") || c.classList.contains("lva-dash") || c.classList.contains("muted"))).toBe(true);
    }
    // open every rejection rule and the books breakdown, then check layout and targets
    for (const b of screen.getAllByRole("button", { expanded: false })) await userEvent.click(b);
    for (const css of ["src/pages/live/liveA.css", "src/pages/live/liveB.css", "src/pages/control/control.css"]) {
      expect(wideDeclarations(readWebFile(css)), css).toEqual([]);
    }
    const inlineWide = Array.from(document.querySelectorAll<HTMLElement>("[style]")).filter((el) => {
      const w = /(\d+)px/.exec(el.style.width || el.style.minWidth || "");
      return w !== null && Number(w[1]) > 390;
    });
    expect(inlineWide).toHaveLength(0);
    for (const svg of Array.from(document.querySelectorAll("svg[width]"))) expect(Number(svg.getAttribute("width"))).toBeLessThanOrEqual(390);
    expect(touchProblems(document)).toEqual([]);
    r.unmount();

    // Control: open the reset form's neighbours (older events, re-run) and check the same
    await renderControl(new FakeApiClient({ killswitches: { switches: [fx.killswitchDrawdownTripped], history: fx.killswitchEvents } }));
    await userEvent.click(screen.getByRole("button", { name: "Older events" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-run Pre-market scan" }));
    await userEvent.click(within(region("Kill switches")).getByRole("button", { name: "Reset Max drawdown" }));
    await settle();
    expect(touchProblems(document)).toEqual([]);
    const ctlWide = Array.from(document.querySelectorAll<HTMLElement>("[style]")).filter((el) => /([4-9]\d{2}|\d{4,})px/.test(el.style.width || el.style.minWidth || ""));
    expect(ctlWide).toHaveLength(0);
  });
});

describe("DB-GWEB breaker: D9 colour rule", () => {
  it("B5. Dashboard: green/red only on P&L money; fees, Claude spend and balances stay neutral even when negative; lights never use money classes", () => {
    const live = lfx.liveWith({
      periods: lfx.periods.map((p) => ({ ...p, fees: "-2.5000", claude_usd: "-0.1000", pnl_after_fees: "-4.0000", net_after_ai: "-4.1000" })),
      claude_today: { ...lfx.claudeToday, spent_usd: "0.9500", used_fraction: "0.9500" },
      books: { ...lfx.booksBroken, cash: "-5.0000", difference: "-1.0000", fees_paid: "3.0000" },
      risk: { ...lfx.riskOut, open_risk: "40.0000", killswitches: lfx.killswitchLights.map((k, i) => (i === 0 ? { ...k, tripped: true, tripped_at: "2026-10-06T13:50:00Z" } : k)) },
      positions: lfx.positionsN(4, { staleMarks: true }),
      activity: [lfx.exitActivity, ...lfx.activity],
    });
    renderWithProviders(<Dash live={live} initialExpanded={[100]} />);
    fireEvent.click(screen.getByRole("button", { name: "Show breakdown" }));

    const coloured = Array.from(document.querySelectorAll("[class]")).filter((el) => /(^|\s)(up|down)(\s|$)/.test(el.getAttribute("class")!));
    expect(coloured.length).toBeGreaterThan(5);
    for (const el of coloured) {
      expect(el.classList.contains("money"), `"${el.textContent}" is green/red without .money`).toBe(true);
      expect(el.textContent!.trim()).toMatch(MONEY_TEXT);
    }
    // costs and balances: always flat
    for (const period of ["today", "week", "run"]) {
      const row = screen.getByTestId(`costs-${period}`);
      const [fees, claude] = Array.from(row.querySelectorAll(".money"));
      expect(fees).toHaveClass("flat");
      expect(claude).toHaveClass("flat");
    }
    for (const el of Array.from(screen.getByTestId("claude-today").querySelectorAll(".money"))) expect(el).toHaveClass("flat");
    const breakdown = screen.getByTestId("books-breakdown");
    for (const term of ["Cash", "Positions at cost", "Actual", "Starting cash", "Fees", "Expected", "Difference"]) {
      const dd = within(breakdown).getByText(term, { selector: "dt" }).nextElementSibling!;
      expect(dd.querySelector(".money"), term).toHaveClass("flat");
    }
    for (const el of Array.from(screen.getByTestId("open-risk").querySelectorAll(".money"))) expect(el).toHaveClass("flat");
    for (const el of Array.from(screen.getByTestId("equity-legend").querySelectorAll(".money"))) expect(el).toHaveClass("flat");
    // non-exit activity items never carry a money tone
    for (const li of Array.from(document.querySelectorAll("li.activity-item"))) {
      if ((li as HTMLElement).dataset.kind !== "exit") expect(li.querySelector(".money")).toBeNull();
    }
    // lights, chips, badges, meters and glyphs: status classes only
    for (const el of Array.from(document.querySelectorAll(".lva-chip, .lva-dot, .lva-glyph, .lva-meter-fill, .live-badge, .timeline-status"))) {
      expect(el.classList.contains("money") || el.classList.contains("up") || el.classList.contains("down"), el.className).toBe(false);
    }
    // the sparkline's stop line is a status colour, never the money red
    for (const line of Array.from(document.querySelectorAll("line.spark-stop"))) expect(line.getAttribute("stroke")).toBe("var(--status-bad)");
  });

  it("B6. Control: no green/red anywhere: no money tones and no status light in the --ok/--bad (money green/red) family", async () => {
    // tokens.css makes the legacy --ok/--bad the same green/red as --money-up/--money-down, and styles.css
    // colours .tone-ok/.tone-bad (the shared Light, used by the reused KillSwitchPanel) with them.
    const tokens = readWebFile("src/theme/tokens.css");
    const dark = /:root\[data-theme="dark"\]\s*\{([^}]*)\}/.exec(tokens)![1]!;
    const value = (name: string) => new RegExp(`${name}:\\s*([^;]+);`).exec(dark)![1]!.trim();
    expect(value("--ok")).toBe(value("--money-up"));
    expect(value("--bad")).toBe(value("--money-down"));
    expect(readWebFile("src/styles.css")).toMatch(/\.tone-ok\s*\{[^}]*var\(--ok\)/);

    await renderControl(new FakeApiClient({ killswitches: { switches: [...fx.killswitchStates.slice(0, 1), fx.killswitchDrawdownTripped], history: fx.killswitchEvents } }));
    expect(document.querySelectorAll(".money.up, .money.down")).toHaveLength(0);
    const greenRed = Array.from(document.querySelectorAll(".tone-ok, .tone-bad")).map((el) => `${el.className} "${el.textContent?.trim()}"`);
    expect(greenRed, "status lights on Control drawn in the money green/red").toEqual([]);
  });
});

describe("DB-GWEB breaker: XSS in every free-text field", () => {
  it("B7. Dashboard: tickers, strategy names, reasons, rules, activity and timeline text render inert", async () => {
    const { live } = lfx.withXssText();
    const bars = [
      { start: "2026-10-06T13:37:00Z", open: "21.56", high: "21.60", low: "21.50", close: "21.58", source: "marks" as const },
      { start: "2026-10-06T13:38:00Z", open: "21.58", high: "21.70", low: "21.55", close: "21.66", source: "candle" as const },
    ];
    const xlive: LiveOut = {
      ...live,
      positions: live.positions!.map((p) => ({ ...p, bars })),
      risk: { ...live.risk!, killswitches: live.risk!.killswitches.map((k) => ({ ...k, tripped: true, tripped_at: "2026-10-06T13:50:00Z" })) },
      part_errors: [{ part: "claude_today", message: `OperationalError: ${lfx.XSS}` }],
    };
    renderWithProviders(<Dash live={xlive} initialExpanded={[xlive.positions![0]!.id]} />);
    for (const b of screen.getAllByRole("button", { expanded: false })) await userEvent.click(b);
    const dash = screen.getByTestId("dash");
    expect(dash.querySelectorAll("img, script, iframe, object, embed")).toHaveLength(0);
    expect(dash.querySelectorAll("[onerror]")).toHaveLength(0);
    const text = dash.textContent ?? "";
    const occurrences = text.split(lfx.XSS).length - 1;
    // activity (13), rules (2) and their tickers, timeline labels, the ticker and strategy of the position, switch labels
    expect(occurrences).toBeGreaterThan(40);
    expect(within(screen.getByRole("list", { name: "Activity items" })).getAllByText((t) => t.includes(lfx.XSS)).length).toBe(13);
    expect(document.querySelector(".pos-ticker")!.textContent).toContain(lfx.XSS);
    expect(screen.getByText(`orb_sip${lfx.XSS}`)).toBeInTheDocument();
  });

  it("B8. Control: error log messages and sources, job summaries and details, strategy names, switch reasons, soak verdicts render inert", async () => {
    const { control } = lfx.withXssText();
    const xcontrol = {
      ...control,
      strategies: control.strategies!.map((s) => ({ ...s, key: `${s.key}${lfx.XSS}` })),
      health: { ...control.health!, opening_bars: { ...control.health!.opening_bars!, complete: false, errors: 2 } },
      soak: { ...control.soak!, today: { ...control.soak!.today!, failed: [`fills${lfx.XSS}`] } },
    };
    const ks: KillSwitchesOut = {
      switches: [{ ...fx.killswitchDrawdownTripped, label: `Max drawdown${lfx.XSS}`, clears: `clears${lfx.XSS}` }],
      history: fx.killswitchEvents.map((h) => ({ ...h, reset_reason: `reason${lfx.XSS}`, reset_by: `web:x${lfx.XSS}` })),
    };
    await renderControl(new FakeApiClient({ control: xcontrol, killswitches: ks }));
    await userEvent.click(screen.getByRole("button", { name: "Older events" }));
    await settle();
    const history = document.querySelector("details");
    if (history) history.open = true;
    const main = document.querySelector("main")!;
    expect(main.querySelectorAll("img, script, iframe, object, embed, [onerror]")).toHaveLength(0);
    const text = main.textContent ?? "";
    for (const piece of [
      `HTTP 429 from Questrade, paused 1.2 s${lfx.XSS}`, // error log message
      `market.data_service${lfx.XSS}`, // error log source
      `8 catalysts classified${lfx.XSS}`, // job summary
      `orb_sip${lfx.XSS}`, // strategy name
      `reason${lfx.XSS}`, // kill-switch reset reason (history)
      `clears${lfx.XSS}`,
      `clean so far${lfx.XSS}`, // soak verdict
      `fills${lfx.XSS}`,
    ]) {
      expect(text, piece).toContain(piece);
    }
  });

  it("B9. links from the API are never javascript: URLs (activity, rejection rules and tickers, positions, strategy settings)", async () => {
    const evil = "javascript:alert(1)";
    const live = lfx.liveWith({
      activity: [{ ...lfx.activity[0]!, link: evil } as ActivityItemOut],
      rejections: { ...lfx.rejections, rules: [{ ...lfx.rejections.rules[1]!, link: evil }] },
      positions: [{ ...lfx.livePosition, link: evil }],
    });
    renderWithProviders(<Dash live={live} initialExpanded={[lfx.livePosition.id]} />);
    await userEvent.click(screen.getByRole("button", { name: /gap_below_min/ }));
    const r = renderWithProviders(<ControlPage />, {
      api: new FakeApiClient({ control: lfx.controlWith({ strategies: lfx.strategyCards.map((s) => ({ ...s, settings_link: evil })) }) }),
      route: "/control",
    });
    await screen.findByRole("region", { name: "Strategies" });
    const hrefs = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href") ?? "");
    expect(hrefs.length).toBeGreaterThan(3);
    expect(hrefs.filter((h) => /^\s*javascript:/i.test(h))).toEqual([]);
    r.unmount();
  });
});

describe("DB-GWEB breaker: panel isolation (S15)", () => {
  it("B10. Control: one failed part shows only in its card with Retry; a whole-request failure keeps the last good data", async () => {
    const api = new FakeApiClient({
      control: lfx.controlWith({ health: null, part_errors: [{ part: "health", message: "OperationalError: health could not be read" }] }),
    });
    await renderControl(api);
    const health = region("Health");
    expect(within(health).getByText("OperationalError: health could not be read")).toBeInTheDocument();
    for (const name of ["Engine", "Kill switches", "Strategies", "Today's schedule and jobs", "Soak", "Error log"]) {
      expect(within(region(name)).queryByText(/could not be read/), name).toBeNull();
    }
    expect(within(region("Soak")).getByText("3 / 10 clean")).toBeInTheDocument();
    await userEvent.click(within(health).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.callsTo("control")).toHaveLength(2));

    api.set("control", lfx.controlOut);
    await userEvent.click(within(region("Health")).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(region("Health")).getByText("543 of 543 bars in 31.2 s")).toBeInTheDocument());
    api.fail("control", new ApiError(500, "internal", "The server failed."));
    // an SSE-style refetch of the page (any of the existing mutations invalidates ["system"])
    await userEvent.click(within(region("Engine")).getByRole("button", { name: "Pause" }));
    await userEvent.click(within(region("Engine")).getByRole("button", { name: "Pause new entries" }));
    await waitFor(() => expect(screen.getByText("The server failed.")).toBeInTheDocument());
    expect(within(screen.getByText("The server failed.").closest(".error-box") as HTMLElement).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(region("Health")).getByText("543 of 543 bars in 31.2 s")).toBeInTheDocument();
    expect(within(region("Soak")).getByText("3 / 10 clean")).toBeInTheDocument();
  });

  it("B11. Dashboard: every failed part shows its own error with Retry, including the top bar's P&L, costs and books", async () => {
    const onRetry = vi.fn();
    // PeriodPnl on its own (not in DB-T11's layout, but it has the contract's error/Retry props)
    const pp = renderWithProviders(<PeriodPnl periods={null} error="OperationalError: periods could not be read" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    pp.unmount();
    onRetry.mockClear();

    renderWithProviders(<Dash live={lfx.liveAllPartsFailed} onRetry={onRetry} />);
    for (const [name, part] of [
      ["Equity", "equity"],
      ["Risk", "risk"],
      ["Positions", "positions"],
      ["Activity", "activity"],
      ["Rejected today, and why", "rejections"],
      ["Today", "timeline"],
    ] as const) {
      const panels = screen.getAllByRole("region", { name }).filter((r) => !r.closest('[aria-label="Session"]'));
      const panel = panels[panels.length - 1]!;
      expect(within(panel).getByText(`OperationalError: ${part} could not be read`), name).toBeInTheDocument();
      await userEvent.click(within(panel).getByRole("button", { name: "Retry" }));
    }
    expect(onRetry).toHaveBeenCalledTimes(6);
    // the top bar (P&L, costs, books): each failed part is visible there and can be retried
    const session = screen.getByRole("region", { name: "Session" });
    const missing = ["periods", "books", "claude_today"].filter((part) => !within(session).queryByText(`OperationalError: ${part} could not be read`));
    const retries = within(session).queryAllByRole("button", { name: "Retry" }).length;
    expect({ missing, retries }).toEqual({ missing: [], retries: expect.any(Number) as number });
    expect(retries, "the top bar offers no Retry for its failed parts").toBeGreaterThanOrEqual(1);
  });
});

describe("DB-GWEB breaker: actions (D1)", () => {
  it("B12. Dashboard mutates nothing; on Control every action confirms (reset needs a typed reason) and a double click calls once", async () => {
    // Dashboard: tap every button there is; the API sees no call at all
    const dashApi = new FakeApiClient();
    const d = renderWithProviders(<Dash live={lfx.liveWith({ positions: lfx.positionsN(4), activity: [lfx.exitActivity, ...lfx.activity] })} />, { api: dashApi });
    for (let i = 0; i < 200; i += 1) {
      const buttons = Array.from(document.querySelectorAll("button"));
      if (i >= buttons.length) break;
      fireEvent.click(buttons[i]!);
    }
    expect(dashApi.calls).toEqual([]);
    d.unmount();

    const api = new FakeApiClient({ killswitches: { switches: [...fx.killswitchStates.filter((s) => s.switch !== "max_drawdown_pct"), fx.killswitchDrawdownTripped], history: [] } });
    await renderControl(api);
    const engine = region("Engine");

    // approval mode: Manual -> Auto only after its confirm (Auto -> Manual saves at once: Phase 4 decision)
    await userEvent.click(within(engine).getByRole("button", { name: "Auto" }));
    expect(api.callsTo("putSetting")).toHaveLength(0);
    await userEvent.dblClick(within(engine).getByRole("button", { name: "Switch to Auto" }));
    await waitFor(() => expect(api.callsTo("putSetting")).toEqual([["approval_mode", "auto"]]));

    // pause: confirm, then one call for a double click
    await userEvent.click(within(engine).getByRole("button", { name: "Pause" }));
    expect(api.callsTo("pause")).toHaveLength(0);
    await userEvent.dblClick(within(engine).getByRole("button", { name: "Pause new entries" }));
    await waitFor(() => expect(api.callsTo("pause")).toHaveLength(1));

    // kill-switch reset: a typed reason of at least 3 characters, a confirm, one call
    const ksr = region("Kill switches");
    await userEvent.click(within(ksr).getByRole("button", { name: "Reset Max drawdown" }));
    const reason = within(ksr).getByLabelText("Reason for the reset");
    await userEvent.type(reason, " x ");
    expect(within(ksr).getByRole("button", { name: "Reset…" })).toBeDisabled();
    await userEvent.type(reason, "checked the fills");
    await userEvent.click(within(ksr).getByRole("button", { name: "Reset…" }));
    expect(api.callsTo("resetKillSwitch")).toHaveLength(0);
    await userEvent.dblClick(within(ksr).getByRole("button", { name: "Confirm reset" }));
    await waitFor(() => expect(api.callsTo("resetKillSwitch")).toEqual([["max_drawdown_pct", { reason: "x checked the fills" }]]));

    // strategy toggle: confirm, one call
    const strat = region("Strategies");
    await userEvent.click(within(strat).getByRole("button", { name: "Turn off orb_sip" }));
    expect(api.callsTo("putStrategy")).toHaveLength(0);
    await userEvent.dblClick(within(strat).getByRole("button", { name: "Yes, turn off orb_sip" }));
    await waitFor(() => expect(api.callsTo("putStrategy")).toEqual([["orb_sip", { enabled: false }]]));

    // Re-run: only on items with `rerun`; it preselects RunJob and runs nothing by itself
    const jobs = region("Today's schedule and jobs");
    const rerunnable = lfx.schedule.filter((s) => s.rerun !== null).map((s) => `Re-run ${s.label}`);
    expect(within(jobs).getAllByRole("button", { name: /^Re-run / }).map((b) => b.getAttribute("aria-label"))).toEqual(rerunnable);
    await userEvent.click(within(jobs).getByRole("button", { name: "Re-run Pre-market scan" }));
    expect((within(jobs).getByLabelText("Job to run") as HTMLSelectElement).value).toBe("premarket");
    expect(api.callsTo("runJob")).toHaveLength(0);
    await userEvent.click(within(jobs).getByRole("button", { name: "Run" }));
    await userEvent.dblClick(within(jobs).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.callsTo("runJob")).toHaveLength(1));
    expect(api.callsTo("runJob")[0]![0]).toBe("premarket");
  });

  it("B13. the duplicate Pause/Resume: no Resume on Control is reachable without a confirmation (design §4.1)", async () => {
    const api = new FakeApiClient({ control: lfx.controlWith({ engine: { ...lfx.engineOut, trading: "paused", paused_at: "2026-10-06T14:01:00Z" } }) });
    await renderControl(api);
    // two Pause/Resume sets on one page: EngineCard's and the reused KillSwitchPanel's
    const resumes = screen.getAllByRole("button", { name: "Resume" });
    expect(resumes.length).toBeGreaterThanOrEqual(1);
    for (const b of resumes) {
      await userEvent.click(b);
      expect(api.callsTo("resume"), `Resume in "${b.closest("section")?.getAttribute("aria-label")}" called the API without a confirm`).toHaveLength(0);
      const cancel = screen.queryAllByRole("button", { name: "Cancel" });
      for (const c of cancel) await userEvent.click(c);
    }
  });
});

describe("DB-GWEB breaker: numbers and times", () => {
  it("B14. decimal strings format without float drift: 0.1+0.2 style, negative zero, half-cent, exponent and huge values", () => {
    const cases: [string, string, "up" | "down" | "flat"][] = [
      ["0.30000000000000004", "+$0.30", "up"],
      ["-0.0000", "$0.00", "flat"],
      ["-0.004", "$0.00", "flat"],
      ["0.005", "+$0.01", "up"],
      ["-0.005", "-$0.01", "down"],
      ["1234567890123.455", "+$1,234,567,890,123.46", "up"],
      ["99999999999999.9999", "+$100,000,000,000,000.00", "up"],
      ["-1E+2", "-$100.00", "down"],
    ];
    for (const [value, text, tone] of cases) {
      const r = renderWithProviders(<Money value={value} signed />);
      const el = r.container.querySelector(".money")!;
      expect(el.textContent, value).toBe(text);
      expect(el, value).toHaveClass(tone);
      r.unmount();
    }
    // a whole period column: realised + open add up on the server; the page shows each exactly
    renderWithProviders(
      <PeriodPnl
        periods={[{ ...lfx.periods[2]!, realized: "0.1000", unrealized: "0.2000", pnl_after_fees: "0.3000", win_rate: "0.3333", expectancy_r: "0.0050" }]}
      />,
    );
    const col = screen.getByTestId("period-run");
    expect(col.textContent).toContain("+$0.30");
    expect(col.textContent).toContain("33.33%");
    expect(col.textContent).toContain("+0.01R");
  });

  it("B15. no float rounding in component-level formatting: R to 2 dp and tiny negative P&L match the shared decimal formatter", () => {
    // RiskPanel's r2 uses Number.toFixed: 0.705 is 0.70499... as a float
    const risk: RiskOut = { ...lfx.riskOut, killswitches: lfx.killswitchLights.map((k) => (k.unit === "r" ? { ...k, value: "0.7050" } : k)) };
    renderWithProviders(<RiskPanel risk={risk} />);
    expect(screen.getByTestId("killswitch-expectancy").textContent).toContain("0.71 R");
    // PositionRow's own Money colours a value that rounds to $0.00 red
    const p: LivePositionOut = { ...lfx.livePosition, unrealized: "-0.0040" };
    const r = renderWithProviders(<PositionRow p={p} expanded={false} onToggle={() => undefined} />);
    const money = r.container.querySelector(".pos-pnl .money")!;
    expect(money.textContent).toBe("$0.00");
    expect(money).toHaveClass("flat");
  });

  it("B16. MT times on both sides of the DST change, and a closed day's next-session date", () => {
    const items: ActivityItemOut[] = [
      { ...lfx.activity[5]!, id: "fill:a", ts: "2026-10-30T13:36:00Z", text: "before" },
      { ...lfx.activity[5]!, id: "fill:b", ts: "2026-11-02T13:36:00Z", text: "after" },
    ];
    // fixed offset (the shell's mode once tzdata 2026c keeps Alberta on UTC-6): 07:36 MT on both sides
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" });
    const a = renderWithProviders(<ActivityFeed items={items} />);
    const times = Array.from(a.container.querySelectorAll(".activity-time")).map((t) => t.textContent);
    expect(times).toEqual(["07:36 MT", "07:36 MT"]);
    a.unmount();
    // zone mode: whatever the engine's Edmonton rule is, never UTC and always labelled MT
    setDisplayZone({ zone: DEFAULT_DISPLAY_ZONE, label: "MT" });
    const f = new Intl.DateTimeFormat("en-US", { timeZone: DEFAULT_DISPLAY_ZONE, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    const b = renderWithProviders(<ActivityFeed items={items} />);
    const zoned = Array.from(b.container.querySelectorAll(".activity-time")).map((t) => t.textContent);
    expect(zoned).toEqual(items.map((i) => `${f.format(new Date(i.ts)).replace(/^24/, "00")} MT`));
    expect(zoned.some((t) => t?.startsWith("13:36"))).toBe(false);
    b.unmount();
    // a tripped switch's time on the risk panel is MT too
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" });
    const risk: RiskOut = { ...lfx.riskOut, killswitches: lfx.killswitchLights.map((k, i) => (i === 0 ? { ...k, tripped: true, tripped_at: "2026-11-02T14:05:00Z" } : k)) };
    const rp = renderWithProviders(<RiskPanel risk={risk} />);
    expect(screen.getByText("Tripped 08:05 MT")).toBeInTheDocument();
    rp.unmount();

    // closed days: the API's session.date is the NEXT session (market.sessions.current_session)
    const saturday = lfx.liveWith({ ...lfx.liveEmptyDay, session: { date: "2026-10-12", phase: "closed_day", is_session: false, open_at: null, close_at: null } });
    const t = renderWithProviders(<TodayTimeline timeline={saturday.timeline} session={saturday.session} />);
    expect(screen.getByText("Market closed today; next session 2026-10-12")).toBeInTheDocument();
    t.unmount();
    const top = renderWithProviders(<TopBar live={saturday} connected updatedAt={NOW} nowMs={NOW} />);
    expect(screen.getByRole("heading", { level: 2, name: /last session 2026-10-09/ })).toBeInTheDocument();
    top.unmount();
    // Thanksgiving 2026-11-26: next session Friday 11-27, the dashboard's day is Wednesday 11-25
    const thanksgiving = lfx.liveWith({ ...lfx.liveEmptyDay, session_day: "2026-11-25", session: { date: "2026-11-27", phase: "closed_day", is_session: false, open_at: null, close_at: null } });
    renderWithProviders(<TodayTimeline timeline={[]} session={thanksgiving.session} />);
    expect(screen.getByText("Market closed today; next session 2026-11-27")).toBeInTheDocument();
  });
});
