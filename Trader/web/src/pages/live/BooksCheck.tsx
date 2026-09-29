// The books check (plan S3, DB-T7): ✓ when cash + positions at cost equals starting cash + realised − fees to
// the cent, ✗ otherwise; a 44 px toggle expands each component and the difference. The glyph uses status
// tokens (accent / orange), never green or red: it is a status, not money.
import { type ReactNode, useId, useState } from "react";

import type { BooksCheckOut } from "../../api/types";
import { Button } from "../../components/ui";
import { Money } from "./PeriodPnl";
import { Panel } from "./Panel";
import "./liveA.css";

export const BOOKS_IDENTITY = "cash + positions at cost = starting cash + realised − fees";

function Line({ label, children, total = false }: { label: string; children: ReactNode; total?: boolean }) {
  return (
    <div className={total ? "lva-fact lva-total" : "lva-fact"}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function Breakdown({ books, id }: { books: BooksCheckOut; id: string }) {
  return (
    <dl className="lva-breakdown" id={id} data-testid="books-breakdown">
      <Line label="Cash">
        <Money value={books.cash} tone="flat" />
      </Line>
      <Line label="Positions at cost">
        <Money value={books.positions_at_cost} tone="flat" />
      </Line>
      <Line label="Actual" total>
        <Money value={books.actual} tone="flat" />
      </Line>
      <Line label="Starting cash">
        <Money value={books.starting_cash} tone="flat" />
      </Line>
      <Line label="Realised (gross)">
        <Money value={books.realized_gross} signed />
      </Line>
      <Line label="Fees">
        <Money value={books.fees_paid} tone="flat" />
      </Line>
      <Line label="Expected" total>
        <Money value={books.expected} tone="flat" />
      </Line>
      <Line label="Difference" total>
        <Money value={books.difference} tone="flat" />
      </Line>
      <Line label="Realised (recorded)">
        <Money value={books.realized_recorded} signed />
      </Line>
      <Line label="Open positions">
        <span className="num">{books.open_positions}</span>
      </Line>
    </dl>
  );
}

function BooksBody({ books }: { books: BooksCheckOut }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="lva-books">
      <div className="lva-books-head">
        <span className={`lva-glyph ${books.ok ? "status-ok" : "status-bad"}`} aria-hidden="true">
          {books.ok ? "✓" : "✗"}
        </span>
        <span className="lva-books-text">
          <span className="lva-sr">{books.ok ? "Books balance: " : "Books do not balance: "}</span>
          {BOOKS_IDENTITY}, to the cent
        </span>
      </div>
      <Button className="lva-link-button" aria-expanded={open} aria-controls={id} onClick={() => setOpen((v) => !v)}>
        {open ? "Hide breakdown" : "Show breakdown"}
      </Button>
      {open && <Breakdown books={books} id={id} />}
    </div>
  );
}

export function BooksCheck({ books, error, onRetry }: { books: BooksCheckOut | null; error?: string | null; onRetry?: () => void }) {
  return (
    <Panel title="Books" error={books ? null : error} onRetry={onRetry} empty={books ? null : "No books check yet"}>
      {books && <BooksBody books={books} />}
    </Panel>
  );
}
