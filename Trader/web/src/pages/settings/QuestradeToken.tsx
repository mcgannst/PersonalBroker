// Paste a Questrade refresh token (SPEC §4.1). The token is a secret: the input is a password field and
// uncontrolled (the value lives only in the DOM input, never in React state), it is read and cleared in the
// same step as the submit, and the call is made directly (`useSecretCall`) rather than through `useMutation`,
// whose cache would keep the token as the mutation's `variables`. Only the resulting token status (which
// never contains the token) or the server's message is kept.
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { TokenOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";
import { useSecretCall } from "./useSecretCall";

function tokenStatus(t: TokenOut): string {
  if (!t.ok) return `Token not OK: ${t.error ?? "unknown error"}`;
  const parts = ["Token OK"];
  if (t.expires_at) parts.push(`access expires ${fmtDateTime(t.expires_at)}`);
  if (t.last_refresh_at) parts.push(`last refresh ${fmtDateTime(t.last_refresh_at)}`);
  return parts.join(", ");
}

export function QuestradeToken() {
  const api = useApi();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [hasText, setHasText] = useState(false);
  const save = useSecretCall<TokenOut>();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const el = input.current;
    if (!el || save.isPending) return;
    const value = el.value.trim();
    el.value = ""; // never keep the token in the page once it is sent
    setHasText(false);
    if (!value) return;
    void save
      .run(() => api.putQuestradeToken(value))
      .finally(() => {
        void queryClient.invalidateQueries({ queryKey: qk.system() });
        void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
      });
  };

  return (
    <form className="stack" onSubmit={submit} aria-label="Questrade token">
      <p className="small">
        Paste a new refresh token generated in Questrade's API centre. It is checked at once and never shown again.
      </p>
      <label htmlFor="questrade-token">Refresh token</label>
      <input
        id="questrade-token"
        ref={input}
        type="password"
        name="questrade-token"
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => setHasText(e.target.value.trim() !== "")}
      />
      <div>
        <Button type="submit" variant="primary" disabled={!hasText} busy={save.isPending}>
          Save token
        </Button>
      </div>
      {save.isSuccess && save.data && (
        <p className={`small ${save.data.ok ? "tone-ok" : "tone-bad"}`} role="status">
          {tokenStatus(save.data)}
        </p>
      )}
      {save.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(save.error)}
        </p>
      )}
    </form>
  );
}
