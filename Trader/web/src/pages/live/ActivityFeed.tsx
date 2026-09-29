// The dashboard's activity feed (DB-T8, design D5): the day's orders, fills, exits, proposals, kill-switch
// events, job failures, alerts and scan summaries, in the server's order (newest first), filtered by chip. Only
// exits carry a money tone; warnings get the amber status tone. Every text is plain text.
import { useState } from "react";

import type { ActivityChip, ActivityItemOut } from "../../api/types";
import { Button } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { Panel } from "./Panel";
import { Money } from "./PeriodPnl";
import { SafeLink } from "./safeLink";

import "./liveB.css";

export type ActivityFilter = "all" | ActivityChip;

const CHIPS: { key: ActivityFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "trades", label: "Trades" },
  { key: "proposals", label: "Proposals" },
  { key: "alerts", label: "Alerts" },
  { key: "scan", label: "Scan" },
];

/** DB-DENSE: the small kind chip on each row (a status look, never a money colour). */
const CHIP_SHORT: Record<ActivityChip, string> = { trades: "trade", proposals: "prop", alerts: "alert", scan: "scan" };

function ActivityRow({ item }: { item: ActivityItemOut }) {
  const showAmount = item.kind === "exit" && item.amount !== null;
  const tone = item.tone === "warn" ? "tone-warn" : "";
  return (
    <li className={["activity-item", tone].filter(Boolean).join(" ")} data-id={item.id} data-kind={item.kind}>
      <span className="activity-time num small muted">{fmtTime(item.ts)}</span>
      <span className="activity-kind" aria-hidden="true">
        {CHIP_SHORT[item.chip] ?? item.chip}
      </span>
      <span className="activity-text">
        {item.link ? (
          <SafeLink className="link-touch" to={item.link}>
            {item.text}
          </SafeLink>
        ) : (
          item.text
        )}
      </span>
      {showAmount && <Money value={item.amount} className="activity-amount" />}
    </li>
  );
}

export function ActivityFeed({ items, error, onRetry }: { items: ActivityItemOut[] | null; error?: string | null; onRetry?: () => void }) {
  const [filter, setFilter] = useState<ActivityFilter>("all");

  if (items === null) {
    return <Panel title="Activity" className="activity-panel" error={error ?? "Activity not available"} onRetry={onRetry} />;
  }

  const shown = filter === "all" ? items : items.filter((i) => i.chip === filter);
  const label = CHIPS.find((c) => c.key === filter)?.label ?? filter;
  let empty: string | null = null;
  if (items.length === 0) empty = "No activity yet today";
  else if (shown.length === 0) empty = `Nothing in ${label} today`;

  const chips = (
    <div className="live-chips" role="group" aria-label="Filter activity">
      {CHIPS.map((c) => (
        <Button key={c.key} className="live-chip" aria-pressed={filter === c.key} onClick={() => setFilter(c.key)}>
          {c.label}
        </Button>
      ))}
    </div>
  );

  return (
    <Panel title="Activity" className="activity-panel" badge={chips}>
      {empty !== null ? (
        <p className="panel-empty muted">{empty}</p>
      ) : (
        <ol className="activity-list" aria-label="Activity items">
          {shown.map((item) => (
            <ActivityRow key={item.id} item={item} />
          ))}
        </ol>
      )}
    </Panel>
  );
}
