// DB-T9 acceptance tests 6 and 9 (health part): the stale-worker badge, the opening-bar fetch (complete,
// incomplete, not fetched), the Questrade counts, the marks flag, the worker-down and Telegram-off texts and
// failed sends.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { HealthPanelOut } from "../../api/types";
import * as fx from "../../test/fixtures";
import * as lfx from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { TELEGRAM_OFF_TEXT, WORKER_DOWN_TEXT } from "../system/StatusCards";
import { HealthCard, NOT_FETCHED_TEXT, openingBarsText } from "./HealthCard";

function region() {
  return screen.getByRole("region", { name: "Health" });
}

/** Renders with the providers, replacing any earlier render (RTL's `rerender` would drop the providers). */
let mounted: { unmount: () => void } | null = null;
afterEach(() => {
  mounted = null;
});
function show(h: HealthPanelOut | null) {
  mounted?.unmount();
  mounted = renderWithProviders(<HealthCard health={h} />);
  return mounted;
}

function health(overrides: Partial<HealthPanelOut> = {}): HealthPanelOut {
  return { ...structuredClone(lfx.healthPanel), ...overrides };
}

describe("HealthCard", () => {
  it("a fresh worker: no stale badge, heartbeat age, token, database and Telegram OK", async () => {
    renderWithProviders(<HealthCard health={health()} />);
    const r = region();
    expect(within(r).getByText("Worker OK")).toBeInTheDocument();
    expect(within(r).getByText("4s ago")).toBeInTheDocument();
    expect(within(r).queryByText("heartbeat stale")).toBeNull();
    expect(within(r).queryByText(WORKER_DOWN_TEXT)).toBeNull();
    expect(within(r).getByText("Token OK")).toBeInTheDocument();
    expect(within(r).getByText("OK, 3 ms")).toBeInTheDocument();
    expect(within(r).getByText("Telegram configured")).toBeInTheDocument();
    expect(within(r).getByRole("button", { name: "Send a test message" })).toBeInTheDocument();
    expect(within(r).queryByText(TELEGRAM_OFF_TEXT)).toBeNull();
    expect(within(r).getByText("tz database 2026c")).toBeInTheDocument();
    expect(await within(r).findByText(/^Time zone OK/)).toBeInTheDocument();
  });

  it("a stale worker shows the badge and the worker-down text (controlStaleWorker)", () => {
    renderWithProviders(<HealthCard health={lfx.controlStaleWorker.health} />);
    const r = region();
    expect(within(r).getByText("heartbeat stale")).toBeInTheDocument();
    expect(within(r).getByText("1m 35s ago")).toBeInTheDocument();
    expect(within(r).getByText("Worker not running")).toBeInTheDocument();
    expect(within(r).getByText(WORKER_DOWN_TEXT)).toBeInTheDocument();
  });

  it("a heartbeat over 60 s shows the badge even while the worker is still ok", () => {
    renderWithProviders(<HealthCard health={health({ worker: { ...lfx.healthPanel.worker, age_seconds: 61 } })} />);
    expect(within(region()).getByText("heartbeat stale")).toBeInTheDocument();
  });

  it("the opening-bar fetch: complete, incomplete, and not fetched by the worker today", () => {
    show(health());
    expect(within(region()).getByText("543 of 543 bars in 31.2 s")).toBeInTheDocument();
    expect(within(region()).getByText("complete")).toBeInTheDocument();

    const incomplete = health({
      opening_bars: { ...lfx.healthPanel.opening_bars!, completed: 530, errors: 2, outstanding: 11, elapsed_s: 45, http_429: 3, pause_s: 4.5, complete: false, raised: "CancelledError" },
    });
    show(incomplete);
    expect(within(region()).getByText("530 of 543 bars in 45 s")).toBeInTheDocument();
    expect(within(region()).getByText("incomplete")).toBeInTheDocument();
    expect(within(region()).getByText("2 errors, 11 outstanding, 3 x 429 (4.5 s paused), stopped by CancelledError")).toBeInTheDocument();

    show(health({ opening_bars: null }));
    expect(within(region()).getByText(NOT_FETCHED_TEXT)).toBeInTheDocument();
    expect(NOT_FETCHED_TEXT).toBe("not fetched by the worker today");
  });

  it("openingBarsText formats whole and fractional seconds", () => {
    expect(openingBarsText({ ...lfx.healthPanel.opening_bars!, elapsed_s: 31.24 })).toBe("543 of 543 bars in 31.2 s");
    expect(openingBarsText({ ...lfx.healthPanel.opening_bars!, elapsed_s: 30 })).toBe("543 of 543 bars in 30 s");
  });

  it("Questrade today: requests, 429s and pause seconds per category, and the rate limits", () => {
    renderWithProviders(<HealthCard health={health()} />);
    const table = within(region()).getByRole("table", { name: "Questrade today" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent(/market\s*1204\s*1\s*1.2 s/);
    expect(rows[2]).toHaveTextContent(/account\s*96\s*0\s*0 s/);
    expect(within(region()).getByText("market_remaining 18 · account_remaining 29")).toBeInTheDocument();
  });

  it("Questrade counts not reported", () => {
    renderWithProviders(<HealthCard health={health({ questrade: null })} />);
    expect(within(region()).getByText("Not reported by the worker today.")).toBeInTheDocument();
  });

  it("marks: last write and the failing flag", () => {
    show(health());
    expect(within(region()).getByText("last write 07:59 MT, 1 symbol")).toBeInTheDocument();
    expect(within(region()).getByText("marks OK")).toBeInTheDocument();
    show(health({ marks: { written_at: null, symbols: 0, failing: true } }));
    expect(within(region()).getByText("marks failing")).toBeInTheDocument();
    expect(within(region()).getByText("no write yet, 0 symbols")).toBeInTheDocument();
    show(health({ marks: null }));
    expect(within(region()).getByText("Marks: not reported by the worker.")).toBeInTheDocument();
  });

  it("Telegram not configured shows the off text; failed sends are listed", () => {
    renderWithProviders(<HealthCard health={health({ telegram_configured: false })} />);
    expect(within(region()).getByText(TELEGRAM_OFF_TEXT)).toBeInTheDocument();
    const sends = within(region()).getByRole("table", { name: "Failed Telegram sends" });
    expect(within(sends).getByText(fx.notificationsFailed[0]!.error!)).toBeInTheDocument();
  });

  it("a bad token links to the paste on Settings; a database failure is shown", () => {
    renderWithProviders(<HealthCard health={health({ token: { ...fx.tokenOut, ok: false, error: "expired" }, db_ok: false, db_latency_ms: null })} />);
    expect(within(region()).getByText("Token error")).toBeInTheDocument();
    expect(within(region()).getByText("expired")).toBeInTheDocument();
    expect(within(region()).getByRole("link", { name: "Paste a new token" })).toHaveAttribute("href", "/settings#questrade");
    expect(within(region()).getByText("not reachable")).toBeInTheDocument();
  });

  it("the health part failed: its error and Retry", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<HealthCard health={null} error="OperationalError: health could not be read" onRetry={onRetry} />);
    expect(within(region()).getByRole("alert")).toHaveTextContent("OperationalError: health could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(onRetry).toHaveBeenCalledTimes(1));
  });

  it("renders free text as plain text", () => {
    const { control } = lfx.withXssText();
    const { container } = renderWithProviders(<HealthCard health={control.health} />);
    expect(container.querySelector("img")).toBeNull();
  });
});
