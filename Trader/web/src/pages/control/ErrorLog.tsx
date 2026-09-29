// The Control page's Error log (live dashboard design §4.7, plan DB-T9): the newest warning-and-above
// `event_log` rows from `/api/control` (the log mirror's copies included), filtered here by level (that level
// and above) and source, as plain text. "Older events" opens the reused EventLog (`GET /api/events`).
import { useMemo, useState } from "react";

import type { EventOut } from "../../api/types";
import { Button } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { Panel } from "../live/Panel";
import { EventLog } from "../system/EventLog";
import { StatusChip, cardError, type ChipTone } from "./parts";

export const EMPTY_TEXT = "No warnings or errors";

const LEVEL_RANK: Record<string, number> = { debug: 0, info: 1, warning: 2, error: 3, critical: 4 };
const LEVELS = ["warning", "error", "critical"] as const;

function rank(level: string): number {
  return LEVEL_RANK[level] ?? LEVEL_RANK.warning!;
}

function levelTone(level: string): ChipTone {
  return rank(level) >= LEVEL_RANK.error! ? "bad" : "warn";
}

const SELECT_STYLE = { minHeight: 44 };

function Rows({ errors }: { errors: EventOut[] }) {
  const [level, setLevel] = useState("");
  const [source, setSource] = useState("");
  const sources = useMemo(() => [...new Set(errors.map((e) => e.source))].sort(), [errors]);
  const min = level ? rank(level) : -1;
  const shown = errors.filter((e) => rank(e.level) >= min && (source === "" || e.source === source));

  return (
    <div className="ctl-stack">
      <div className="ctl-row ctl-filters">
        <span className="ctl-row">
          <label htmlFor="error-log-level">Level</label>
          <select id="error-log-level" value={level} onChange={(e) => setLevel(e.target.value)} style={SELECT_STYLE}>
            <option value="">All</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l === "critical" ? "critical" : `${l} and above`}
              </option>
            ))}
          </select>
        </span>
        <span className="ctl-row ctl-shrink">
          <label htmlFor="error-log-source">Source</label>
          <select id="error-log-source" className="ctl-select" value={source} onChange={(e) => setSource(e.target.value)} style={SELECT_STYLE}>
            <option value="">All sources</option>
            {sources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </span>
        <span className="ctl-small ctl-muted num">{`${shown.length} of ${errors.length}`}</span>
      </div>
      {shown.length === 0 ? (
        <p className="ctl-muted">No events match these filters.</p>
      ) : (
        <ul className="ctl-list" aria-label="Warnings and errors">
          {shown.map((e) => (
            <li key={e.id} data-id={e.id}>
              <div className="ctl-row">
                <span className="ctl-small ctl-muted num">{fmtDateTime(e.ts)}</span>
                <StatusChip tone={levelTone(e.level)}>{e.level}</StatusChip>
                <span className="ctl-small ctl-muted ctl-text">{e.source}</span>
              </div>
              <div className="ctl-text">{e.message}</div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ErrorLog({ errors, error, onRetry }: { errors: EventOut[] | null; error?: string | null; onRetry?: () => void }) {
  const [older, setOlder] = useState(false);
  const failed = cardError(errors, error);
  return (
    <Panel title="Error log" error={failed} onRetry={onRetry}>
      {errors && (
        <div className="ctl-stack">
          {errors.length === 0 ? <p className="ctl-muted">{EMPTY_TEXT}</p> : <Rows errors={errors} />}
          <div className="ctl-row">
            <Button variant="plain" aria-expanded={older} onClick={() => setOlder((o) => !o)}>
              Older events
            </Button>
          </div>
          {older && <EventLog />}
        </div>
      )}
    </Panel>
  );
}
