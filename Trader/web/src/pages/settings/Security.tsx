// Account security (SPEC §14): change the password (at least 8 characters, typed twice; Stephen's decision
// 2026-09-27) and optional two-step sign-in (TOTP). The TOTP secret is shown only during setup and is
// dropped from state when setup ends or is cancelled.
//
// Passwords and codes are secrets: every input here is uncontrolled (the value lives only in the DOM input,
// never in React state), the values are read and the inputs cleared in the same step as a submit, and the calls
// go through `useSecretCall` rather than `useMutation`, whose cache would keep them as `variables`. A form that
// fails the local checks sends nothing and keeps what was typed, so it can be corrected.
//
// Wrong credentials: the server answers a wrong current password or two-step code with 403 `bad_credentials`
// or 422 (never 401, which the HTTP client treats as an expired session), and its message is shown here.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent, type RefObject } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { OkOut, PasswordChangeIn, TotpSetupOut } from "../../api/types";
import { Button, ErrorBox, Loading, errorMessage } from "../../components/ui";
import { useSecretCall } from "./useSecretCall";

export const MIN_PASSWORD_CHARS = 8;
const CODE_RE = /^\d{6}$/;

/** Why a new password cannot be sent, or null when it can. */
export function passwordProblem(current: string, next: string, again: string): string | null {
  if (!current) return "Enter your current password.";
  if (next !== again) return "The new passwords don't match.";
  if (next.length < MIN_PASSWORD_CHARS) return `The new password needs at least ${MIN_PASSWORD_CHARS} characters.`;
  return null;
}

/** An input's current value ("" when it is not mounted). */
function valueOf(ref: RefObject<HTMLInputElement>): string {
  return ref.current?.value ?? "";
}

/** Empties the given inputs. */
function clearInputs(...refs: RefObject<HTMLInputElement>[]): void {
  for (const ref of refs) if (ref.current) ref.current.value = "";
}

function ChangePassword({ totpEnabled }: { totpEnabled: boolean }) {
  const api = useApi();
  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  const againRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const change = useSecretCall<OkOut>();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (change.isPending) return;
    change.reset();
    const current = valueOf(currentRef);
    const next = valueOf(nextRef);
    const p = passwordProblem(current, next, valueOf(againRef));
    setProblem(p);
    if (p) return;
    const code = valueOf(codeRef).trim();
    const body: PasswordChangeIn = { current_password: current, new_password: next };
    if (totpEnabled && code) body.totp = code;
    clearInputs(currentRef, nextRef, againRef, codeRef);
    void change.run(() => api.changePassword(body));
  };

  return (
    <form className="stack" aria-label="Change password" onSubmit={submit}>
      <h3>Password</h3>
      <label htmlFor="pw-current">Current password</label>
      <input id="pw-current" ref={currentRef} type="password" autoComplete="current-password" />
      <label htmlFor="pw-new">New password</label>
      <input id="pw-new" ref={nextRef} type="password" autoComplete="new-password" />
      <label htmlFor="pw-again">New password again</label>
      <input id="pw-again" ref={againRef} type="password" autoComplete="new-password" />
      {totpEnabled && (
        <>
          <label htmlFor="pw-code">Two-step code</label>
          <input id="pw-code" ref={codeRef} inputMode="numeric" autoComplete="one-time-code" maxLength={6} />
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
  const passwordRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [hasPassword, setHasPassword] = useState(false);
  const [codeReady, setCodeReady] = useState(false);
  // `start.data` is the setup answer (the secret to show); it is forgotten by `end()`.
  const start = useSecretCall<TotpSetupOut>();
  const confirm = useSecretCall<OkOut>();

  function end() {
    // Forget the secret and everything typed.
    clearInputs(passwordRef, codeRef);
    setHasPassword(false);
    setCodeReady(false);
    setOpen(false);
    start.reset();
    confirm.reset();
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

  const setup = start.isSuccess ? start.data : undefined;
  if (!setup) {
    return (
      <form
        className="stack"
        aria-label="Set up two-step"
        onSubmit={(e) => {
          e.preventDefault();
          const pw = valueOf(passwordRef);
          clearInputs(passwordRef);
          setHasPassword(false);
          if (pw) void start.run(() => api.totpSetup({ password: pw }));
        }}
      >
        <label htmlFor="totp-setup-password">Password</label>
        <input
          id="totp-setup-password"
          ref={passwordRef}
          type="password"
          autoComplete="current-password"
          onChange={(e) => setHasPassword(e.target.value !== "")}
        />
        <div className="row">
          <Button type="submit" variant="primary" disabled={!hasPassword} busy={start.isPending}>
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
        const code = valueOf(codeRef).trim();
        if (!CODE_RE.test(code)) return;
        clearInputs(codeRef);
        setCodeReady(false);
        void confirm.run(() => api.totpConfirm({ code })).then((ok) => {
          if (ok === undefined) return;
          end();
          onDone("Two-step sign-in is on.");
          void queryClient.invalidateQueries({ queryKey: qk.me() });
        });
      }}
    >
      <p className="small">Add this key to an authenticator app, then enter the 6-digit code it shows.</p>
      <p>
        Key: <code style={{ wordBreak: "break-all" }}>{setup.secret}</code>
      </p>
      {linkOk && (
        <p>
          <a className="link-touch" href={setup.otpauth_uri}>
            Open in an authenticator app
          </a>
        </p>
      )}
      <label htmlFor="totp-confirm-code">Code from the app</label>
      <input
        id="totp-confirm-code"
        ref={codeRef}
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        onChange={(e) => setCodeReady(CODE_RE.test(e.target.value.trim()))}
      />
      <div className="row">
        <Button type="submit" variant="primary" disabled={!codeReady} busy={confirm.isPending}>
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
  const passwordRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const [hasPassword, setHasPassword] = useState(false);
  const [codeReady, setCodeReady] = useState(false);
  const disable = useSecretCall<OkOut>();
  const ready = hasPassword && codeReady;
  return (
    <form
      className="stack"
      aria-label="Turn off two-step"
      onSubmit={(e) => {
        e.preventDefault();
        const password = valueOf(passwordRef);
        const code = valueOf(codeRef).trim();
        if (!password || !CODE_RE.test(code)) return;
        clearInputs(passwordRef, codeRef);
        setHasPassword(false);
        setCodeReady(false);
        onDone("");
        void disable.run(() => api.totpDisable({ password, code })).then((ok) => {
          if (ok === undefined) return;
          onDone("Two-step sign-in is off.");
          void queryClient.invalidateQueries({ queryKey: qk.me() });
        });
      }}
    >
      <label htmlFor="totp-off-password">Password</label>
      <input
        id="totp-off-password"
        ref={passwordRef}
        type="password"
        autoComplete="current-password"
        onChange={(e) => setHasPassword(e.target.value !== "")}
      />
      <label htmlFor="totp-off-code">Code from the app</label>
      <input
        id="totp-off-code"
        ref={codeRef}
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={6}
        onChange={(e) => setCodeReady(CODE_RE.test(e.target.value.trim()))}
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
