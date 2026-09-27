// Every runtime setting (BR-53, SPEC §13), in collapsible groups (`SettingOut.group`), each with its own
// form generated from its `FieldOut` and its own Save. `approval_mode` has its own section (with the
// confirmation step), so it is not repeated here.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { SettingOut, SettingsOut } from "../../api/types";
import { Badge, Button, ErrorBox, Loading, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { APPROVAL_KEY, storeSetting } from "./ApprovalMode";
import { FieldInput } from "./FieldInput";
import { useDraft } from "./useDraft";
import { displayValue, fieldErrorsFor, sameValue, validateValue, wireValue } from "./validate";

function domId(key: string): string {
  return `setting-${key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

export function SettingRow({ setting }: { setting: SettingOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useDraft<unknown>(setting.value);

  const save = useMutation({
    mutationFn: (value: unknown) => api.putSetting(setting.key, value),
    onSuccess: (saved) => {
      queryClient.setQueryData<SettingsOut>(qk.settings(), (old) => storeSetting(old, saved));
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    },
  });

  const dirty = !sameValue(draft, setting.value);
  const rule = validateValue(setting.field, draft);
  const serverErrors = save.isError ? fieldErrorsFor(save.error, []).other : [];
  const otherError = save.isError && serverErrors.length === 0 ? errorMessage(save.error) : null;

  return (
    <article className="setting stack" aria-label={setting.key}>
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
          <span className="small tone-ok" role="status">
            Saved.
          </span>
        )}
      </div>
      {otherError && (
        <p className="small tone-bad" role="alert">
          {otherError}
        </p>
      )}
    </article>
  );
}

/** Settings grouped in the server's order (it sorts by group, then key). */
export function groupSettings(items: readonly SettingOut[]): [string, SettingOut[]][] {
  const groups = new Map<string, SettingOut[]>();
  for (const s of items) {
    if (s.key === APPROVAL_KEY) continue;
    const list = groups.get(s.group) ?? [];
    list.push(s);
    groups.set(s.group, list);
  }
  return [...groups.entries()];
}

export function SettingsGroups() {
  const api = useApi();
  const settings = useQuery({ queryKey: qk.settings(), queryFn: () => api.settings() });
  if (settings.isPending) return <Loading />;
  if (settings.isError) return <ErrorBox error={settings.error} onRetry={() => void settings.refetch()} />;
  return (
    <div className="stack">
      {groupSettings(settings.data.items).map(([group, items]) => (
        <details key={group} className="settings-group">
          <summary>{group}</summary>
          <div className="stack">
            {items.map((s) => (
              <SettingRow key={s.key} setting={s} />
            ))}
          </div>
        </details>
      ))}
    </div>
  );
}
