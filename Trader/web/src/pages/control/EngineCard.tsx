// The Control page's Engine card (live dashboard design §4.1, plan DB-T9): approval mode through the reused
// ApprovalMode (its Manual -> Auto confirm), the trading state with Pause / Resume behind the existing
// Confirm step (the existing `POST /api/killswitch/pause|resume` routes), the live run and the deployed
// version. No new engine action.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { EngineOut, KillSwitchesOut, TradingState } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDate, fmtDateTime } from "../../lib/format";
import { Panel } from "../live/Panel";
import { ApprovalMode } from "../settings/ApprovalMode";
import { Confirm } from "../settings/Confirm";
import { Facts, StatusChip, cardError, type ChipTone } from "./parts";

type Action = "pause" | "resume";

const STATE: Record<TradingState, { word: string; tone: ChipTone }> = {
  running: { word: "Running", tone: "ok" },
  paused: { word: "Paused", tone: "warn" },
  blocked: { word: "Blocked by a kill switch", tone: "bad" },
};

const CONFIRM: Record<Action, { message: string; label: string; variant: "danger" | "primary" }> = {
  pause: { message: "Block new entries? Exits and stops keep working.", label: "Pause new entries", variant: "danger" },
  resume: { message: "Allow new entries again?", label: "Resume new entries", variant: "primary" },
};

function TradingControls({ engine }: { engine: EngineOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<Action | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: (a: Action): Promise<KillSwitchesOut> => (a === "pause" ? api.pause() : api.resume()),
    onMutate: () => setNotice(null),
    onSuccess: (out, a) => {
      queryClient.setQueryData(qk.killswitches(), out);
      setNotice(a === "pause" ? "Paused: no new entries." : "Resumed.");
    },
    onSettled: () => {
      setConfirming(null);
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      void queryClient.invalidateQueries({ queryKey: qk.killswitches() });
    },
  });
  const state = STATE[engine.trading] ?? { word: engine.trading, tone: "muted" as const };
  const action: Action = engine.trading === "paused" ? "resume" : "pause";
  const c = confirming ? CONFIRM[confirming] : null;

  return (
    <div className="ctl-stack">
      <div className="ctl-row">
        <StatusChip tone={state.tone}>{state.word}</StatusChip>
        {engine.trading === "paused" && engine.paused_at && <span className="ctl-small ctl-muted">since {fmtDateTime(engine.paused_at)}</span>}
      </div>
      {c && confirming ? (
        <Confirm
          message={c.message}
          confirmLabel={c.label}
          variant={c.variant}
          busy={act.isPending}
          onConfirm={() => act.mutate(confirming)}
          onCancel={() => setConfirming(null)}
        />
      ) : (
        <div className="ctl-row">
          <Button
            variant={action === "pause" ? "danger" : "primary"}
            disabled={act.isPending}
            onClick={() => {
              act.reset();
              setNotice(null);
              setConfirming(action);
            }}
          >
            {action === "pause" ? "Pause" : "Resume"}
          </Button>
        </div>
      )}
      {notice && (
        <p className="ctl-small ctl-ok" role="status">
          {notice}
        </p>
      )}
      {act.isError && (
        <p className="ctl-small ctl-bad" role="alert">
          {errorMessage(act.error)}
        </p>
      )}
    </div>
  );
}

export function EngineCard({ engine, error, onRetry }: { engine: EngineOut | null; error?: string | null; onRetry?: () => void }) {
  return (
    <Panel title="Engine" error={cardError(engine, error)} onRetry={onRetry}>
      {engine && (
        <div className="ctl-stack">
          <h3 className="ctl-sub">Approval mode</h3>
          <ApprovalMode />
          <h3 className="ctl-sub">Trading</h3>
          <TradingControls engine={engine} />
          <h3 className="ctl-sub">Run and version</h3>
          <Facts
            rows={[
              ["Live run", `#${engine.run_id} since ${fmtDate(engine.run_start_date)}`],
              ["Version", `${engine.version} (${engine.app_env})`],
              ["Alembic revision", engine.alembic_revision ?? "n/a"],
            ]}
          />
        </div>
      )}
    </Panel>
  );
}
