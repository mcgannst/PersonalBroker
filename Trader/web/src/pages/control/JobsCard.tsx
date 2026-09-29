// The Control page's "Today's schedule and jobs" card (live dashboard design §4.4, plan DB-T9): every
// expected job and event with its MT time, status, duration, attempts and key detail; Re-run for the day jobs
// the System page already allowed, which opens the reused RunJob preselected (it keeps its confirm step and
// the existing `POST /api/jobs/{job}/run`); RunJob for every manual job below.
import { useEffect, useRef, useState } from "react";

import type { ManualJob, ScheduleItemOut, SessionInfoOut, TimelineStatus } from "../../api/types";
import { Button } from "../../components/ui";
import { fmtDate, fmtDuration, fmtTime } from "../../lib/format";
import { Panel } from "../live/Panel";
import { RunJob } from "../system/RunJob";
import { PartError, StatusChip, cardError, type ChipTone } from "./parts";

const STATUS_TONE: Record<TimelineStatus, ChipTone> = {
  done: "ok",
  running: "ok",
  next: "ok",
  failed: "bad",
  missed: "bad",
  skipped: "muted",
  upcoming: "muted",
};

function ScheduleRow({ item, onRerun }: { item: ScheduleItemOut; onRerun: (job: ManualJob) => void }) {
  const facts: string[] = [];
  if (item.duration_seconds !== null) facts.push(`took ${fmtDuration(item.duration_seconds)}`);
  if (item.attempts > 1) facts.push(`${item.attempts} attempts`);
  return (
    <li>
      <div className="ctl-row ctl-spread">
        <span className="ctl-row">
          <span className="ctl-small ctl-muted num">{fmtTime(item.at)}</span>
          <span className="ctl-strong ctl-text">{item.label}</span>
        </span>
        <StatusChip tone={STATUS_TONE[item.status] ?? "muted"}>{item.status}</StatusChip>
      </div>
      {facts.length > 0 && (
        <div className="ctl-row ctl-small ctl-muted">
          {facts.map((f) => (
            <span key={f}>{f}</span>
          ))}
        </div>
      )}
      {item.summary && <div className="ctl-small ctl-text">{item.summary}</div>}
      {item.detail && <div className="ctl-small ctl-muted ctl-text">{item.detail}</div>}
      {item.rerun && (
        <div className="ctl-row">
          <Button variant="plain" aria-label={`Re-run ${item.label}`} onClick={() => onRerun(item.rerun as ManualJob)}>
            Re-run
          </Button>
        </div>
      )}
    </li>
  );
}

export function JobsCard({
  schedule,
  manualJobs,
  session,
  error,
  onRetry,
}: {
  schedule: ScheduleItemOut[] | null;
  manualJobs: ManualJob[];
  session: SessionInfoOut;
  error?: string | null;
  onRetry?: () => void;
}) {
  const [preselect, setPreselect] = useState<{ job: ManualJob; n: number } | null>(null);
  const runRef = useRef<HTMLDivElement>(null);
  const failed = cardError(schedule, error);

  // RunJob starts on its first job: a Re-run remounts it with that job first (the same jobs, reordered).
  const jobs: ManualJob[] = preselect
    ? [preselect.job, ...manualJobs.filter((j) => j !== preselect.job)]
    : manualJobs;

  useEffect(() => {
    if (!preselect) return;
    const select = runRef.current?.querySelector("select");
    if (select) {
      select.focus();
      if (typeof select.scrollIntoView === "function") select.scrollIntoView({ block: "center" });
    }
  }, [preselect]);

  return (
    <Panel title="Today's schedule and jobs">
      <div className="ctl-stack">
        <p className="ctl-small ctl-muted">
          {session.is_session ? `Session ${fmtDate(session.date)}` : `No session today. Next session ${fmtDate(session.date)}`}
        </p>
        {failed || schedule === null ? (
          <PartError message={failed ?? ""} onRetry={onRetry} />
        ) : schedule.length === 0 ? (
          <p className="ctl-muted">Nothing scheduled.</p>
        ) : (
          <ul className="ctl-list" aria-label="Schedule">
            {schedule.map((item) => (
              <ScheduleRow key={item.key} item={item} onRerun={(job) => setPreselect((p) => ({ job, n: (p?.n ?? 0) + 1 }))} />
            ))}
          </ul>
        )}
        <div ref={runRef}>
          <RunJob key={preselect ? `${preselect.job}:${preselect.n}` : "default"} jobs={jobs} />
        </div>
      </div>
    </Panel>
  );
}
