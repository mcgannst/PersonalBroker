// Paste a Questrade refresh token (SPEC §4.1). The input is a password field, never echoed back and cleared
// as soon as it is sent; the page shows the resulting token status or the server's message.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { TokenOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";

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
  const [token, setToken] = useState("");
  const save = useMutation({
    mutationFn: (value: string) => api.putQuestradeToken(value),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const value = token.trim();
    setToken(""); // never keep the token in the page once it is sent
    if (value) save.mutate(value);
  };

  return (
    <form className="stack" onSubmit={submit} aria-label="Questrade token">
      <p className="small">
        Paste a new refresh token generated in Questrade's API centre. It is checked at once and never shown again.
      </p>
      <label htmlFor="questrade-token">Refresh token</label>
      <input
        id="questrade-token"
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={token}
        onChange={(e) => setToken(e.target.value)}
      />
      <div>
        <Button type="submit" variant="primary" disabled={token.trim() === ""} busy={save.isPending}>
          Save token
        </Button>
      </div>
      {save.isSuccess && (
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
