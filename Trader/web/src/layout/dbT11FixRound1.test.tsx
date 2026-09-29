// DB-T11 fix round 1: the web wiring gauntlet's findings (B8 kill-switch refresh keys, B12/B13 design D9
// colours) and nits (pre-paint theme, DayDecisions back/forward, --chart-exit), pinned beside the breaker.
import { useQueryClient } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { useNavigate, type NavigateFunction } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { prefixesFor, qk, TOPIC_KEYS } from "../api/queryKeys";
import ReportsPage from "../pages/Reports";
import { KillSwitchPanel } from "../pages/settings/KillSwitchPanel";
import { renderWithProviders } from "../test/render";
import { THEME_STORAGE_KEY } from "../theme/tokens";

function webPath(relative: string): string {
  const candidates = [join(process.cwd(), relative), join(process.cwd(), "Trader", "web", relative)];
  const found = candidates.find((p) => existsSync(p));
  if (!found) throw new Error(`cannot find ${relative} from ${process.cwd()}`);
  return found;
}

const read = (relative: string) => readFileSync(webPath(relative), "utf8");

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
});

describe("B8: a kill-switch change refreshes the Control data", () => {
  it("the SSE killswitch topic invalidates the system prefix (Control's control query) as well as the dashboard", () => {
    expect(TOPIC_KEYS.killswitch).toEqual(expect.arrayContaining(["dashboard", "killswitches", "system"]));
    expect(qk.control()[0]).toBe("system");
    expect(prefixesFor(["killswitch"])).toContain(qk.control()[0]);
  });

  it("Pause and Resume from the panel invalidate both the dashboard and the system prefixes", async () => {
    const seen: unknown[] = [];
    function Spy() {
      const qc = useQueryClient();
      const orig = qc.invalidateQueries.bind(qc);
      qc.invalidateQueries = ((filters?: { queryKey?: unknown }) => {
        seen.push(JSON.stringify(filters?.queryKey));
        return orig(filters as never);
      }) as typeof qc.invalidateQueries;
      return null;
    }
    const r = renderWithProviders(
      <>
        <Spy />
        <KillSwitchPanel />
      </>,
    );
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    await userEvent.click(screen.getByRole("button", { name: "Pause new entries" }));
    await waitFor(() => expect(r.api.callsTo("pause")).toHaveLength(1));
    await waitFor(() => expect(seen).toEqual(expect.arrayContaining([JSON.stringify(qk.dashboard()), JSON.stringify(qk.system())])));
    seen.length = 0;
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(seen).toEqual(expect.arrayContaining([JSON.stringify(qk.dashboard()), JSON.stringify(qk.system())])));
  });
});

describe("B12-B13: design D9 colour rules", () => {
  it("danger buttons and failed timeline labels are remapped to the status colours on the Dashboard and Control", () => {
    const styles = read("src/styles.css");
    expect(styles).toMatch(/\.live-page \.btn-danger,\s*\.control-page \.btn-danger\s*\{[^}]*var\(--status-bad\)/);
    const dash = read("src/pages/dashboard/dashboard.css");
    expect(dash).toMatch(
      /\.live-page \.timeline-item\.tone-bad \.timeline-label,\s*\.control-page \.timeline-item\.tone-bad \.timeline-label\s*\{[^}]*var\(--status-bad\)/,
    );
  });

  it("the status-* classes are defined globally (the header sits outside .live-page) and never use green/red", () => {
    const styles = read("src/styles.css").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const t of ["ok", "warn", "bad", "muted"]) {
      const rule = new RegExp(`(^|\\n)\\.status-${t}\\s*\\{([^}]*)\\}`).exec(styles);
      expect(rule, t).not.toBeNull();
      expect(rule![2]).toContain(`var(--status-${t})`);
      expect(rule![2]).not.toMatch(/var\(--(ok|bad|money-up|money-down)\)/);
    }
  });

  it("--chart-exit is not the money green in any theme block", () => {
    const tokens = read("src/theme/tokens.css");
    const blocks = tokens.match(/:root[^{]*\{[^}]*\}/g) ?? [];
    expect(blocks).toHaveLength(4);
    for (const b of blocks) {
      const exit = /--chart-exit:\s*([^;]+);/.exec(b)![1]!.trim();
      const up = /--money-up:\s*([^;]+);/.exec(b)![1]!.trim();
      const ok = /--ok:\s*([^;]+);/.exec(b)![1]!.trim();
      expect(exit).not.toBe(up);
      expect(exit).not.toBe(ok);
    }
  });
});

describe("pre-paint theme script (no dark flash for Light users)", () => {
  const src = read("public/theme-init.js");
  const run = () => new Function(src)();

  it("index.html loads it as a classic script in <head>, before the app module (CSP script-src 'self': a file, not inline)", () => {
    const html = read("index.html");
    const head = html.slice(0, html.indexOf("</head>"));
    expect(head).toContain('<script src="/theme-init.js"></script>');
    expect(html.indexOf("/theme-init.js")).toBeLessThan(html.indexOf("/src/main.tsx"));
    expect(html).not.toMatch(/<script>(?!<)/);
    expect(src).toContain(`"${THEME_STORAGE_KEY}"`);
  });

  it("applies the remembered choice", () => {
    for (const choice of ["light", "auto", "dark"]) {
      vi.stubGlobal("localStorage", { getItem: (k: string) => (k === THEME_STORAGE_KEY ? choice : null) });
      run();
      expect(document.documentElement.getAttribute("data-theme")).toBe(choice);
    }
  });

  it("defaults to dark for nothing stored, junk, or blocked storage", () => {
    vi.stubGlobal("localStorage", { getItem: () => null });
    run();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    document.documentElement.setAttribute("data-theme", "light");
    vi.stubGlobal("localStorage", { getItem: () => "<script>" });
    run();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    document.documentElement.setAttribute("data-theme", "light");
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("SecurityError");
      },
    });
    expect(run).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

describe("DayDecisions: the filters follow the URL on back/forward", () => {
  it("a pushed link sets the filters; Back clears them again; Forward restores them", async () => {
    let navigate: NavigateFunction | null = null;
    function Grab() {
      navigate = useNavigate();
      return null;
    }
    const r = renderWithProviders(
      <>
        <ReportsPage />
        <Grab />
      </>,
      { route: "/reports?day=2026-10-06" },
    );
    const stage = await screen.findByRole("combobox", { name: "Stage" });
    expect(stage).toHaveValue("");
    act(() => navigate!("/reports?day=2026-10-06&stage=scan&outcome=rejected&ticker=AAPL"));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Stage" })).toHaveValue("scan"));
    expect(screen.getByRole("combobox", { name: "Outcome" })).toHaveValue("rejected");
    expect(screen.getByRole("searchbox", { name: "Ticker" })).toHaveValue("AAPL");
    expect(r.api.callsTo("decisionDay").at(-1)).toEqual([expect.objectContaining({ stage: "scan", outcome: "rejected", ticker: "AAPL", offset: 0 })]);

    act(() => navigate!(-1));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Stage" })).toHaveValue(""));
    expect(screen.getByRole("combobox", { name: "Outcome" })).toHaveValue("");
    expect(screen.getByRole("searchbox", { name: "Ticker" })).toHaveValue("");

    act(() => navigate!(1));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Stage" })).toHaveValue("scan"));
  });

  it("the view's own writes are not read back: a ticker being typed (not yet a valid URL ticker) is kept", async () => {
    const r = renderWithProviders(<ReportsPage />, { route: "/reports?day=2026-10-06" });
    const box = await screen.findByRole("searchbox", { name: "Ticker" });
    await userEvent.type(box, "brk.b1");
    expect(within(screen.getByRole("group", { name: "Filters" })).getByRole("searchbox", { name: "Ticker" })).toHaveValue("BRK.B1");
    expect(new URLSearchParams(r.location().search).get("ticker")).toBe("BRK.B1");
  });
});
