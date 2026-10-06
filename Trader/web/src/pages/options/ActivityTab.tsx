// Options, Activity tab (OPTSIM-T15): the merged feed (fills, lifecycle events, strategy decisions, alerts,
// prompts, orders), newest first, with a filter per kind.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { OPT_SLOW_MS, oqk, useOptionsApi, type OptActivityQuery } from "../../api/optionsClient";
import type { OptActivityKind, OptActivityOut } from "../../api/types";
import { Badge, Button, Card, Empty, Loading, type Tone } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { OptErrorBox } from "./shared";

export const ACTIVITY_KINDS: readonly { kind: OptActivityKind; label: string }[] = [
  { kind: "fill", label: "Fills" },
  { kind: "order", label: "Orders" },
  { kind: "lifecycle", label: "Lifecycle" },
  { kind: "decision", label: "Decisions" },
  { kind: "alert", label: "Alerts" },
  { kind: "prompt", label: "Prompts" },
];

const QUERY: OptActivityQuery = { limit: 100 };

function levelTone(level: string): Tone {
  const l = level.toLowerCase();
  if (l === "error" || l === "critical") return "bad";
  if (l === "warning" || l === "warn") return "warn";
  return "muted";
}

function Item({ item }: { item: OptActivityOut }) {
  return (
    <li className="opt-feed-item">
      <div className="row">
        <span className="small muted num">{fmtDateTime(item.ts)}</span>
        <Badge tone={levelTone(item.level)}>{item.kind}</Badge>
        <span className="small muted">
          {item.source}
          {item.underlying ? ` · ${item.underlying}` : ""}
        </span>
      </div>
      <div className="opt-wrap">
        <strong>{item.title}</strong>
        {item.detail ? ` · ${item.detail}` : ""}
      </div>
    </li>
  );
}

export function ActivityTab() {
  const api = useOptionsApi();
  const [hidden, setHidden] = useState<ReadonlySet<OptActivityKind>>(new Set());
  const q = useQuery({ queryKey: oqk.activity(QUERY), queryFn: () => api.optActivity(QUERY), refetchInterval: OPT_SLOW_MS });
  const toggle = (kind: OptActivityKind) =>
    setHidden((old) => {
      const next = new Set(old);
      if (!next.delete(kind)) next.add(kind);
      return next;
    });
  const items = (q.data?.items ?? []).filter((i) => !hidden.has(i.kind));
  return (
    <Card title="Activity">
      <div className="row" role="group" aria-label="Show">
        {ACTIVITY_KINDS.map(({ kind, label }) => (
          <Button key={kind} aria-pressed={!hidden.has(kind)} className={hidden.has(kind) ? "opt-filter" : "opt-filter is-on"} onClick={() => toggle(kind)}>
            {label}
          </Button>
        ))}
      </div>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : items.length === 0 ? (
        <Empty>{q.data.items.length === 0 ? "Nothing has happened yet" : "Nothing of the chosen kinds"}</Empty>
      ) : (
        <ul className="opt-feed" aria-label="Activity">
          {items.map((i) => (
            <Item key={i.id} item={i} />
          ))}
        </ul>
      )}
    </Card>
  );
}
