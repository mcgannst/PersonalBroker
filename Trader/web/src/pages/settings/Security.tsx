// Account security (SPEC §14): change the password (at least 8 characters, typed twice; Stephen's decision
// 2026-09-27) and optional two-step sign-in (TOTP). The TOTP secret is shown only during setup and is
// dropped from state when setup ends or is cancelled. Password fields are cleared once sent.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { PasswordChangeIn, TotpSetupOut } from "../../api/types";
import { Button, ErrorBox, Loading, errorMessage } from "../../components/ui";

export const MIN_PASSWORD_CHARS = 8;
const CODE_RE = /^\d{6}$/;

/** Why a new password cannot be sent, or null when it can. */
export function passwordProblem(current: string, next: string, again: string): string | null {
  if (!current) return "Enter your current password.";
  if (next !== again) return "The new passwords don't match.";
  if (next.length < MIN_PASSWORD_CHARS) return `The new password needs at least ${MIN_PASSWORD_CHARS} characters.`;
  return null;
}

function ChangePassword({ totpEnabled }: { totpEnabled: boolean }) {
  const api = useApi();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [code, setCode] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const change = useMutation({ mutationFn: (body: PasswordChangeIn) => api.changePassword(body) });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    change.reset();
    const p = passwordProblem(current, next, again);
    setProblem(p);
    if (p) return;
    const body: PasswordChangeIn = { current_password: current, new_password: next };
    if (totpEnabled && code.trim()) body.totp = code.trim();
    setCurrent("");
    setNext("");
    setAgain("");
    setCode("");
    change.mutate(body);
  };

  return (
    <form className="stack" aria-label="Change password" onSubmit={submit}>
      <h3>Password</h3>
      <label htmlFor="pw-current">Current password</label>
      <input id="pw-current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} />
      <label htmlFor="pw-new">New password</label>
      <input id="pw-new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
      <label htmlFor="pw-again">New password again</label>
      <input id="pw-again" type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
      {totpEnabled && (
        <>
          <label htmlFor="pw-code">Two-step code</label>
          <input id="pw-code" inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} />
        </>
      )}
      <p className="small muted">At least {MIN_PASSWORD_CHARS} characters. Your other sessions are signed out.</p>
      <div>
        <Button type="submit" variant="primary" busy={change.isPending}>
          Change password
        </Button>
      </div>
      {problem && (
        <p className="small tone-bad" role="alert">
          {problem}
        </p>
      )}
      {change.isSuccess && (
        <p className="small tone-ok" role="status">
          Password changed. Your other sessions were signed out.
        </p>
      )}
      {change.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(change.error)}
        </p>
      )}
    </form>
  );
}

function TotpSetup({ onDone }: { onDone: (message: string) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [setup, setSetup] = useState<TotpSetupOut | null>(null);
  const [code, setCode] = useState("");

  const start = useMutation({
    mutationFn: (pw: string) => api.totpSetup({ password: pw }),
    onSuccess: (out) => setSetup(out),
  });
  const confirm = useMutation({
    mutationFn: (c: string) => api.totpConfirm({ code: c }),
    onSuccess: () => {
      end();
      onDone("Two-step sign-in is on.");
      void queryClient.invalidateQueries({ queryKey: qk.me() });
    },
  });

  function end() {
    // Forget the secret and everything typed.
    setSetup(null);
    setPassword("");
    setCode("");
    setOpen(false);
    start.reset();
  }

  if (!open) {
    return (
      <div>
        <Button
          variant="plain"
          onClick={() => {
            onDone("");
            setOpen(true);
          }}
        >
          Set up two-step
        </Button>
      </div>
    );
  }

  if (!setup) {
    return (
      <form
        className="stack"
        aria-label="Set up two-step"
        onSubmit={(e) => {
          e.preventDefault();
          const pw = password;
          setPassword("");
          if (pw) start.mutate(pw);
        }}
      >
        <label htmlFor="totp-setup-password">Password</label>
        <input id="totp-setup-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <div className="row">
          <Button type="submit" variant="primary" disabled={!password} busy={start.isPending}>
            Continue
          </Button>
          <Button variant="plain" onClick={end}>
            Cancel
          </Button>
        </div>
        {start.isError && (
          <p className="small tone-bad" role="alert">
            {errorMessage(start.error)}
          </p>
        )}
      </form>
    );
  }

  const linkOk = setup.otpauth_uri.startsWith("otpauth://");
  return (
    <form
      className="stack"
      aria-label="Confirm two-step"
      onSubmit={(e) => {
        e.preventDefault();
        if (CODE_RE.test(code)) confirm.mutate(code);
      }}
    >
      <p className="small">Add this key to an authenticator app, then enter the 6-digit code it shows.</p>
      <p>
        Key: <code style={{ wordBreak: "break-all" }}>{setup.secret}</code>
      </p>
      {linkOk && (
        <p>
          <a href={setup.otpauth_uri}>Open in an authenticator app</a>
        </p>
      )}
      <label htmlFor="totp-confirm-code">Code from the app</label>
      <input
        id="totp-confirm-code"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={code}
        onChange={(e) => setCode(e.target.value.trim())}
      />
      <div className="row">
        <Button type="submit" variant="primary" disabled={!CODE_RE.test(code)} busy={confirm.isPending}>
          Confirm
        </Button>
        <Button variant="plain" disabled={confirm.isPending} onClick={end}>
          Cancel
        </Button>
      </div>
      {confirm.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(confirm.error)}
        </p>
      )}
    </form>
  );
}

function TotpDisable({ onDone }: { onDone: (message: string) => void }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const disable = useMutation({
    mutationFn: (body: { password: string; code: string }) => api.totpDisable(body),
    onSuccess: () => {
      onDone("Two-step sign-in is off.");
      void queryClient.invalidateQueries({ queryKey: qk.me() });
    },
  });
  const ready = password !== "" && CODE_RE.test(code);
  return (
    <form
      className="stack"
      aria-label="Turn off two-step"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ready) return;
        const body = { password, code };
        setPassword("");
        setCode("");
        onDone("");
        disable.mutate(body);
      }}
    >
      <label htmlFor="totp-off-password">Password</label>
      <input id="totp-off-password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <label htmlFor="totp-off-code">Code from the app</label>
      <input
        id="totp-off-code"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        value={code}
        onChange={(e) => setCode(e.target.value.trim())}
      />
      <div>
        <Button type="submit" variant="danger" disabled={!ready} busy={disable.isPending}>
          Turn off two-step
        </Button>
      </div>
      {disable.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(disable.error)}
        </p>
      )}
    </form>
  );
}

export function Security() {
  const api = useApi();
  const me = useQuery({ queryKey: qk.me(), queryFn: () => api.me() });
  const [totpNotice, setTotpNotice] = useState("");
  if (me.isPending) return <Loading />;
  if (me.isError) return <ErrorBox error={me.error} onRetry={() => void me.refetch()} />;
  const totpEnabled = me.data.user.totp_enabled;
  return (
    <div className="stack">
      <p className="small muted">Signed in as {me.data.user.username}.</p>
      <ChangePassword totpEnabled={totpEnabled} />
      <div className="stack">
        <h3>Two-step sign-in</h3>
        <p className="small">{totpEnabled ? "On: sign-in asks for a code from your authenticator app." : "Off: sign-in needs only your password."}</p>
        {totpEnabled ? <TotpDisable onDone={setTotpNotice} /> : <TotpSetup onDone={setTotpNotice} />}
        {totpNotice && (
          <p className="small tone-ok" role="status">
            {totpNotice}
          </p>
        )}
      </div>
    </div>
  );
}
