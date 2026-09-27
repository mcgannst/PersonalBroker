// Where the login page may send Stephen afterwards (P4-T12). Only a same-site path is accepted: it must start
// with a single "/" followed by a character that is neither "/" nor "\", and contain no "\" or control
// character anywhere, also after percent-decoding. React Router 6 has an open-redirect advisory for paths
// with backslashes (browsers read "/\evil.com" as "//evil.com"), so anything else falls back to the
// dashboard. The login page itself is never a target (no loop).
export const DEFAULT_AFTER_LOGIN = "/dashboard";
const MAX_LENGTH = 2048;

// Control characters (C0, DEL and C1).
const CONTROL = /[\u0000-\u001f\u007f-\u009f]/;

function isSafePath(s: string): boolean {
  if (s.length < 2 || s[0] !== "/") return false;
  if (s[1] === "/" || s[1] === "\\") return false;
  if (s.includes("\\") || CONTROL.test(s)) return false;
  return true;
}

export function safeNext(raw: string | null | undefined): string {
  if (!raw || raw.length > MAX_LENGTH) return DEFAULT_AFTER_LOGIN;
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    return DEFAULT_AFTER_LOGIN;
  }
  if (!isSafePath(raw) || !isSafePath(decoded)) return DEFAULT_AFTER_LOGIN;
  const path = raw.split(/[?#]/, 1)[0] ?? "";
  if (path === "/login" || path.startsWith("/login/")) return DEFAULT_AFTER_LOGIN;
  return raw;
}
