import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import type { DecisionOut } from "../../api/types";
import { FakeApiClient } from "../../test/fakeApi";
import * as fx from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import PendingProposal from "./PendingProposal";

/** The skew that makes the browser's "now" equal the fixtures' server time. */
function fixtureSkew(): number {
  return Date.parse(fx.SERVER_TIME) - Date.now();
}

function buttons() {
  return {
    approve: screen.getByRole("button", { name: "Approve" }),
    reject: screen.getByRole("button", { name: "Reject" }),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("PendingProposal (acceptance test 2)", () => {
  it("shows the entry's details and a countdown", () => {
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />);
    const card = screen.getByRole("article", { name: /proposal 12/i });
    const c = within(card);
    expect(c.getByText("ENTRY")).toBeInTheDocument();
    expect(c.getByText("AAA")).toBeInTheDocument();
    expect(c.getByText(/BUY 30/)).toBeInTheDocument();
    expect(c.getByText("Buy stop 21.56")).toBeInTheDocument();
    expect(c.getByText("Stop loss 20.98")).toBeInTheDocument();
    expect(c.getByText("Risk $17.40")).toBeInTheDocument();
    expect(c.getByText(/ORB breakout above 21.55/)).toBeInTheDocument();
    expect(c.getByText(/orb_sip/)).toBeInTheDocument();
    // 14:04:10Z expiry, server "now" 14:00:00Z: about 4m 10s left (a second may tick by).
    expect(c.getByText(/Expires in 4m (10|9)s/)).toBeInTheDocument();
  });

  it("approves with one tap: one call, both buttons disabled while busy, then the message", async () => {
    let release: (d: DecisionOut) => void = () => undefined;
    const api = new FakeApiClient();
    api.respond("approve", () => new Promise<DecisionOut>((resolve) => (release = resolve)));
    const onDecided = vi.fn();
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} onDecided={onDecided} />, { api });

    await userEvent.click(buttons().approve);
    expect(buttons().approve).toBeDisabled();
    expect(buttons().reject).toBeDisabled();
    await userEvent.click(buttons().approve);
    expect(api.callsTo("approve")).toEqual([[12]]);
    expect(api.callsTo("reject")).toEqual([]);

    await act(async () => release(fx.decisionOut));
    expect(await screen.findByText("Approved")).toBeInTheDocument();
    expect(onDecided).toHaveBeenCalledTimes(1);
    // Decided: the buttons stay disabled.
    expect(buttons().approve).toBeDisabled();
    expect(buttons().reject).toBeDisabled();
  });

  it("rejects with one tap (no confirmation step)", async () => {
    const api = new FakeApiClient();
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api });
    await userEvent.click(buttons().reject);
    expect(await screen.findByText("Rejected")).toBeInTheDocument();
    expect(api.callsTo("reject")).toEqual([[12]]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("PendingProposal outcomes (acceptance test 3)", () => {
  it("shows a blocked entry's message", async () => {
    const api = new FakeApiClient({ approve: fx.decisionBlocked });
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api });
    await userEvent.click(buttons().approve);
    const msg = await screen.findByText("Entry blocked: kill switch manual_pause is tripped");
    expect(msg.closest(".tone-bad")).not.toBeNull();
  });

  it("shows an ApiError's message and re-enables the buttons", async () => {
    const api = new FakeApiClient();
    api.fail("approve", new ApiError(409, "conflict", "Already expired"));
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={fixtureSkew()} />, { api });
    await userEvent.click(buttons().approve);
    expect(await screen.findByText("Already expired")).toBeInTheDocument();
    expect(buttons().approve).toBeEnabled();
    expect(buttons().reject).toBeEnabled();
  });
});

describe("PendingProposal countdown (acceptance test 4)", () => {
  it("turns red under 60 s and disables both buttons at zero", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-06T14:04:07Z"));
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={0} />);
    const countdown = screen.getByText("Expires in 3s");
    expect(countdown).toHaveClass("tone-bad");
    expect(buttons().approve).toBeEnabled();

    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.getByText("Expired, waiting for the server")).toBeInTheDocument();
    expect(buttons().approve).toBeDisabled();
    expect(buttons().reject).toBeDisabled();
  });

  it("uses the server skew, not the browser clock", () => {
    vi.useFakeTimers();
    // The browser is 10 minutes slow; the server says 14:04:07Z.
    vi.setSystemTime(new Date("2026-10-06T13:54:07Z"));
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={600_000} />);
    expect(screen.getByText("Expires in 3s")).toBeInTheDocument();
  });

  it("is not red with more than a minute left", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-06T14:00:00Z"));
    renderWithProviders(<PendingProposal proposal={fx.pendingProposal} serverSkewMs={0} />);
    expect(screen.getByText("Expires in 4m 10s")).not.toHaveClass("tone-bad");
  });
});

describe("PendingProposal headlines", () => {
  it("names each kind", () => {
    const kinds: [string, string][] = [
      ["stop", "PROTECTIVE STOP"],
      ["exit", "EXIT"],
      ["cancel", "CANCEL"],
    ];
    for (const [kind, headline] of kinds) {
      const { unmount } = renderWithProviders(
        <PendingProposal proposal={{ ...fx.pendingProposal, kind, side: "sell", order_type: "market", stop: null }} serverSkewMs={0} />,
      );
      expect(screen.getByText(headline)).toBeInTheDocument();
      unmount();
    }
  });
});
