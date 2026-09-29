// DB-T7 acceptance test 3: the books check.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { booksBroken, booksOk } from "../../test/liveFixtures";
import { BooksCheck } from "./BooksCheck";

const IDENTITY = "cash + positions at cost = starting cash + realised − fees";

describe("BooksCheck (acceptance test 3)", () => {
  it("shows ✓ with the identity when the books balance", () => {
    render(<BooksCheck books={booksOk} />);
    const region = screen.getByRole("region", { name: "Books" });
    expect(within(region).getByText("✓")).toHaveClass("status-ok");
    expect(region).toHaveTextContent(IDENTITY);
    expect(region).toHaveTextContent("to the cent");
    expect(within(region).queryByTestId("books-breakdown")).not.toBeInTheDocument();
  });

  it("shows ✗ and, expanded, each component and the difference to the cent", async () => {
    render(<BooksCheck books={booksBroken} />);
    const region = screen.getByRole("region", { name: "Books" });
    expect(within(region).getByText("✗")).toHaveClass("status-bad");
    const toggle = within(region).getByRole("button", { name: /breakdown/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const breakdown = within(region).getByTestId("books-breakdown");
    expect(breakdown).toHaveTextContent("Cash$111.93");
    expect(breakdown).toHaveTextContent("Positions at cost$646.80");
    expect(breakdown).toHaveTextContent("Actual$758.73");
    expect(breakdown).toHaveTextContent("Starting cash$750.00");
    expect(breakdown).toHaveTextContent("Realised (gross)+$10.73");
    expect(breakdown).toHaveTextContent("Fees$3.00");
    expect(breakdown).toHaveTextContent("Expected$757.73");
    expect(breakdown).toHaveTextContent("Difference$1.00");
    expect(breakdown).toHaveTextContent("Realised (recorded)+$8.73");
    await userEvent.click(toggle);
    expect(within(region).queryByTestId("books-breakdown")).not.toBeInTheDocument();
  });

  it("with no books and an error: shows the error and Retry", async () => {
    const onRetry = vi.fn();
    render(<BooksCheck books={null} error="OperationalError: books could not be read" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("OperationalError: books could not be read");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("with no books and no error: an empty state", () => {
    render(<BooksCheck books={null} />);
    expect(screen.getByText("No books check yet")).toBeInTheDocument();
  });
});
