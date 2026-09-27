// The journal editor for one session day (BR-60 on the web): Rules followed Yes / No / clear, notes up to 5,000
// characters with a counter, Save. Only the fields that changed are sent (the server leaves the others alone).
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import type { IsoDate, JournalDayOut, JournalIn } from "../../api/types";
import { Button, Card, ErrorBox } from "../../components/ui";

/** The server's limit on `JournalIn.notes`. */
export const NOTES_MAX = 5000;

interface Draft {
  rules_followed: boolean | null;
  notes: string;
}

function draftOf(day: JournalDayOut | undefined): Draft {
  return { rules_followed: day?.rules_followed ?? null, notes: day?.notes ?? "" };
}

/** The body for `putJournal`: only what differs from `saved`; empty notes are sent as null. */
export function journalChanges(saved: Draft, draft: Draft): JournalIn {
  const body: JournalIn = {};
  if (draft.rules_followed !== saved.rules_followed) body.rules_followed = draft.rules_followed;
  if (draft.notes !== saved.notes) body.notes = draft.notes === "" ? null : draft.notes;
  return body;
}

export function JournalEditor({ date, day, onClose }: { date: IsoDate; day: JournalDayOut | undefined; onClose: () => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [saved, setSaved] = useState<Draft>(() => draftOf(day));
  const [draft, setDraft] = useState<Draft>(() => draftOf(day));
  const [done, setDone] = useState(false);
  const body = journalChanges(saved, draft);
  const dirty = Object.keys(body).length > 0;

  const save = useMutation({
    mutationFn: (v: { body: JournalIn; draft: Draft }) => api.putJournal(date, v.body),
    onSuccess: (_day, v) => {
      setSaved(v.draft);
      setDone(true);
      void queryClient.invalidateQueries({ queryKey: ["journal"] });
      void queryClient.invalidateQueries({ queryKey: ["metrics"] });
    },
  });

  const edit = (next: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...next }));
    setDone(false);
  };

  const answer = (value: boolean | null, label: string) => (
    <Button
      variant={draft.rules_followed === value ? "primary" : "plain"}
      aria-pressed={draft.rules_followed === value}
      onClick={() => edit({ rules_followed: value })}
    >
      {label}
    </Button>
  );

  return (
    <div role="region" aria-label={`Journal for ${date}`}>
      <Card
        title={`Journal for ${date}`}
        actions={
          <Button variant="plain" onClick={onClose}>
            Close
          </Button>
        }
      >
        <p>Did you follow your rules?</p>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          {answer(true, "Yes")}
          {answer(false, "No")}
          <Button
            variant="plain"
            aria-pressed={false}
            disabled={draft.rules_followed === null}
            onClick={() => edit({ rules_followed: null })}
          >
            Clear answer
          </Button>
        </div>
        <label style={{ display: "block", marginTop: 12 }}>
          Notes
          <textarea
            rows={5}
            maxLength={NOTES_MAX}
            value={draft.notes}
            onChange={(e) => edit({ notes: e.target.value.slice(0, NOTES_MAX) })}
            style={{ width: "100%", boxSizing: "border-box" }}
          />
        </label>
        <p className="small muted" aria-live="polite">
          {draft.notes.length} / {NOTES_MAX}
        </p>
        {save.isError && <ErrorBox error={save.error} />}
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <Button variant="primary" busy={save.isPending} disabled={!dirty} onClick={() => save.mutate({ body, draft })}>
            Save
          </Button>
          {done && !dirty && <span className="tone-ok">Saved</span>}
        </div>
      </Card>
    </div>
  );
}
