// Links that come from the API (activity items, rejection rules, positions, strategy settings) go through
// here (DB-GWEB fix round 1): only a same-site path ("/reports?day=...") becomes a link; anything else
// (`javascript:`, `//host`, `/\host`, `https://...`, blank) renders as plain text.
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** The link when it is a same-site path, else null. */
export function safeLink(link: string | null | undefined): string | null {
  if (typeof link !== "string") return null;
  if (!link.startsWith("/") || link.startsWith("//") || link.startsWith("/\\")) return null;
  // no control characters or whitespace a browser would strip before resolving the URL
  if (/[\u0000-\u001f\u007f\s]/.test(link)) return null;
  return link;
}

/**
 * A router link for a safe `to`; otherwise its children as plain text in an `<a>` WITHOUT `href` (HTML's
 * placeholder link: not focusable, not clickable, no URL at all), keeping the layout and class.
 */
export function SafeLink({ to, className, children, ...rest }: { to: string | null | undefined; className?: string; children: ReactNode; "aria-label"?: string }) {
  const href = safeLink(to);
  if (href === null) return <a className={className ? `${className} link-unsafe` : "link-unsafe"}>{children}</a>;
  return (
    <Link className={className} to={href} aria-label={rest["aria-label"]}>
      {children}
    </Link>
  );
}
