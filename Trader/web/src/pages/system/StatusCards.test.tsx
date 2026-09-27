import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { SystemOut } from "../../api/types";
import { setDisplayZone } from "../../lib/format";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { FailedSends, RateLimits, StatusCards } from "./StatusCards";

function system(overrides: Partial<SystemOut> = {}): SystemOut {
  return { ...structuredClone(fx.systemOut), ...overrides };
}

describe("StatusCards (acceptance test 1)", () => {
  it("renders token OK, worker OK and Telegram configured in green", () => {
    renderWithProviders(<StatusCards system={system()} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Token OK" })).toHaveClass("tone-ok");
    expect(screen.getByRole("status", { name: "Worker OK" })).toHaveClass("tone-ok");
    expect(screen.getByRole("status", { name: "Telegram configured" })).toHaveClass("tone-ok");
    expect(screen.queryByText(/worker not running/i)).toBeNull();
    expect(screen.queryByText(/Telegram not configured/)).toBeNull();
  });

  it("shows the token's last refresh and expiry in MT, the worker's phase, pid and host", () => {
    renderWithProviders(<StatusCards system={system()} serverOffsetMinutes={-360} />);
    // last_refresh_at 11:30Z and expires_at 14:30Z at UTC-6.
    expect(screen.getByText("2026-10-06 05:30 MT")).toBeInTheDocument();
    expect(screen.getByText("2026-10-06 08:30 MT")).toBeInTheDocument();
    expect(screen.getByText("2h 30m")).toBeInTheDocument();
    expect(screen.getByText("session")).toBeInTheDocument();
    expect(screen.getByText("42 on trader-dev")).toBeInTheDocument();
    expect(screen.getByText("0005")).toBeInTheDocument();
    expect(screen.getByText("2026c")).toBeInTheDocument();
  });

  it("renders the red Telegram notice when Telegram is not configured", () => {
    renderWithProviders(<StatusCards system={system({ telegram_configured: false })} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Telegram not configured" })).toHaveClass("tone-bad");
    expect(
      screen.getByText("Telegram not configured: nothing is relayed to your phone; approve on the Dashboard"),
    ).toBeInTheDocument();
  });

  it("renders the worker-not-running text in red when the worker is not OK", () => {
    const s = system();
    s.worker = { ...s.worker, ok: false, phase: "stopped", age_seconds: 900 };
    renderWithProviders(<StatusCards system={s} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Worker not running" })).toHaveClass("tone-bad");
    expect(screen.getByText("worker not running: approvals and fills will not happen")).toBeInTheDocument();
  });

  it("shows the token error and a link to paste a new token when the token is not OK", () => {
    const s = system();
    s.token = { ...s.token, ok: false, error: "The refresh token was rejected" };
    renderWithProviders(<StatusCards system={s} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Token error" })).toHaveClass("tone-bad");
    expect(screen.getByText("The refresh token was rejected")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /paste a new token/i })).toHaveAttribute("href", "/settings#questrade");
  });

  it("says the token is not seeded when there is none", () => {
    const s = system();
    s.token = { ok: false, seeded: false, age_hours: null, expires_at: null, last_refresh_at: null, error: null };
    renderWithProviders(<StatusCards system={s} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Token not seeded" })).toHaveClass("tone-bad");
  });
});

describe("time-zone card (acceptance test 2)", () => {
  it("fixed time-zone mode renders the amber warning with the server's offset", () => {
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" }, { browserOffsetMinutes: -420, serverOffsetMinutes: -360 });
    renderWithProviders(<StatusCards system={system()} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Time zone: server offset" })).toHaveClass("tone-warn");
    expect(
      screen.getByText("Your browser's time-zone data differs from the server's; times use the server's offset (UTC−6)"),
    ).toBeInTheDocument();
  });

  it("fixed mode without a recorded check still names the server's offset from meta", () => {
    setDisplayZone({ fixedOffsetMinutes: -360, label: "MT" });
    renderWithProviders(<StatusCards system={system()} serverOffsetMinutes={-360} />);
    expect(screen.getByText(/times use the server's offset \(UTC−6\)/)).toBeInTheDocument();
  });

  it("zone mode renders green", () => {
    setDisplayZone({ zone: "America/Edmonton", label: "MT" }, { browserOffsetMinutes: -360, serverOffsetMinutes: -360 });
    renderWithProviders(<StatusCards system={system()} serverOffsetMinutes={-360} />);
    expect(screen.getByRole("status", { name: "Time zone OK" })).toHaveClass("tone-ok");
    expect(screen.queryByText(/differs from the server's/)).toBeNull();
  });
});

describe("RateLimits (acceptance test 7)", () => {
  it("shows the remaining requests per category", () => {
    renderWithProviders(<RateLimits rateLimit={{ market: 18, account: 29 }} />);
    expect(screen.getByText("market")).toBeInTheDocument();
    expect(screen.getByText("18")).toBeInTheDocument();
    expect(screen.getByText("account")).toBeInTheDocument();
    expect(screen.getByText("29")).toBeInTheDocument();
  });

  it("null shows 'not reported yet'", () => {
    renderWithProviders(<RateLimits rateLimit={null} />);
    expect(screen.getByText(/not reported yet/)).toBeInTheDocument();
  });
});

describe("FailedSends", () => {
  it("lists kind, status, MT time, attempts and error", () => {
    renderWithProviders(<FailedSends items={fx.notificationsFailed} />);
    expect(screen.getByText("proposal")).toBeInTheDocument();
    expect(screen.getByText("failed")).toBeInTheDocument();
    expect(screen.getByText("2026-10-05 07:36 MT")).toBeInTheDocument();
    expect(screen.getByText("TimedOut")).toBeInTheDocument();
  });

  it("says so when there are none", () => {
    renderWithProviders(<FailedSends items={[]} />);
    expect(screen.getByText("No failed sends.")).toBeInTheDocument();
  });
});
