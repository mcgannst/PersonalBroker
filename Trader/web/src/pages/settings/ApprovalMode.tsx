// The approval-mode toggle (BR-30, SPEC §6.2). Manual → Auto asks for confirmation; Auto → Manual saves at
// once. The server audits the change; pending proposals stay pending.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { SettingOut, SettingsOut } from "../../api/types";
import { Badge, Button, ErrorBox, Loading, errorMessage } from "../../components/ui";
import { Confirm } from "./Confirm";

export const APPROVAL_KEY = "approval_mode";
type Mode = "manual" | "auto";

/** Puts a saved setting into the cached settings list, so every view shows it at once. */
export function storeSetting(data: SettingsOut | undefined, saved: SettingOut): SettingsOut | undefined {
  if (!data) return data;
  return { ...data, items: data.items.map((s) => (s.key === saved.key ? saved : s)) };
}

export function ApprovalMode() {
  const api = useApi();
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: qk.settings(), queryFn: () => api.settings() });
  const [confirming, setConfirming] = useState(false);

  const save = useMutation({
    mutationFn: (mode: Mode) => api.putSetting(APPROVAL_KEY, mode),
    onSuccess: (saved) => {
      setConfirming(false);
      // The response is the stored setting; the SSE `settings` topic refreshes the rest.
      queryClient.setQueryData<SettingsOut>(qk.settings(), (old) => storeSetting(old, saved));
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    },
    onError: () => setConfirming(false),
  });

  if (settings.isPending) return <Loading />;
  if (settings.isError) return <ErrorBox error={settings.error} onRetry={() => void settings.refetch()} />;
  const current = settings.data.items.find((s) => s.key === APPROVAL_KEY)?.value === "auto" ? "auto" : "manual";

  const choose = (mode: Mode) => {
    if (mode === current || save.isPending) return;
    save.reset();
    if (mode === "auto") setConfirming(true);
    else save.mutate("manual");
  };

  return (
    <div className="stack">
      <p>
        Now:{" "}
        <Badge tone={current === "auto" ? "warn" : "info"}>{current === "auto" ? "AUTO" : "MANUAL"}</Badge>{" "}
        {current === "auto" ? "orders are placed without asking you." : "every order waits for your approval."}
      </p>
      <div className="row" role="group" aria-label="Approval mode">
        <Button variant={current === "manual" ? "primary" : "plain"} aria-pressed={current === "manual"} busy={save.isPending && save.variables === "manual"} onClick={() => choose("manual")}>
          Manual
        </Button>
        <Button variant={current === "auto" ? "primary" : "plain"} aria-pressed={current === "auto"} busy={save.isPending && save.variables === "auto"} onClick={() => choose("auto")}>
          Auto
        </Button>
      </div>
      {confirming && (
        <Confirm
          message="Every order will be placed without asking you. Continue?"
          confirmLabel="Switch to Auto"
          busy={save.isPending}
          onConfirm={() => save.mutate("auto")}
          onCancel={() => setConfirming(false)}
        />
      )}
      {save.isError && (
        <p className="tone-bad" role="alert">
          {errorMessage(save.error)}
        </p>
      )}
    </div>
  );
}
