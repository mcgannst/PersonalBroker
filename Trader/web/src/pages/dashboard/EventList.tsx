// The latest events: MT time, level colour, source and message (P4-T13).
import type { EventOut } from "../../api/types";
import { Badge } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { levelTone } from "./labels";

export const MAX_EVENTS = 20;

export default function EventList({ events }: { events: EventOut[] }) {
  if (events.length === 0) return <p className="empty">No events yet</p>;
  return (
    <ul className="plain-list event-list" aria-label="Events">
      {events.slice(0, MAX_EVENTS).map((e) => (
        <li key={e.id} className="event">
          <div className="row small">
            <span className="num muted">{fmtTime(e.ts)}</span>
            <Badge tone={levelTone(e.level)}>{e.level}</Badge>
            <span className="muted">{e.source}</span>
          </div>
          <p className="event-message">{e.message}</p>
        </li>
      ))}
    </ul>
  );
}
