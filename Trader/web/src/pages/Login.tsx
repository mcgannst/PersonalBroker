// The login page (P4-T12, SPEC §14). Username, password and an optional two-step code. The server's
// message is shown on a failure (wrong password 401, too many attempts 429); the password and code are
// cleared from state as soon as the form is sent. On success it goes to `next` when that is a same-site
// path (see `safeNext`), else the dashboard.
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import type { LoginIn } from "../api/types";
import { Button, Card, ErrorBox, Loading } from "../components/ui";
import { useOptionalAuth } from "../layout/AuthContext";
import { safeNext } from "../layout/safeNext";
import "../layout/layout.css";

export default function LoginPage() {
  const api = useApi();
  const auth = useOptionalAuth();
  const status = auth?.status ?? "out";
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (status === "loading") {
    return (
      <main className="page login-page">
        <Loading />
      </main>
    );
  }
  if (status === "in") return <Navigate to={next} replace />;

  const onSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const body: LoginIn = { username: username.trim(), password };
    const totp = code.trim();
    if (totp) body.totp = totp;
    // Never keep the secrets in state after sending them.
    setPassword("");
    setCode("");
    setError(null);
    setBusy(true);
    try {
      const session = await api.login(body);
      auth?.setSession(session);
      navigate(next, { replace: true });
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };

  return (
    <main className="page login-page">
      <h1>Login</h1>
      <Card>
        <form className="login-form" onSubmit={(e) => void onSubmit(e)} noValidate>
          <label>
            Username
            <input
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <label>
            Code (if two-step is on)
            <input
              name="totp"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </label>
          {error !== null && <ErrorBox error={error} />}
          <Button type="submit" variant="primary" busy={busy} disabled={!username.trim() || !password}>
            Log in
          </Button>
        </form>
      </Card>
    </main>
  );
}
