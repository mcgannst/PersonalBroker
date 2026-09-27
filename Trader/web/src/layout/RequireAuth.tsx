// Gate for every page except /login (P4-T12): a logged-out visitor goes to /login?next=<path and query>,
// so Telegram deep links (/dashboard?proposal=12, /trades?position=3, ...) open after logging in.
import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";

import { ErrorBox, Loading } from "../components/ui";
import { useAuth } from "./AuthContext";

export function loginPathFor(pathAndQuery: string): string {
  return `/login?next=${encodeURIComponent(pathAndQuery)}`;
}

export function RequireAuth({ children }: { children?: ReactNode }) {
  const auth = useAuth();
  const location = useLocation();
  if (auth.status === "loading") {
    return (
      <main className="page">
        <Loading />
      </main>
    );
  }
  if (auth.status === "error") {
    return (
      <main className="page">
        <ErrorBox error={auth.error} onRetry={() => void auth.refresh()} />
      </main>
    );
  }
  if (auth.status === "out" || !auth.user) {
    return <Navigate to={loginPathFor(`${location.pathname}${location.search}`)} replace />;
  }
  return children !== undefined ? <>{children}</> : <Outlet />;
}
