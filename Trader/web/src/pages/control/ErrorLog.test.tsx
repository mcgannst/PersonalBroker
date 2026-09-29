// DB-T9 acceptance test 8: the error log filters by level and source, renders text literally, has an empty
// state, and opens the older-events log.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { EventOut } from "../../api/types";
import * as lfx from "../../test/liveFixtures";
import { renderWithProviders } from "../../test/render";
import { EMPTY_TEXT, ErrorLog } from "./ErrorLog";

const rows: EventOut[] = [
  { id: 305, ts: "2026-10-06T14:05:00Z", level: "critical", source: "worker", message: "worker step crashed", data: null },
  { id: 304, ts: "2026-10-06T14:00:00Z", level: "error", source: "log.api", message: "request failed", data: null },
  { id: 303, ts: "2026-10-06T13:50:00Z", level: "warning", source: "market.data_service", message: "HTTP 429 from Questrade, paused 1.2 s", data: null },
  { id: 302, ts: "2026-10-06T13:40:00Z", level: "error", source: "market.data_service", message: "quote fetch failed", data: null },
];

function region() {
  return screen.getByRole("region", { name: "Error log" });
}

function shown(): string[] {
  const list = within(region()).queryByRole("list", { name: "Warnings and errors" });
  return list ? within(list).getAllByRole("listitem").map((li) => li.getAttribute("data-id") ?? "") : [];
}

describe("ErrorLog", () => {
  it("lists every row newest first with its time, level, source and message", () => {
    renderWithProviders(<ErrorLog errors={rows} />);
    expect(shown()).toEqual(["305", "304", "303", "302"]);
    const first = within(region()).getAllByRole("listitem")[0]!;
    expect(within(first).getByText("2026-10-06 08:05 MT")).toBeInTheDocument();
    expect(within(first).getByText("critical")).toBeInTheDocument();
    expect(within(first).getByText("worker")).toBeInTheDocument();
    expect(within(first).getByText("worker step crashed")).toBeInTheDocument();
    expect(within(region()).getByText("4 of 4")).toBeInTheDocument();
  });

  it("filters by level (that level and above) and by source, together", async () => {
    renderWithProviders(<ErrorLog errors={rows} />);
    const level = within(region()).getByLabelText("Level");
    const source = within(region()).getByLabelText("Source");
    expect(Array.from((source as HTMLSelectElement).options).map((o) => o.value)).toEqual(["", "log.api", "market.data_service", "worker"]);
    await userEvent.selectOptions(level, "error");
    expect(shown()).toEqual(["305", "304", "302"]);
    await userEvent.selectOptions(level, "critical");
    expect(shown()).toEqual(["305"]);
    await userEvent.selectOptions(level, "");
    await userEvent.selectOptions(source, "market.data_service");
    expect(shown()).toEqual(["303", "302"]);
    await userEvent.selectOptions(level, "error");
    expect(shown()).toEqual(["302"]);
    expect(within(region()).getByText("1 of 4")).toBeInTheDocument();
    await userEvent.selectOptions(source, "log.api");
    await userEvent.selectOptions(level, "critical");
    expect(shown()).toEqual([]);
    expect(within(region()).getByText("No events match these filters.")).toBeInTheDocument();
  });

  it("renders markup in messages and sources as literal text", () => {
    const { control } = lfx.withXssText();
    const { container } = renderWithProviders(<ErrorLog errors={control.errors} />);
    expect(within(region()).getByText(`HTTP 429 from Questrade, paused 1.2 s${lfx.XSS}`)).toBeInTheDocument();
    expect(within(region()).getAllByText(`market.data_service${lfx.XSS}`).length).toBeGreaterThan(0);
    expect(container.querySelector("img")).toBeNull();
  });

  it("empty state: No warnings or errors", () => {
    renderWithProviders(<ErrorLog errors={[]} />);
    expect(within(region()).getByText(EMPTY_TEXT)).toBeInTheDocument();
    expect(EMPTY_TEXT).toBe("No warnings or errors");
  });

  it("Older events opens the event log (GET /api/events)", async () => {
    const { api } = renderWithProviders(<ErrorLog errors={rows} />);
    const toggle = within(region()).getByRole("button", { name: "Older events" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(api.callsTo("events")).toHaveLength(0);
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(region()).getByRole("heading", { name: "Event log" })).toBeInTheDocument();
    await waitFor(() => expect(api.callsTo("events")).toHaveLength(1));
  });

  it("the errors part failed: its error and Retry", async () => {
    const onRetry = vi.fn();
    renderWithProviders(<ErrorLog errors={null} error="OperationalError: errors could not be read" onRetry={onRetry} />);
    expect(within(region()).getByRole("alert")).toHaveTextContent("OperationalError: errors could not be read");
    await userEvent.click(within(region()).getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
