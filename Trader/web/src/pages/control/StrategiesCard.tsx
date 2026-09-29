// The Control page's Strategies card (live dashboard design §4.3, plan DB-T9): each strategy on or off with
// its config revision and a link to its Settings section. The on/off toggle asks first, then makes exactly
// the call Settings' StrategyForms makes (`PUT /api/strategies/{key}` with `{enabled}`).
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { StrategyCardOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { Panel } from "../live/Panel";
import { safeLink } from "../live/safeLink";
import { Confirm } from "../settings/Confirm";
import { StatusChip, cardError } from "./parts";

function StrategyRow({ s }: { s: StrategyCardOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const save = useMutation({
    mutationFn: (enabled: boolean) => api.putStrategy(s.key, { enabled }),
    onSettled: () => {
      setConfirming(false);
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      void queryClient.invalidateQueries({ queryKey: qk.strategies() });
    },
  });
  const target = !s.enabled;
  const verb = target ? "on" : "off";
  const link = safeLink(s.settings_link);
  const meta = [`revision ${s.revision}`, `v${s.version}`, `updated ${fmtDateTime(s.updated_at)}${s.updated_by ? ` by ${s.updated_by}` : ""}`];
  if (s.max_positions !== null) meta.push(`max ${s.max_positions} positions`);

  return (
    <li>
      <div className="ctl-row ctl-spread">
        <span className="ctl-row">
          <span className="ctl-strong ctl-text">{s.key}</span>
          <span className="ctl-small ctl-muted">{s.kind}</span>
        </span>
        <StatusChip tone={s.enabled ? "ok" : "muted"}>{s.enabled ? "On" : "Off"}</StatusChip>
      </div>
      <div className="ctl-small ctl-muted ctl-text">{meta.join(" · ")}</div>
      {s.owns_open_positions && <div className="ctl-small ctl-warn">Owns open positions</div>}
      {confirming ? (
        <Confirm
          message={`Turn ${s.key} ${verb}?${!target && s.owns_open_positions ? " It owns open positions." : ""}`}
          confirmLabel={`Yes, turn ${verb} ${s.key}`}
          variant={target ? "primary" : "danger"}
          busy={save.isPending}
          onConfirm={() => save.mutate(target)}
          onCancel={() => setConfirming(false)}
        />
      ) : (
        <div className="ctl-row">
          <Button
            variant="plain"
            aria-label={`Turn ${verb} ${s.key}`}
            disabled={save.isPending}
            onClick={() => {
              save.reset();
              setConfirming(true);
            }}
          >
            {`Turn ${verb}`}
          </Button>
          {link && (
            <Link className="link-touch" to={link} aria-label={`${s.key} settings`}>
              Settings
            </Link>
          )}
        </div>
      )}
      {save.isError && (
        <p className="ctl-small ctl-bad" role="alert">
          {errorMessage(save.error)}
        </p>
      )}
    </li>
  );
}

export function StrategiesCard({ strategies, error, onRetry }: { strategies: StrategyCardOut[] | null; error?: string | null; onRetry?: () => void }) {
  return (
    <Panel title="Strategies" error={cardError(strategies, error)} onRetry={onRetry} empty="No strategies configured.">
      {strategies && strategies.length > 0 && (
        <ul className="ctl-list" aria-label="Strategies">
          {strategies.map((s) => (
            <StrategyRow key={s.key} s={s} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
