// The old System page's StatusCards/RateLimits tests went with those components (DB-T11 fix round 1: nothing
// but these tests used them; Control's Health card and its tests carry the token, worker, Telegram, time-zone
// and rate-limit states). What Control still reuses from this file is tested here.
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { FailedSends, utcOffsetLabel } from "./StatusCards";

describe("utcOffsetLabel", () => {
  it("labels whole, half-hour and zero offsets", () => {
    expect(utcOffsetLabel(-360)).toBe("UTC−6");
    expect(utcOffsetLabel(330)).toBe("UTC+5:30");
    expect(utcOffsetLabel(0)).toBe("UTC");
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
