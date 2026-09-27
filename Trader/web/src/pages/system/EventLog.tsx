// Errors (from `/api/system`) and the event log (`/api/events`, newest first, a level filter, "Load older"
// pages backwards with `before`).
import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useApi, type EventsQuery } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { EventOut } from "../../api/types";
import { Badge, Button, Card, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { levelTone } from "../dashboard/labels";

const LEVELS = ["info", "warning", "error", "critical"] as const;

/** The page size sent to `/api/events` (the server's default `limit`): a shorter page is the last one. */
export const EVENTS_PAGE_SIZE = 100;

function EventRows({ events }: { events: EventOut[] }) {
  return (
    <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {events.map((e) => (
        <li key={e.id} className="small">
          <div className="row">
            <span className="muted">{fmtDateTime(e.ts)}</span>
            <Badge tone={levelTone(e.level)}>{e.level}</Badge>
            <span className="muted">{e.source}</span>
          </div>
          <div style={{ overflowWrap: "anywhere" }}>{e.message}</div>
        </li>
      ))}
    </ul>
  );
}

/** The recent errors and critical events. */
export function ErrorList({ errors }: { errors: EventOut[] }) {
  return <Card title="Errors">{errors.length === 0 ? <Empty>No errors.</Empty> : <EventRows events={errors} />}</Card>;
}

export function EventLog() {
  const api = useApi();
  const [level, setLevel] = useState("");
  const base: EventsQuery = level ? { level } : {};
  const query = useInfiniteQuery({
    queryKey: [...qk.events(base), "log"],
    queryFn: ({ pageParam }) =>
      api.events(pageParam === undefined ? { ...base, limit: EVENTS_PAGE_SIZE } : { ...base, limit: EVENTS_PAGE_SIZE, before: pageParam }),
    initialPageParam: undefined as number | undefined,
    // A page shorter than the limit is the last one: no "Load older" after it.
    getNextPageParam: (last) =>
      last.items.length >= EVENTS_PAGE_SIZE ? Math.min(...last.items.map((e) => e.id)) : undefined,
  });
  const events = query.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <Card
      title="Event log"
      actions={
        <div className="row small">
          <label htmlFor="event-level">Level</label>
          <select id="event-level" value={level} onChange={(e) => setLevel(e.target.value)} style={{ minHeight: 44 }}>
            <option value="">All</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l} and above
              </option>
            ))}
          </select>
        </div>
      }
    >
      {query.isPending ? (
        <Loading />
      ) : query.isError && events.length === 0 ? (
        <ErrorBox error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <div className="stack">
          {events.length === 0 ? <Empty>No events.</Empty> : <EventRows events={events} />}
          {query.isFetchNextPageError && <ErrorBox error={query.error} />}
          {query.hasNextPage ? (
            <div>
              <Button busy={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
                Load older
              </Button>
            </div>
          ) : (
            events.length > 0 && <p className="small muted">No older events.</p>
          )}
        </div>
      )}
    </Card>
  );
}
