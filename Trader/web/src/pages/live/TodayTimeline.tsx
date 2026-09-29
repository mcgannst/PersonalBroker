// "Today" on the dashboard (DB-T8, design §3 item 5): the day's jobs and engine events with their MT times and
// status marks, reusing the Dashboard's `Timeline` list; on a non-session day, the next session's date.
import type { SessionInfoOut, TimelineItemOut } from "../../api/types";
import { fmtDate } from "../../lib/format";
import Timeline from "../dashboard/Timeline";
import { Panel } from "./Panel";

import "./liveB.css";

export function TodayTimeline({
  timeline,
  session,
  error,
  onRetry,
}: {
  timeline: TimelineItemOut[] | null;
  /** On a non-session day `session.date` is the next session (the API's `SessionInfoOut`). */
  session: SessionInfoOut;
  error?: string | null;
  onRetry?: () => void;
}) {
  if (timeline === null) {
    return <Panel title="Today" error={error ?? "Timeline not available"} onRetry={onRetry} />;
  }
  return (
    <Panel title="Today">
      <div className="today-timeline">
        {!session.is_session && <p className="panel-empty muted">{`Market closed today; next session ${fmtDate(session.date)}`}</p>}
        {(session.is_session || timeline.length > 0) && <Timeline items={timeline} />}
      </div>
    </Panel>
  );
}
