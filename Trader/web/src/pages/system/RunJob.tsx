// Run a job by hand (P4-T9 `POST /api/jobs/{job}/run`): a job, an optional session date and Force, a
// confirm step, then the server's launch message or its 404/409/422 message.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { JobRunIn, ManualJob } from "../../api/types";
import { Button, Card, errorMessage } from "../../components/ui";
import { Confirm } from "../settings/Confirm";

/** `trader token-refresh` takes no options: the server refuses a date or force for it. */
const NO_OPTIONS: ReadonlySet<ManualJob> = new Set<ManualJob>(["token-refresh"]);

export function RunJob({ jobs }: { jobs: ManualJob[] }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [job, setJob] = useState<ManualJob | "">(jobs[0] ?? "");
  const [date, setDate] = useState("");
  const [force, setForce] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const run = useMutation({
    mutationFn: ({ job, body }: { job: ManualJob; body: JobRunIn }) => api.runJob(job, body),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });

  const noOptions = job !== "" && NO_OPTIONS.has(job);
  const body: JobRunIn = {};
  if (!noOptions && date) body.date = date;
  if (!noOptions && force) body.force = true;
  const summary = `${job}${body.date ? ` for ${body.date}` : ""}${body.force ? " with Force" : ""}`;

  function confirm() {
    if (job === "") return;
    setConfirming(false);
    run.mutate({ job, body });
  }

  return (
    <Card title="Run a job">
      <div className="stack">
        <div className="row">
          <label htmlFor="run-job">Job to run</label>
          <select
            id="run-job"
            value={job}
            onChange={(e) => {
              setJob(e.target.value as ManualJob);
              setConfirming(false);
            }}
            style={{ minHeight: 44 }}
          >
            {jobs.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div className="row">
          <label htmlFor="run-job-date">Date (optional)</label>
          <input
            id="run-job-date"
            type="date"
            value={date}
            disabled={noOptions}
            onChange={(e) => setDate(e.target.value)}
            style={{ minHeight: 44 }}
          />
        </div>
        <p className="small muted">Leave the date empty for the job&apos;s own default session.</p>
        <label className="check-row">
          <input type="checkbox" checked={force} disabled={noOptions} onChange={(e) => setForce(e.target.checked)} />
          Force
        </label>
        {confirming ? (
          <Confirm message={`Run ${summary}?`} confirmLabel="Confirm" variant="primary" onConfirm={confirm} onCancel={() => setConfirming(false)} />
        ) : (
          <div>
            <Button variant="primary" busy={run.isPending} disabled={job === ""} onClick={() => setConfirming(true)}>
              Run
            </Button>
          </div>
        )}
        {run.isSuccess && <p className="small">{run.data.message}</p>}
        {run.isError && (
          <p className="small tone-bad" role="alert">
            {errorMessage(run.error)}
          </p>
        )}
      </div>
    </Card>
  );
}
