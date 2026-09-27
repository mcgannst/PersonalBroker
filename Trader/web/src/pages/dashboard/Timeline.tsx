// The session timeline: one row per job or engine event with its MT time, label and status (P4-T13).
import type { TimelineItemOut } from "../../api/types";
import { fmtTime } from "../../lib/format";
import { timelineLook } from "./labels";

export default function Timeline({ items }: { items: TimelineItemOut[] }) {
  if (items.length === 0) return <p className="empty">No schedule for this day</p>;
  return (
    <ol className="timeline" aria-label="Timeline">
      {items.map((item) => {
        const look = timelineLook(item.status);
        const isNext = item.status === "next";
        const classes = ["timeline-item", `status-${item.status}`, look.tone ? `tone-${look.tone}` : "", isNext ? "is-next" : ""]
          .filter(Boolean)
          .join(" ");
        return (
          <li key={`${item.key}@${item.at}`} data-key={item.key} className={classes} aria-current={isNext ? "step" : undefined}>
            <span className="timeline-time num">{fmtTime(item.at)}</span>
            <span className="timeline-label">
              {item.label}
              {item.detail && <span className="small muted">{` ${item.detail}`}</span>}
            </span>
            <span className="timeline-status text-tone">{look.text}</span>
          </li>
        );
      })}
    </ol>
  );
}
