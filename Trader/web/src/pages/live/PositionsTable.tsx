// The dashboard's open positions (DB-T8, design D2; dense layout DB-DENSE): up to `CARDS_MAX` positions as cards
// in a grid, more as up to 20 compact rows; sortable by unrealised $ (default, descending) or distance to stop
// (ascending, missing last); near-stop positions highlighted; a "prices stale" badge on the panel and on each
// position whose mark is stale or missing (never a hidden one); a tap expands a position's chart, at most
// `EXPAND_MAX` at once (a fourth tap collapses the oldest). The parent owns the expanded ids (`?expand=`). With
// no position the panel is one slim line: flat, today's closed count and what comes next.
import { useMemo, useState } from "react";

import type { LivePositionOut, SessionInfoOut, TimelineItemOut } from "../../api/types";
import { Button } from "../../components/ui";
import { fmtDate, fmtTime } from "../../lib/format";
import { Panel } from "./Panel";
import { PositionCard, PositionRow, StaleBadge } from "./PositionRow";

import "./liveB.css";

export type PositionSort = "unrealized" | "stop";

/** At most this many rows are expanded at once (the API's `EXPAND_MAX`). */
export const EXPAND_MAX = 3;

/** Up to this many positions show as cards; more switch to the compact rows. */
export const CARDS_MAX = 6;

export interface PositionsTableProps {
  positions: LivePositionOut[] | null;
  closedToday: number;
  staleAfterSeconds: number;
  expanded: number[];
  onExpand: (ids: number[]) => void;
  error?: string | null;
  onRetry?: () => void;
  /** DB-DENSE: for the flat line (a closed market names the next session). */
  session?: SessionInfoOut;
  /** DB-DENSE: for the flat line (the next step of the day). */
  timeline?: TimelineItemOut[] | null;
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

const SEP = " · ";

function closedLine(n: number): string {
  return n === 1 ? "1 trade closed today" : `${n} trades closed today`;
}

/** What the flat line says comes next: the next session on a closed day, else the day's next step, else null. */
export function flatNext(session: SessionInfoOut | undefined, timeline: TimelineItemOut[] | null | undefined): string | null {
  if (session && !session.is_session) return `market closed · next session ${fmtDate(session.date)}`;
  const next = (timeline ?? []).find((t) => t.status === "next");
  return next ? `waiting for ${next.label} ${fmtTime(next.at)}` : null;
}

export function PositionsTable({
  positions,
  closedToday,
  staleAfterSeconds,
  expanded: expandedIn,
  onExpand,
  error,
  onRetry,
  session,
  timeline,
}: PositionsTableProps) {
  // A hand-edited `?expand=` can hold more ids than the API allows: only the first EXPAND_MAX count here.
  const expanded = expandedIn.slice(0, EXPAND_MAX);
  const [sort, setSort] = useState<PositionSort>("unrealized");
  const rows = useMemo(() => (positions ? sortPositions(positions, sort) : []), [positions, sort]);
  const anyStale = rows.some((p) => p.mark_state !== "live");

  if (positions === null) {
    return <Panel title="Positions" className="pos-panel" error={error ?? "Positions not available"} onRetry={onRetry} />;
  }

  if (positions.length === 0) {
    const next = flatNext(session, timeline);
    return (
      <Panel title="Positions" className="pos-panel is-flat">
        <p className="pos-flat" data-testid="positions-flat">
          <span className="pos-flat-tag">FLAT</span>
          <span className="pos-flat-sep" aria-hidden="true">{SEP}</span>
          <span className="muted">No open positions</span>
          <span className="pos-flat-sep" aria-hidden="true">{SEP}</span>
          <span className="muted num">{closedLine(closedToday)}</span>
          {next !== null && (
            <>
              <span className="pos-flat-sep" aria-hidden="true">{SEP}</span>
              <span className="muted">{next}</span>
            </>
          )}
        </p>
      </Panel>
    );
  }

  const cards = positions.length <= CARDS_MAX;
  const toggle = (id: number) => () => onExpand(toggleExpanded(expanded, id));
  const badge = (
    <span className="live-head-badges">
      <span className="small muted num">{positions.length} open</span>
      {anyStale && <StaleBadge title={`a price is older than ${staleAfterSeconds} s or missing`} />}
      <span className="live-chips" role="group" aria-label="Sort positions">
        {SORTS.map((s) => (
          <Button key={s.key} className="live-chip" aria-pressed={sort === s.key} onClick={() => setSort(s.key)}>
            {s.label}
          </Button>
        ))}
      </span>
    </span>
  );

  return (
    <Panel title="Positions" className={cards ? "pos-panel has-cards" : "pos-panel has-rows"} badge={badge}>
      {cards ? (
        <ul className="pos-cards" aria-label="Open positions">
          {rows.map((p) => (
            <PositionCard key={p.id} p={p} expanded={expanded.includes(p.id)} onToggle={toggle(p.id)} />
          ))}
        </ul>
      ) : (
        <ul className="pos-list" aria-label="Open positions">
          {rows.map((p) => (
            <PositionRow key={p.id} p={p} expanded={expanded.includes(p.id)} onToggle={toggle(p.id)} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
