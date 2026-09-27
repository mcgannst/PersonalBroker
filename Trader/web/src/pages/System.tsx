// The System page (P4 plan, task P4-T16; SPEC §12 System; BR-55): token, worker and Telegram status, the
// time-zone check, rate limits, job runs and manual runs, errors and the event log, failed Telegram sends and
// the watchlist upload. Pasting a Questrade token and the Telegram test live on Settings (P4-T15); the cards
// link to /settings#questrade and /settings#telegram (orchestrator ruling, 2026-09-27).
import { useQuery } from "@tanstack/react-query";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import { ErrorBox, Loading } from "../components/ui";
import { ErrorList, EventLog } from "./system/EventLog";
import { JobRuns } from "./system/JobRuns";
import { RunJob } from "./system/RunJob";
import { FailedSends, RateLimits, StatusCards } from "./system/StatusCards";
import { WatchlistUpload } from "./system/WatchlistUpload";

export default function SystemPage() {
  const api = useApi();
  const system = useQuery({ queryKey: qk.system(), queryFn: () => api.system() });
  const meta = useQuery({ queryKey: qk.meta(), queryFn: () => api.meta() });

  return (
    <main className="page">
      <h1>System</h1>
      {system.isPending ? (
        <Loading />
      ) : system.isError ? (
        <ErrorBox error={system.error} onRetry={() => void system.refetch()} />
      ) : (
        <div className="stack">
          <StatusCards system={system.data} meta={meta.data ?? null} />
          <RateLimits rateLimit={system.data.rate_limit} />
          <JobRuns lastRuns={system.data.last_runs} />
          <RunJob jobs={system.data.manual_jobs} />
          <ErrorList errors={system.data.errors} />
          <EventLog />
          <FailedSends items={system.data.notifications_failed} />
          <WatchlistUpload />
        </div>
      )}
    </main>
  );
}
