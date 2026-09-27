// Paste a new Questrade refresh token (P4-T9 `POST /api/credentials/questrade`).
//
// The token is a secret: the input is uncontrolled (the value lives only in the DOM input, never in React
// state), it is read and cleared in the same step as the submit, and the call is made directly rather than
// through `useMutation`, whose state would keep the token as the mutation's `variables`. Only the resulting
// `TokenOut` status (which never contains the token) or the server's message is kept.
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { TokenOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";

type Outcome = { kind: "ok"; token: TokenOut } | { kind: "error"; message: string } | null;

export function TokenPaste() {
  const api = useApi();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const el = input.current;
    if (!el || busy) return;
    const token = el.value.trim();
    el.value = "";
    if (!token) return;
    setBusy(true);
    setOutcome(null);
    try {
      const result = await api.putQuestradeToken(token);
      setOutcome({ kind: "ok", token: result });
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
    } catch (error) {
      setOutcome({ kind: "error", message: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={(e) => void submit(e)}>
      <label htmlFor="questrade-token">New refresh token</label>
      <input
        id="questrade-token"
        ref={input}
        type="password"
        name="questrade-token"
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        style={{ width: "100%", maxWidth: "32rem", minHeight: 44 }}
      />
      <p className="small muted">Generate a new manual token for the app in Questrade&apos;s API centre, then paste it here.</p>
      <div>
        <Button type="submit" variant="primary" busy={busy}>
          Save token
        </Button>
      </div>
      {outcome?.kind === "ok" &&
        (outcome.token.ok ? (
          <p className="small tone-ok">Token saved and working.</p>
        ) : (
          <p className="small tone-bad" role="alert">
            {outcome.token.error ?? "The token was saved but is not working."}
          </p>
        ))}
      {outcome?.kind === "error" && (
        <p className="small tone-bad" role="alert">
          {outcome.message}
        </p>
      )}
    </form>
  );
}
