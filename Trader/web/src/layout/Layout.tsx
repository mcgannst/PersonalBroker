// The app frame for logged-in pages (P4-T12; live dashboard plan S12, DB-T11): a header ("Trader", a DEV badge
// on dev, the live dot, the theme control and Logout), a side list of pages on wide screens (Dashboard,
// Control, Reports, Replay, Settings, then a "More" group: Trades, Candidates, Performance, Journal) and a
// bottom tab bar on phones (Dashboard, Control, Reports, More -> Replay, Settings, Trades, Candidates,
// Performance, Journal), so every page is at most two taps away. It also opens the live-update stream and
// applies the server's time-zone check.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import { Badge, Button } from "../components/ui";
import { LiveUpdatesProvider, useLiveUpdates } from "../live/useLiveUpdates";
import { THEME_STORAGE_KEY, type ThemeChoice } from "../theme/tokens";
import { useAuth } from "./AuthContext";
import { applyServerMeta } from "./serverTime";
import "./layout.css";

interface NavItem {
  to: string;
  label: string;
}

/** The primary pages (design D6), in order. */
export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/control", label: "Control" },
  { to: "/reports", label: "Reports" },
  { to: "/replay", label: "Replay" },
  { to: "/settings", label: "Settings" },
];

/** The other pages: their routes and deep links (Telegram's /trades?position=, /journal?date=) are unchanged. */
export const MORE_ITEMS: readonly NavItem[] = [
  { to: "/trades", label: "Trades" },
  { to: "/candidates", label: "Candidates" },
  { to: "/performance", label: "Performance" },
  { to: "/journal", label: "Journal" },
];

const TAB_PATHS = new Set(["/dashboard", "/control", "/reports"]);
/** The phone's tabs (then More). */
export const TAB_ITEMS: readonly NavItem[] = NAV_ITEMS.filter((i) => TAB_PATHS.has(i.to));
/** The phone's More menu: the primary pages without a tab, then the other pages. */
export const TAB_MORE_ITEMS: readonly NavItem[] = [...NAV_ITEMS.filter((i) => !TAB_PATHS.has(i.to)), ...MORE_ITEMS];

/** How often the server's meta (clock, zone) is re-read while the app is open. */
const META_REFRESH_MS = 30 * 60_000;

export const THEME_CHOICES: readonly { value: ThemeChoice; label: string }[] = [
  { value: "auto", label: "Auto" },
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];
/** Dark slate unless the browser remembers another choice (open question 1). */
export const DEFAULT_THEME: ThemeChoice = "dark";

function isThemeChoice(value: unknown): value is ThemeChoice {
  return value === "auto" || value === "dark" || value === "light";
}

/** The theme this browser remembers, else dark (storage may be missing or blocked). */
export function readThemeChoice(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeChoice(stored) ? stored : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** Applies a theme to `<html>` (tokens.css selects the palette by `data-theme`). */
export function applyTheme(choice: ThemeChoice): void {
  if (typeof document !== "undefined") document.documentElement.setAttribute("data-theme", choice);
}

function storeThemeChoice(choice: ThemeChoice): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // storage blocked: the choice lasts for this page only
  }
}

function ThemeSwitch() {
  const [choice, setChoice] = useState<ThemeChoice>(readThemeChoice);

  useEffect(() => applyTheme(choice), [choice]);

  const onChange = (e: ChangeEvent<HTMLSelectElement>) => {
    const next = e.target.value;
    if (!isThemeChoice(next)) return;
    storeThemeChoice(next);
    setChoice(next);
  };

  return (
    <select className="theme-select" aria-label="Theme" value={choice} onChange={onChange}>
      {THEME_CHOICES.map((t) => (
        <option key={t.value} value={t.value}>
          {t.label}
        </option>
      ))}
    </select>
  );
}

const navClass = ({ isActive }: { isActive: boolean }) => (isActive ? "nav-link is-active" : "nav-link");

/** The header's live dot, in the status colours (design D9: sky when live, slate while reconnecting; never
 * the money green). The shared Light's markup with a `status-*` class. */
function LiveDot() {
  const { connected } = useLiveUpdates();
  const label = connected ? "Live" : "Reconnecting";
  return (
    <span className={`light ${connected ? "status-ok" : "status-muted"}`} role="status" aria-label={label}>
      <span className="light-dot" aria-hidden="true" />
      <span className="light-label">{label}</span>
    </span>
  );
}

function MoreMenu() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const ref = useRef<HTMLDivElement>(null);
  const moreActive = TAB_MORE_ITEMS.some((i) => location.pathname.startsWith(i.to));

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
          {TAB_MORE_ITEMS.map((item) => (
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
          <ThemeSwitch />
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
          <h2 className="side-nav-heading" id="side-nav-more">
            More
          </h2>
          <div className="side-nav-group" role="group" aria-labelledby="side-nav-more">
            {MORE_ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to} className={navClass}>
                {item.label}
              </NavLink>
            ))}
          </div>
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
