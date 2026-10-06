// Options, Strategies tab (OPTSIM-T15): one section per option strategy plug-in the server lists, each with
// its Enabled switch, the settings form generated from its `fields` (their descriptions, ASSUMPTION notes
// included, are shown under each field) and its own panel. Nothing here names a strategy.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { OPT_SLOW_MS, oqk, useOptionsApi } from "../../api/optionsClient";
import type { Items, JsonObject, OptStrategyOut, StrategyIn } from "../../api/types";
import { Button, Card, Empty, Loading, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { FieldInput } from "../settings/FieldInput";
import { useDraft, useObjectDraft } from "../settings/useDraft";
import { fieldErrorsFor, sameValue, validateValue, wireValue } from "../settings/validate";
import { PanelView } from "./PanelView";
import { OptErrorBox } from "./shared";

/** The body to send: only changed params, and `enabled` only when it changed; null when nothing changed. */
export function optStrategyChanges(strategy: OptStrategyOut, params: Record<string, unknown>, enabled: boolean): StrategyIn | null {
  const changed: JsonObject = {};
  for (const f of strategy.fields) {
    if (!sameValue(params[f.name], strategy.params[f.name])) changed[f.name] = wireValue(f, params[f.name]);
  }
  const body: StrategyIn = {};
  if (Object.keys(changed).length) body.params = changed;
  if (enabled !== strategy.enabled) body.enabled = enabled;
  return Object.keys(body).length ? body : null;
}

function StrategySection({ strategy }: { strategy: OptStrategyOut }) {
  const api = useOptionsApi();
  const queryClient = useQueryClient();
  const [params, setParam] = useObjectDraft(strategy.params);
  const [enabled, setEnabled] = useDraft<boolean>(strategy.enabled);
  const [lastSaved, setLastSaved] = useState<number | null>(null);

  const save = useMutation({
    mutationFn: (body: StrategyIn) => api.optPutStrategy(strategy.key, body),
    onSuccess: (saved) => {
      setLastSaved(saved.revision);
      queryClient.setQueryData<Items<OptStrategyOut>>(oqk.strategies(), (old) =>
        old ? { ...old, items: old.items.map((s) => (s.key === saved.key ? saved : s)) } : old,
      );
      void queryClient.invalidateQueries({ queryKey: oqk.panel(strategy.key) });
    },
  });

  const body = optStrategyChanges(strategy, params, enabled);
  const invalid = strategy.fields.some((f) => validateValue(f, params[f.name]) !== null);
  const names = strategy.fields.map((f) => f.name);
  const serverErrors = save.isError ? fieldErrorsFor(save.error, names) : { fields: {}, other: [] };
  const generalError = save.isError && Object.keys(serverErrors.fields).length === 0 && serverErrors.other.length === 0 ? errorMessage(save.error) : null;
  const touch = () => {
    if (save.isError || save.isSuccess) save.reset();
  };

  return (
    <Card title={strategy.key}>
      <section className="stack opt-gap" aria-label={strategy.key}>
        <p className="small muted">
          {`v${strategy.version} · revision ${strategy.revision} · ${strategy.open_structures} open`} · changed {fmtDateTime(strategy.updated_at)}
          {strategy.updated_by ? ` by ${strategy.updated_by}` : ""}
        </p>
        <PanelView strategyKey={strategy.key} />
        <h4 className="opt-subhead">Settings</h4>
        <label className="check-row">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => {
              touch();
              setEnabled(e.target.checked);
            }}
          />{" "}
          Enabled
        </label>
        {!enabled && strategy.open_structures > 0 && (
          <p className="small tone-warn opt-notice" role="note">
            It has open positions: disabled, it opens nothing new.
          </p>
        )}
        {strategy.fields.map((f) => (
          <FieldInput
            key={f.name}
            id={`opt-strategy-${strategy.key}-${f.name}`}
            field={f}
            value={params[f.name]}
            errors={serverErrors.fields[f.name] ?? []}
            onChange={(v) => {
              touch();
              setParam(f.name, v);
            }}
          />
        ))}
        <div className="row">
          <Button variant="primary" disabled={body === null || invalid} busy={save.isPending} onClick={() => body && save.mutate(body)}>
            Save
          </Button>
          {save.isSuccess && body === null && lastSaved !== null && (
            <span className="small tone-ok opt-toned" role="status">
              Saved as revision {lastSaved}.
            </span>
          )}
        </div>
        {(serverErrors.other.length > 0 || generalError) && (
          <div className="small tone-bad opt-notice" role="alert">
            {serverErrors.other.map((m, i) => (
              <p key={i}>{m}</p>
            ))}
            {generalError && <p>{generalError}</p>}
          </div>
        )}
      </section>
    </Card>
  );
}

export function StrategiesTab() {
  const api = useOptionsApi();
  const q = useQuery({ queryKey: oqk.strategies(), queryFn: () => api.optStrategies(), refetchInterval: OPT_SLOW_MS });
  if (q.isPending) return <Loading />;
  if (q.isError) return <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  if (q.data.items.length === 0) return <Empty>No option strategies are installed</Empty>;
  return (
    <div className="stack opt-gap">
      {q.data.items.map((s) => (
        <StrategySection key={s.key} strategy={s} />
      ))}
    </div>
  );
}
