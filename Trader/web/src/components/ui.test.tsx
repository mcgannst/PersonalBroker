import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { Badge, Button, Card, Empty, ErrorBox, Light, Loading, Stat, Table } from "./ui";

describe("Button (acceptance test 4)", () => {
  it("is disabled while busy and at least 44 px tall", () => {
    render(
      <Button variant="primary" busy>
        Approve
      </Button>,
    );
    const button = screen.getByRole("button", { name: /approve/i });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveStyle({ minHeight: "44px" });
    expect(button).toHaveClass("btn", "btn-primary");
  });

  it("passes button props through and calls onClick when idle", async () => {
    const onClick = vi.fn();
    render(
      <Button variant="danger" onClick={onClick} title="reject it">
        Reject
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Reject" });
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute("type", "button");
    expect(button).toHaveClass("btn-danger");
    expect(button).toHaveStyle({ minHeight: "44px" });
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("stays disabled when disabled even if not busy", () => {
    render(<Button disabled>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});

describe("ErrorBox (acceptance test 4)", () => {
  it("shows an ApiError's message and a Retry button that calls onRetry", async () => {
    const onRetry = vi.fn();
    const error = new ApiError(409, "conflict", "Already paused.");
    render(<ErrorBox error={error} onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Already paused.");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("never shows a stack or a raw error message, and has no Retry without onRetry", () => {
    const error = new TypeError("Cannot read properties of undefined (reading 'x')");
    render(<ErrorBox error={error} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Something went wrong.");
    expect(alert.textContent).not.toContain("Cannot read");
    expect(alert.textContent).not.toContain("at ");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});

describe("other primitives", () => {
  it("Card renders its title, actions and children", () => {
    render(
      <Card title="Pending" actions={<button type="button">Refresh</button>}>
        <p>body</p>
      </Card>,
    );
    expect(screen.getByRole("heading", { name: "Pending" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.getByText("body")).toBeInTheDocument();
  });

  it("Badge and Light carry their tone", () => {
    render(
      <>
        <Badge tone="warn">AUTO</Badge>
        <Light tone="bad" label="Max drawdown" />
      </>,
    );
    expect(screen.getByText("AUTO")).toHaveClass("badge", "tone-warn");
    const light = screen.getByRole("status", { name: /max drawdown/i });
    expect(light).toHaveClass("light", "tone-bad");
    expect(light).toHaveTextContent("Max drawdown");
  });

  it("Stat, Table, Loading and Empty render", () => {
    render(
      <>
        <Stat label="Equity" value="$720.00" sub="peak $730.00" />
        <Table>
          <thead>
            <tr>
              <th>Ticker</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>AAA</td>
            </tr>
          </tbody>
        </Table>
        <Loading />
        <Empty>No trades yet</Empty>
      </>,
    );
    expect(screen.getByText("Equity")).toBeInTheDocument();
    expect(screen.getByText("$720.00")).toBeInTheDocument();
    expect(screen.getByText("peak $730.00")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(table.parentElement).toHaveClass("table-scroll");
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(screen.getByText("No trades yet")).toHaveClass("empty");
  });
});
