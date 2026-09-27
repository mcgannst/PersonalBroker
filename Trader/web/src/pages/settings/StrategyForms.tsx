// Strategy plug-in settings (BR-10, BR-22): per strategy its version, revision, kind, an Enabled switch and a
// params form generated from `StrategyOut.fields`. Save sends only what changed; the server makes a new
// versioned, audited revision.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { Items, JsonObject, StrategyIn, StrategyOut } from "../../api/types";
import { Button, ErrorBox, Loading, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { CHECK_LABEL, FieldInput } from "./FieldInput";
import { useDraft, useObjectDraft } from "./useDraft";
import { fieldErrorsFor, sameValue, validateValue, wireValue } from "./validate";

/** The body to send: only changed params, and `enabled` only when it changed; null when nothing changed. */
export function strategyChanges(strategy: StrategyOut, params: Record<string, unknown>, enabled: boolean): StrategyIn | null {
  const changed: JsonObject = {};
  for (const f of strategy.fields) {
    if (!sameValue(params[f.name], strategy.params[f.name])) changed[f.name] = wireValue(f, params[f.name]);
  }
  const body: StrategyIn = {};
  if (Object.keys(changed).length) body.params = changed;
  if (enabled !== strategy.enabled) body.enabled = enabled;
  return Object.keys(body).length ? body : null;
}

function StrategyCard({ strategy }: { strategy: StrategyOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [params, setParam] = useObjectDraft(strategy.params);
  const [enabled, setEnabled] = useDraft<boolean>(strategy.enabled);
  const [lastSaved, setLastSaved] = useState<number | null>(null);

  const save = useMutation({
    mutationFn: (body: StrategyIn) => api.putStrategy(strategy.key, body),
    onSuccess: (saved) => {
      setLastSaved(saved.revision);
      queryClient.setQueryData<Items<StrategyOut>>(qk.strategies(), (old) =>
        old ? { ...old, items: old.items.map((s) => (s.key === saved.key ? saved : s)) } : old,
      );
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    },
  });

  const body = strategyChanges(strategy, params, enabled);
  const invalid = strategy.fields.some((f) => validateValue(f, params[f.name]) !== null);
  const names = strategy.fields.map((f) => f.name);
  const serverErrors = save.isError ? fieldErrorsFor(save.error, names) : { fields: {}, other: [] };
  const generalError = save.isError && Object.keys(serverErrors.fields).length === 0 && serverErrors.other.length === 0 ? errorMessage(save.error) : null;
  const touch = () => {
    if (save.isError || save.isSuccess) save.reset();
  };

  return (
    <article className="card stack" aria-label={strategy.key}>
      <header className="stack">
        <h3>{strategy.key}</h3>
        <p className="small muted">{`v${strategy.version} · revision ${strategy.revision} · ${strategy.kind}`}</p>
        <p className="small muted">
          Changed {fmtDateTime(strategy.updated_at)}
          {strategy.updated_by ? ` by ${strategy.updated_by}` : ""}
        </p>
      </header>
      <label style={CHECK_LABEL}>
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
      {!enabled && strategy.owns_open_positions && (
        <p className="small tone-warn" role="note">
          {strategy.key} owns an open position: disabled, it keeps managing its open position until flat (exits only).
        </p>
      )}
      {strategy.fields.map((f) => (
        <FieldInput
          key={f.name}
          id={`strategy-${strategy.key}-${f.name}`}
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
          <span className="small tone-ok" role="status">
            Saved as revision {lastSaved}.
          </span>
        )}
      </div>
      {(serverErrors.other.length > 0 || generalError) && (
        <div className="small tone-bad" role="alert">
          {serverErrors.other.map((m, i) => (
            <p key={i}>{m}</p>
          ))}
          {generalError && <p>{generalError}</p>}
        </div>
      )}
    </article>
  );
}

export function StrategyForms() {
  const api = useApi();
  const strategies = useQuery({ queryKey: qk.strategies(), queryFn: () => api.strategies() });
  if (strategies.isPending) return <Loading />;
  if (strategies.isError) return <ErrorBox error={strategies.error} onRetry={() => void strategies.refetch()} />;
  return (
    <div className="stack">
      {strategies.data.items.map((s) => (
        <StrategyCard key={s.key} strategy={s} />
      ))}
    </div>
  );
}
