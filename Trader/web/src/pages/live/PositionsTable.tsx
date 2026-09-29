// The dashboard's open positions (DB-T8, design D2): up to 20 compact rows, sortable by unrealised $ (default,
// descending) or distance to stop (ascending, missing last); near-stop rows highlighted; a "prices stale" badge on
// the panel and on each row whose mark is stale or missing (never a hidden row); a tap expands a row's chart, at
// most `EXPAND_MAX` at once (a fourth tap collapses the oldest). The parent owns the expanded ids (`?expand=`).
import { useMemo, useState } from "react";

import type { LivePositionOut } from "../../api/types";
import { Button } from "../../components/ui";
import { Panel } from "./Panel";
import { PositionRow, StaleBadge } from "./PositionRow";

import "./liveB.css";

export type PositionSort = "unrealized" | "stop";

/** At most this many rows are expanded at once (the API's `EXPAND_MAX`). */
export const EXPAND_MAX = 3;

export interface PositionsTableProps {
  positions: LivePositionOut[] | null;
  closedToday: number;
  staleAfterSeconds: number;
  expanded: number[];
  onExpand: (ids: number[]) => void;
  error?: string | null;
  onRetry?: () => void;
}

const SORTS: { key: PositionSort; label: string }[] = [
  { key: "unrealized", label: "Unrealised $" },
  { key: "stop", label: "Distance to stop" },
];

/** A decimal string as a number for ordering only (never for arithmetic); null when absent or unparsable. */
function sortValue(value: string | null): number | null {
  if (value === null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Orders by `key` (descending or ascending), values that are missing last, then by id for a stable order. */
export function sortPositions(positions: LivePositionOut[], sort: PositionSort): LivePositionOut[] {
  const pick = sort === "unrealized" ? (p: LivePositionOut) => sortValue(p.unrealized) : (p: LivePositionOut) => sortValue(p.distance_to_stop_r);
  const dir = sort === "unrealized" ? -1 : 1;
  return [...positions].sort((a, b) => {
    const va = pick(a);
    const vb = pick(b);
    if (va === null || vb === null) {
      if (va === vb) return a.id - b.id;
      return va === null ? 1 : -1;
    }
    return va === vb ? a.id - b.id : (va - vb) * dir;
  });
}

/** The expanded ids after tapping `id`: removed when open, else appended with the oldest dropped beyond the cap. */
export function toggleExpanded(expanded: readonly number[], id: number): number[] {
  if (expanded.includes(id)) return expanded.filter((x) => x !== id);
  const next = [...expanded, id];
  return next.slice(Math.max(0, next.length - EXPAND_MAX));
}

function closedLine(n: number): string {
  return n === 1 ? "1 trade closed today" : `${n} trades closed today`;
}

export function PositionsTable({ positions, closedToday, staleAfterSeconds, expanded, onExpand, error, onRetry }: PositionsTableProps) {
  const [sort, setSort] = useState<PositionSort>("unrealized");
  const rows = useMemo(() => (positions ? sortPositions(positions, sort) : []), [positions, sort]);
  const anyStale = rows.some((p) => p.mark_state !== "live");
  const count = positions && positions.length > 0 ? <span className="small muted num">{positions.length} open</span> : null;
  const badge = (
    <span className="live-head-badges">
      {count}
      {anyStale && <StaleBadge title={`a price is older than ${staleAfterSeconds} s or missing`} />}
    </span>
  );

  if (positions === null) {
    return <Panel title="Positions" error={error ?? "Positions not available"} onRetry={onRetry} />;
  }

  if (positions.length === 0) {
    return (
      <Panel title="Positions">
        <div className="pos-empty">
          <p className="panel-empty muted">No open positions</p>
          <p className="muted small">{closedLine(closedToday)}</p>
        </div>
      </Panel>
    );
  }

  return (
    <Panel title="Positions" badge={badge}>
      <div className="live-chips" role="group" aria-label="Sort positions">
        {SORTS.map((s) => (
          <Button key={s.key} className="live-chip" aria-pressed={sort === s.key} onClick={() => setSort(s.key)}>
            {s.label}
          </Button>
        ))}
      </div>
      <ul className="pos-list" aria-label="Open positions">
        {rows.map((p) => (
          <PositionRow key={p.id} p={p} expanded={expanded.includes(p.id)} onToggle={() => onExpand(toggleExpanded(expanded, p.id))} />
        ))}
      </ul>
    </Panel>
  );
}
