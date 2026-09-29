// The Control page (live dashboard design §4, plan DB-T9; D1, D6): everything that changes the engine, and
// the System page's contents. Fed by `GET /api/control` (query `qk.control()`, under the `system` prefix, so
// every existing mutation and SSE topic that refreshes `system` refreshes it). Each card shows its own part
// error with Retry (plan S15); a whole-request failure keeps the page frame and the last good data. Every
// action reuses an existing component or mutation route with its rules (confirm, typed reason, CSRF, audit).
import { useQuery } from "@tanstack/react-query";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import { ErrorBox, Loading } from "../components/ui";
import { EngineCard } from "./control/EngineCard";
import { ErrorLog } from "./control/ErrorLog";
import { HealthCard } from "./control/HealthCard";
import { JobsCard } from "./control/JobsCard";
import { KillSwitchCard } from "./control/KillSwitchCard";
import { SoakCard } from "./control/SoakCard";
import { StrategiesCard } from "./control/StrategiesCard";
import { partError } from "./control/parts";
import { WatchlistUpload } from "./system/WatchlistUpload";
import "./control/control.css";

export default function ControlPage() {
  const api = useApi();
  const control = useQuery({ queryKey: qk.control(), queryFn: () => api.control() });
  const retry = () => void control.refetch();
  const d = control.data;

  return (
    <main className="page control-page">
      <h1>Control</h1>
      {control.isError && <ErrorBox error={control.error} onRetry={retry} />}
      {d ? (
        <div className="control-grid">
          <div className="control-cell">
            <EngineCard engine={d.engine} error={partError(d.part_errors, "engine")} onRetry={retry} />
          </div>
          <div className="control-cell">
            <KillSwitchCard
              lights={d.killswitches}
              history={d.killswitch_history}
              error={partError(d.part_errors, "killswitches", "killswitch_history")}
              onRetry={retry}
            />
          </div>
          <div className="control-cell">
            <StrategiesCard strategies={d.strategies} error={partError(d.part_errors, "strategies")} onRetry={retry} />
          </div>
          <div className="control-cell">
            <JobsCard
              schedule={d.schedule}
              manualJobs={d.manual_jobs}
              session={d.session}
              error={partError(d.part_errors, "schedule")}
              onRetry={retry}
            />
          </div>
          <div className="control-cell control-wide">
            <HealthCard health={d.health} error={partError(d.part_errors, "health")} onRetry={retry} />
          </div>
          <div className="control-cell control-wide">
            <SoakCard soak={d.soak} error={partError(d.part_errors, "soak")} onRetry={retry} />
          </div>
          <div className="control-cell control-wide">
            <ErrorLog errors={d.errors} error={partError(d.part_errors, "errors")} onRetry={retry} />
          </div>
          <div className="control-cell control-wide">
            <WatchlistUpload />
          </div>
        </div>
      ) : (
        control.isPending && <Loading />
      )}
    </main>
  );
}
