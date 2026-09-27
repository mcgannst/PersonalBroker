// The app frame for logged-in pages (P4-T12): a header ("Trader", a DEV badge on dev, the live dot and
// Logout), a side list of pages on wide screens and a bottom tab bar on phones (Dashboard, Candidates,
// Trades, Journal, More -> Performance, Reports, Settings, System), so every page is at most two taps away.
// It also opens the live-update stream and applies the server's time-zone check.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button, Light } from "../components/ui";
import { LiveUpdatesProvider, useLiveUpdates } from "../live/useLiveUpdates";
import { useAuth } from "./AuthContext";
import { applyServerMeta } from "./serverTime";
import "./layout.css";

interface NavItem {
  to: string;
  label: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/candidates", label: "Candidates" },
  { to: "/trades", label: "Trades" },
  { to: "/performance", label: "Performance" },
  { to: "/journal", label: "Journal" },
  { to: "/reports", label: "Reports" },
  { to: "/replay", label: "Replay" },
  { to: "/settings", label: "Settings" },
  { to: "/system", label: "System" },
];

const TAB_PATHS = new Set(["/dashboard", "/candidates", "/trades", "/journal"]);
const TAB_ITEMS = NAV_ITEMS.filter((i) => TAB_PATHS.has(i.to));
const MORE_ITEMS = NAV_ITEMS.filter((i) => !TAB_PATHS.has(i.to));

/** How often the server's meta (clock, zone) is re-read while the app is open. */
const META_REFRESH_MS = 30 * 60_000;

const navClass = ({ isActive }: { isActive: boolean }) => (isActive ? "nav-link is-active" : "nav-link");

function LiveDot() {
  const { connected } = useLiveUpdates();
  return <Light tone={connected ? "ok" : "muted"} label={connected ? "Live" : "Reconnecting"} />;
}

function MoreMenu() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const ref = useRef<HTMLDivElement>(null);
  const moreActive = MORE_ITEMS.some((i) => location.pathname.startsWith(i.to));

  useEffect(() => setOpen(false), [location.pathname, location.search]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  return (
    <div className="tab-more" ref={ref}>
      <button
        type="button"
        className={moreActive ? "nav-link is-active" : "nav-link"}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        More
      </button>
      {open && (
        <div className="tab-menu" role="menu" aria-label="More pages">
          {MORE_ITEMS.map((item) => (
            <Link key={item.to} to={item.to} role="menuitem" className="nav-link">
              {item.label}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function Frame() {
  const api = useApi();
  const auth = useAuth();
  const navigate = useNavigate();
  const [leaving, setLeaving] = useState(false);
  const meta = useQuery({ queryKey: qk.meta(), queryFn: () => api.meta(), staleTime: META_REFRESH_MS, refetchInterval: META_REFRESH_MS });

  useEffect(() => {
    if (meta.data) applyServerMeta(meta.data);
  }, [meta.data]);

  const onLogout = async () => {
    setLeaving(true);
    await auth.logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="shell">
      <header className="shell-header">
        <div className="row shell-brand">
          <Link to="/dashboard" className="shell-title">
            Trader
          </Link>
          {meta.data?.app_env === "dev" && <Badge tone="warn">DEV</Badge>}
        </div>
        <div className="row shell-actions">
          <LiveDot />
          <Button variant="plain" busy={leaving} onClick={() => void onLogout()}>
            Log out
          </Button>
        </div>
      </header>
      <div className="shell-body">
        <nav className="side-nav" aria-label="Main">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} className={navClass}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="shell-main">
          <Outlet />
        </div>
      </div>
      <nav className="tab-bar" aria-label="Tabs">
        {TAB_ITEMS.map((item) => (
          <NavLink key={item.to} to={item.to} className={navClass}>
            {item.label}
          </NavLink>
        ))}
        <MoreMenu />
      </nav>
    </div>
  );
}

export function Layout() {
  const auth = useAuth();
  return (
    <LiveUpdatesProvider onUnauthorized={auth.clear}>
      <Frame />
    </LiveUpdatesProvider>
  );
}

export default Layout;
