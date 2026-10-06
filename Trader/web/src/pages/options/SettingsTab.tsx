// Options, Settings tab (OPTSIM-T15): the `options.*` settings, each with the form generated from its
// `FieldOut` (the same `FieldInput` as the Settings page) and its own Save.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { OPT_SLOW_MS, oqk, useOptionsApi } from "../../api/optionsClient";
import type { SettingOut, SettingsOut } from "../../api/types";
import { Badge, Button, Card, Empty, Loading, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { FieldInput } from "../settings/FieldInput";
import { useDraft } from "../settings/useDraft";
import { displayValue, fieldErrorsFor, sameValue, validateValue, wireValue } from "../settings/validate";
import { OptErrorBox } from "./shared";

function domId(key: string): string {
  return `opt-setting-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function OptSettingRow({ setting }: { setting: SettingOut }) {
  const api = useOptionsApi();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useDraft<unknown>(setting.value);

  const save = useMutation({
    mutationFn: (value: unknown) => api.optPutSetting(setting.key, value),
    onSuccess: (saved) => {
      queryClient.setQueryData<SettingsOut>(oqk.settings(), (old) => (old ? { ...old, items: old.items.map((s) => (s.key === saved.key ? saved : s)) } : old));
      void queryClient.invalidateQueries({ queryKey: oqk.account() });
    },
  });

  const dirty = !sameValue(draft, setting.value);
  const rule = validateValue(setting.field, draft);
  const serverErrors = save.isError ? fieldErrorsFor(save.error, []).other : [];
  const otherError = save.isError && serverErrors.length === 0 ? errorMessage(save.error) : null;

  return (
    <article className="opt-block stack" aria-label={setting.key}>
      <FieldInput
        id={domId(setting.key)}
        field={setting.field}
        value={draft}
        errors={serverErrors}
        onChange={(v) => {
          if (save.isError || save.isSuccess) save.reset();
          setDraft(v);
        }}
      />
      <p className="small muted">
        <code>{setting.key}</code> · Current: {displayValue(setting.value)}{" "}
        {setting.is_default ? <Badge tone="muted">default</Badge> : <span>(default {displayValue(setting.default)})</span>}
      </p>
      {setting.updated_by && (
        <p className="small muted">
          Changed by {setting.updated_by}
          {setting.updated_at ? `, ${fmtDateTime(setting.updated_at)}` : ""}
        </p>
      )}
      <div className="row">
        <Button variant="primary" disabled={!dirty || rule !== null} busy={save.isPending} onClick={() => save.mutate(wireValue(setting.field, draft))}>
          Save
        </Button>
        {dirty && (
          <Button variant="plain" disabled={save.isPending} onClick={() => setDraft(setting.value)}>
            Undo
          </Button>
        )}
        {save.isSuccess && !dirty && (
          <span className="small tone-ok opt-toned" role="status">
            Saved.
          </span>
        )}
      </div>
      {otherError && (
        <p className="small tone-bad opt-notice" role="alert">
          {otherError}
        </p>
      )}
    </article>
  );
}

export function SettingsTab() {
  const api = useOptionsApi();
  const q = useQuery({ queryKey: oqk.settings(), queryFn: () => api.optSettings(), refetchInterval: OPT_SLOW_MS });
  return (
    <Card title="Options settings">
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.items.length === 0 ? (
        <Empty>No settings</Empty>
      ) : (
        <div className="stack opt-gap">
          {q.data.items.map((s) => (
            <OptSettingRow key={s.key} setting={s} />
          ))}
        </div>
      )}
    </Card>
  );
}
