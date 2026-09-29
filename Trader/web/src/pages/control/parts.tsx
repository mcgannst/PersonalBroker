// Small shared pieces of the Control page's cards (DB-T9): the part-error lookup, an inline part error for
// cards that keep working controls when their data part failed, a status chip in the D9 status colours
// (accent, amber, orange, slate: never the money green or red) and a facts list.
import type { ReactNode } from "react";

import type { PartErrorOut } from "../../api/types";
import { Button } from "../../components/ui";

/** Shown when a part is null without a `part_errors` entry (should not happen; never blank). */
export const PART_MISSING_TEXT = "This part could not be read.";

/** The message of the first `part_errors` entry for any of `parts`, else null. */
export function partError(errors: readonly PartErrorOut[], ...parts: string[]): string | null {
  const hit = errors.find((e) => parts.includes(e.part));
  return hit ? hit.message : null;
}

/** The error a card shows: the given one, or a generic line when its part is null anyway. */
export function cardError(value: unknown, error: string | null | undefined): string | null {
  if (error) return error;
  return value === null || value === undefined ? PART_MISSING_TEXT : null;
}

/** The same error state as `Panel`'s, for use inside a card that keeps other content. */
export function PartError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="panel-error ctl-part-error" role="alert">
      <p className="ctl-muted ctl-text">{message}</p>
      {onRetry && (
        <Button variant="plain" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export type ChipTone = "ok" | "warn" | "bad" | "muted";

export function StatusChip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`ctl-chip ctl-chip-${tone}`}>{children}</span>;
}

/** A two-column list of facts (term, value); wraps on a phone. */
export function Facts({ rows, label }: { rows: [string, ReactNode][]; label?: string }) {
  return (
    <dl className="ctl-facts" aria-label={label}>
      {rows.map(([term, value]) => (
        <div className="ctl-fact" key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
