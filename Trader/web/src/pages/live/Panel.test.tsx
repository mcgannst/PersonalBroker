// DB-T1 acceptance test 9: the shared panel frame.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MIN_TOUCH_PX } from "../../components/ui";
import { Panel } from "./Panel";

describe("Panel (acceptance test 9)", () => {
  it("renders its title as a heading inside a labelled region, with its children", () => {
    render(
      <Panel title="Positions" badge={<span>prices stale</span>}>
        <p>row one</p>
      </Panel>,
    );
    const region = screen.getByRole("region", { name: "Positions" });
    expect(within(region).getByRole("heading", { name: "Positions" })).toBeInTheDocument();
    expect(within(region).getByText("row one")).toBeInTheDocument();
    expect(within(region).getByText("prices stale")).toBeInTheDocument();
  });

  it("uses ariaLabel as the region's name when given", () => {
    render(<Panel title="Risk" ariaLabel="Risk and kill switches" />);
    expect(screen.getByRole("region", { name: "Risk and kill switches" })).toBeInTheDocument();
  });

  it("shows the error and a 44 px Retry that calls onRetry instead of the children", async () => {
    const onRetry = vi.fn();
    render(
      <Panel title="Activity" error="OperationalError: activity could not be read" onRetry={onRetry}>
        <p>hidden child</p>
      </Panel>,
    );
    const region = screen.getByRole("region", { name: "Activity" });
    expect(within(region).getByRole("alert")).toHaveTextContent("OperationalError: activity could not be read");
    expect(within(region).queryByText("hidden child")).not.toBeInTheDocument();
    const retry = within(region).getByRole("button", { name: "Retry" });
    expect(parseInt(retry.style.minHeight, 10)).toBeGreaterThanOrEqual(MIN_TOUCH_PX);
    await userEvent.click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows no Retry without onRetry", () => {
    render(<Panel title="Books" error="failed" />);
    expect(screen.getByRole("alert")).toHaveTextContent("failed");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders error text containing markup as plain text", () => {
    const { container } = render(<Panel title="Errors" error={'<script>alert(1)</script><img src=x onerror=alert(1)>'} />);
    expect(screen.getByRole("alert")).toHaveTextContent("<script>alert(1)</script><img src=x onerror=alert(1)>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("shows the empty text only when there are no children", () => {
    const { rerender } = render(<Panel title="Rejected" empty="No rejections recorded today" />);
    expect(screen.getByText("No rejections recorded today")).toBeInTheDocument();
    rerender(
      <Panel title="Rejected" empty="No rejections recorded today">
        <p>rvol_below_min</p>
      </Panel>,
    );
    expect(screen.queryByText("No rejections recorded today")).not.toBeInTheDocument();
    expect(screen.getByText("rvol_below_min")).toBeInTheDocument();
    rerender(<Panel title="Rejected" empty={"<b>none</b>"} />);
    expect(screen.getByText("<b>none</b>")).toBeInTheDocument();
  });
});
