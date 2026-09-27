// The "New replay" form (SPEC §12 Replay, BR-54): a date range (default the 20 weekdays ending at the latest
// allowed session), a label, the data notes, the Offline box (checked and locked in market hours), setting
// overrides for the `override_keys` that have a descriptor in `settings()`, and per strategy its Enabled box
// and params. Only changed settings and params are sent. A 422 shows each message under its input; any other
// failure shows the server's message.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { JsonObject, ReplayIn, ReplayOptionsOut, ReplayStrategyIn, SettingOut, StrategyOut } from "../../api/types";
import { Button, ErrorBox, Loading, errorMessage } from "../../components/ui";
import { FieldInput } from "../settings/FieldInput";
import { strategyChanges } from "../settings/StrategyForms";
import { sameValue, validateValue, wireValue } from "../settings/validate";
import { replayHref } from "./ReplayList";
import { defaultRange, fieldPaths } from "./shared";

export const CATALYST_MODE_KEY = "replay.catalyst_mode";

/** The data notes shown above the form, from the options. */
export function dataNotes(o: ReplayOptionsOut): string {
  const archive = o.archive_from ? `archive from ${o.archive_from}` : "no candle archive yet";
  const snapshots = o.snapshots_from
    ? `universe snapshots from ${o.snapshots_from}; earlier days use today's universe (biased)`
    : "no universe snapshots yet; days without a snapshot use today's universe (biased)";
  return `Questrade data only from ${o.questrade_from}; ${archive}; ${snapshots}`;
}

function Messages({ messages }: { messages: readonly string[] | undefined }) {
  if (!messages?.length) return null;
  return (
    <div className="small tone-bad" role="alert">
      {messages.map((m, i) => (
        <p key={i}>{m}</p>
      ))}
    </div>
  );
}

function DateField({
  id,
  label,
  value,
  max,
  onChange,
  errors,
}: {
  id: string;
  label: string;
  value: string;
  max: string;
  onChange: (v: string) => void;
  errors?: readonly string[];
}) {
  return (
    <div className="field stack">
      <label htmlFor={id}>{label}</label>
      <input id={id} type="date" value={value} max={max} required aria-invalid={errors?.length ? true : undefined} onChange={(e) => onChange(e.target.value)} />
      <Messages messages={errors} />
    </div>
  );
}

type Edits = Record<string, unknown>;

function StrategySection({
  strategy,
  params,
  enabled,
  onParam,
  onEnabled,
  errors,
}: {
  strategy: StrategyOut;
  params: Record<string, unknown>;
  enabled: boolean;
  onParam: (name: string, value: unknown) => void;
  onEnabled: (value: boolean) => void;
  errors: Record<string, string[]>;
}) {
  const prefix = `strategies.${strategy.key}`;
  const own = [...(errors[prefix] ?? []), ...(errors[`${prefix}.enabled`] ?? []), ...(errors[`${prefix}.params`] ?? [])];
  return (
    <section className="card stack" aria-label={`Strategy ${strategy.key}`}>
      <h3>{strategy.key}</h3>
      <p className="small muted">{`v${strategy.version} · revision ${strategy.revision} · ${strategy.kind}`}</p>
      <label className="check-row">
        <input type="checkbox" checked={enabled} onChange={(e) => onEnabled(e.target.checked)} /> Enabled
      </label>
      <Messages messages={own} />
      {strategy.fields.map((f) => (
        <FieldInput
          key={f.name}
          id={`replay-strategy-${strategy.key}-${f.name}`}
          field={f}
          value={params[f.name]}
          errors={errors[`${prefix}.params.${f.name}`] ?? []}
          onChange={(v) => onParam(f.name, v)}
        />
      ))}
    </section>
  );
}

function Form({
  options,
  settings,
  strategies,
  onClose,
}: {
  options: ReplayOptionsOut;
  settings: SettingOut[];
  strategies: StrategyOut[];
  onClose?: () => void;
}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const initial = defaultRange(options.latest_allowed);
  const [from, setFrom] = useState(initial.from);
  const [to, setTo] = useState(initial.to);
  const [label, setLabel] = useState("");
  const [offlineChoice, setOfflineChoice] = useState(false);
  const [settingEdits, setSettingEdits] = useState<Edits>({});
  const [paramEdits, setParamEdits] = useState<Record<string, Edits>>({});
  const [enabledEdits, setEnabledEdits] = useState<Record<string, boolean>>({});
  const offline = options.offline_now || offlineChoice;

  const start = useMutation({
    mutationFn: (body: ReplayIn) => api.startReplay(body),
    onSuccess: (out) => {
      queryClient.setQueryData(qk.replay(out.id), out);
      void queryClient.invalidateQueries({ queryKey: ["replays"] });
      void queryClient.invalidateQueries({ queryKey: qk.replayOptions() });
      navigate(replayHref(out.id));
    },
  });
  const touch = () => {
    if (start.isError) start.reset();
  };

  const keys = new Set(options.override_keys);
  const overrideSettings = settings.filter((s) => keys.has(s.key));
  const settingValue = (s: SettingOut): unknown => (s.key in settingEdits ? settingEdits[s.key] : s.value);
  const paramsOf = (s: StrategyOut): Record<string, unknown> => ({ ...s.params, ...(paramEdits[s.key] ?? {}) });
  const enabledOf = (s: StrategyOut): boolean => enabledEdits[s.key] ?? s.enabled;

  // The body: only changed settings, and per strategy only changed params and a changed Enabled box.
  const overrides: JsonObject = {};
  for (const s of overrideSettings) {
    const v = settingValue(s);
    if (!sameValue(v, s.value)) overrides[s.key] = wireValue(s.field, v);
  }
  const strategyBodies: Record<string, ReplayStrategyIn> = {};
  for (const s of strategies) {
    const body = strategyChanges(s, paramsOf(s), enabledOf(s));
    if (body) strategyBodies[s.key] = body;
  }
  const invalid =
    from === "" ||
    to === "" ||
    overrideSettings.some((s) => validateValue(s.field, settingValue(s)) !== null) ||
    strategies.some((s) => s.fields.some((f) => validateValue(f, paramsOf(s)[f.name]) !== null));

  // No entries when the catalyst mode is "unknown" and an enabled strategy requires a catalyst.
  const catalystSetting = overrideSettings.find((s) => s.key === CATALYST_MODE_KEY);
  const catalystUnknown = catalystSetting !== undefined && settingValue(catalystSetting) === "unknown";
  const requiring = strategies.filter((s) => enabledOf(s) && paramsOf(s).require_catalyst === true).map((s) => s.key);
  const noEntries = catalystUnknown && requiring.length > 0;

  // Server messages, by field path.
  const paths = start.isError ? fieldPaths(start.error) : {};
  const shown = new Set<string>(["date_from", "date_to", "label", "offline"]);
  for (const s of overrideSettings) shown.add(`overrides.${s.key}`);
  for (const s of strategies) {
    for (const p of [`strategies.${s.key}`, `strategies.${s.key}.enabled`, `strategies.${s.key}.params`]) shown.add(p);
    for (const f of s.fields) shown.add(`strategies.${s.key}.params.${f.name}`);
  }
  const otherMessages = Object.entries(paths)
    .filter(([path]) => !shown.has(path))
    .flatMap(([, msgs]) => msgs);
  const general: string[] = start.isError ? [errorMessage(start.error), ...otherMessages] : [];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (invalid || start.isPending) return;
    const body: ReplayIn = { date_from: from, date_to: to };
    if (label.trim()) body.label = label.trim();
    body.overrides = overrides;
    body.strategies = strategyBodies;
    body.offline = offline;
    start.mutate(body);
  };

  const section = (title: string, children: ReactNode) => (
    <details className="card">
      <summary>{title}</summary>
      <div className="stack">{children}</div>
    </details>
  );

  return (
    <form className="card stack" aria-label="New replay" onSubmit={submit} noValidate>
      <h2>New replay</h2>
      <p className="small muted">{dataNotes(options)}</p>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <DateField
          id="replay-from"
          label="From"
          value={from}
          max={options.latest_allowed}
          errors={paths.date_from}
          onChange={(v) => {
            touch();
            setFrom(v);
          }}
        />
        <DateField
          id="replay-to"
          label="To"
          value={to}
          max={options.latest_allowed}
          errors={paths.date_to}
          onChange={(v) => {
            touch();
            setTo(v);
          }}
        />
      </div>
      <p className="small muted">{`At most ${options.max_sessions} sessions, ending no later than ${options.latest_allowed}.`}</p>
      <div className="field stack">
        <label htmlFor="replay-label">Label</label>
        <input
          id="replay-label"
          type="text"
          maxLength={200}
          autoComplete="off"
          placeholder="optional"
          value={label}
          onChange={(e) => {
            touch();
            setLabel(e.target.value);
          }}
        />
        <Messages messages={paths.label} />
      </div>
      <div className="stack">
        <label className="check-row">
          <input
            type="checkbox"
            checked={offline}
            disabled={options.offline_now}
            onChange={(e) => {
              touch();
              setOfflineChoice(e.target.checked);
            }}
          />{" "}
          Offline (database only)
        </label>
        {options.offline_now && (
          <p className="small tone-info" role="note">
            Market hours: the replay uses stored data only
          </p>
        )}
        <Messages messages={paths.offline} />
      </div>

      {overrideSettings.length > 0 &&
        section(
          "Adjust settings",
          overrideSettings.map((s) => (
            <FieldInput
              key={s.key}
              id={`replay-setting-${s.key}`}
              field={s.field}
              value={settingValue(s)}
              errors={paths[`overrides.${s.key}`] ?? []}
              onChange={(v) => {
                touch();
                setSettingEdits((cur) => ({ ...cur, [s.key]: v }));
              }}
            />
          )),
        )}

      {strategies.length > 0 &&
        section(
          "Strategies",
          strategies.map((s) => (
            <StrategySection
              key={s.key}
              strategy={s}
              params={paramsOf(s)}
              enabled={enabledOf(s)}
              errors={paths}
              onParam={(name, v) => {
                touch();
                setParamEdits((cur) => ({ ...cur, [s.key]: { ...(cur[s.key] ?? {}), [name]: v } }));
              }}
              onEnabled={(v) => {
                touch();
                setEnabledEdits((cur) => ({ ...cur, [s.key]: v }));
              }}
            />
          )),
        )}

      {noEntries && (
        <p className="small tone-warn" role="note">
          {`Catalyst mode "unknown" reports every name without a catalyst, and ${requiring.join(", ")} requires one: no entries will be taken.`}
        </p>
      )}

      {general.length > 0 && (
        <div className="small tone-bad" role="alert">
          {general.map((m, i) => (
            <p key={i}>{m}</p>
          ))}
        </div>
      )}

      <div className="row">
        <Button type="submit" variant="primary" disabled={invalid} busy={start.isPending}>
          Start replay
        </Button>
        {onClose && (
          <Button variant="plain" disabled={start.isPending} onClick={onClose}>
            Close
          </Button>
        )}
      </div>
    </form>
  );
}

export function ReplayForm({ options, onClose }: { options: ReplayOptionsOut; onClose?: () => void }) {
  const api = useApi();
  const settings = useQuery({ queryKey: qk.settings(), queryFn: () => api.settings() });
  const strategies = useQuery({ queryKey: qk.strategies(), queryFn: () => api.strategies() });
  if (settings.isPending || strategies.isPending) return <Loading />;
  if (settings.isError) return <ErrorBox error={settings.error} onRetry={() => void settings.refetch()} />;
  if (strategies.isError) return <ErrorBox error={strategies.error} onRetry={() => void strategies.refetch()} />;
  return <Form options={options} settings={settings.data.items} strategies={strategies.data.items} onClose={onClose} />;
}
