// Form drafts that follow the server (P4-T15): when a refetch (SSE or after a save) brings a new server
// value, a draft that was not edited follows it; an edited draft is kept.
import { useEffect, useRef, useState } from "react";

import { sameValue } from "./validate";

/** A draft of one value. */
export function useDraft<T>(server: T): [T, (value: T) => void] {
  const [draft, setDraft] = useState<T>(server);
  const previous = useRef<T>(server);
  const key = JSON.stringify(server);
  useEffect(() => {
    if (sameValue(draft, previous.current)) setDraft(server);
    previous.current = server;
  }, [key]); // keyed on the content, not the identity
  return [draft, setDraft];
}

/** A draft of an object, followed key by key (an edit of one key never blocks the others from updating). */
export function useObjectDraft(server: Record<string, unknown>): [Record<string, unknown>, (key: string, value: unknown) => void] {
  const [draft, setDraft] = useState<Record<string, unknown>>(server);
  const previous = useRef(server);
  const key = JSON.stringify(server);
  useEffect(() => {
    setDraft((current) => {
      const next: Record<string, unknown> = { ...current };
      for (const k of new Set([...Object.keys(server), ...Object.keys(previous.current)])) {
        if (sameValue(current[k], previous.current[k])) next[k] = server[k];
      }
      return next;
    });
    previous.current = server;
  }, [key]);
  return [draft, (k, value) => setDraft((current) => ({ ...current, [k]: value }))];
}
