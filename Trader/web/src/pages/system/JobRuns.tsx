// Job runs: the newest run per job (from `/api/system`), or one job's history (`/api/jobs?job=`).
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { MANUAL_JOBS, type JobRunOut } from "../../api/types";
import { Badge, Card, Empty, ErrorBox, Loading, Table, type Tone } from "../../components/ui";
import { fmtDate, fmtDateTime, fmtDuration } from "../../lib/format";

function statusTone(status: string): Tone {
  if (status === "succeeded") return "ok";
  if (status === "failed") return "bad";
  if (status === "running") return "info";
  if (status === "skipped") return "muted";
  return "warn";
}

function firstLine(text: string | null): string {
  return text ? (text.split("\n")[0] ?? "") : "";
}

function RunsTable({ runs }: { runs: JobRunOut[] }) {
  if (runs.length === 0) return <Empty>No job runs.</Empty>;
  return (
    <Table aria-label="Job runs">
      <thead>
        <tr>
          <th>Job</th>
          <th>Session</th>
          <th>Status</th>
          <th>Started</th>
          <th className="num">Duration</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.id}>
            <td>{run.job}</td>
            <td>{fmtDate(run.session_date)}</td>
            <td>
              <Badge tone={statusTone(run.status)}>{run.status}</Badge>
            </td>
            <td>{fmtDateTime(run.started_at)}</td>
            <td className="num">{run.duration_seconds === null ? "" : fmtDuration(run.duration_seconds)}</td>
            <td>{firstLine(run.error)}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function JobHistory({ job }: { job: string }) {
  const api = useApi();
  const query = useQuery({ queryKey: qk.jobs({ job }), queryFn: () => api.jobs({ job }) });
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} onRetry={() => void query.refetch()} />;
  return <RunsTable runs={query.data.items} />;
}

export function JobRuns({ lastRuns }: { lastRuns: JobRunOut[] }) {
  const [job, setJob] = useState("");
  const names = [...new Set([...lastRuns.map((r) => r.job), ...MANUAL_JOBS])].sort();
  return (
    <Card
      title="Job runs"
      actions={
        <div className="row small">
          <label htmlFor="job-filter">Job</label>
          <select id="job-filter" value={job} onChange={(e) => setJob(e.target.value)} style={{ minHeight: 44 }}>
            <option value="">Latest per job</option>
            {names.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
      }
    >
      {job === "" ? <RunsTable runs={lastRuns} /> : <JobHistory job={job} />}
    </Card>
  );
}
