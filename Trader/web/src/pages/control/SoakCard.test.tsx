// DB-T9 acceptance test 7 (card level): the soak summary, no day recorded yet, and the part error.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import * as lfx from "../../test/liveFixtures";
import { SoakCard } from "./SoakCard";

function region() {
  return screen.getByRole("region", { name: "Soak" });
}

describe("SoakCard", () => {
  it("shows clean days n / target, day 1, earliest finish, today's verdict so far and the last final day", () => {
    render(<SoakCard soak={lfx.soakSummary} />);
    const r = region();
    expect(within(r).getByText("3 / 10 clean")).toBeInTheDocument();
    expect(within(r).getByText("2026-09-29")).toBeInTheDocument();
    expect(within(r).getByText("2026-10-12")).toBeInTheDocument();
    expect(within(r).getByText("2026-10-05")).toBeInTheDocument();
    expect(within(r).getByText("clean so far")).toBeInTheDocument();
    expect(within(r).getByText("(provisional, 2026-10-06)")).toBeInTheDocument();
  });

  it("lists today's failed checks", () => {
    const soak = { ...lfx.soakSummary, today: { session_date: "2026-10-06", verdict: "not clean", failed: ["premarket", "postclose"], provisional: false } };
    render(<SoakCard soak={soak} />);
    expect(within(region()).getByText("not clean")).toBeInTheDocument();
    expect(within(region()).getByText("failed: premarket, postclose")).toBeInTheDocument();
    expect(within(region()).getByText("(final, 2026-10-06)")).toBeInTheDocument();
  });

  it("no soak day yet: zero clean, no day 1, nothing today", () => {
    render(<SoakCard soak={lfx.controlNoSoak.soak} />);
    expect(within(region()).getByText("0 / 10 clean")).toBeInTheDocument();
    expect(within(region()).getByText("no clean day yet")).toBeInTheDocument();
    expect(within(region()).getByText("no verdict for today")).toBeInTheDocument();
  });

  it("the soak part failed: its error and Retry", async () => {
    const onRetry = vi.fn();
    render(<SoakCard soak={null} error="OperationalError: soak could not be read" onRetry={onRetry} />);
    expect(within(region()).getByRole("alert")).toHaveTextContent("OperationalError: soak could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("renders free text as plain text", () => {
    const { control } = lfx.withXssText();
    const { container } = render(<SoakCard soak={control.soak} />);
    expect(within(region()).getByText(`clean so far${lfx.XSS}`)).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });
});
