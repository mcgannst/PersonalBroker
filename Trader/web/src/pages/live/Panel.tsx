// The shared panel frame of the Dashboard and Control pages (DB-T1). A labelled region with a heading; its own
// error state with a 44 px Retry (plan S15) and a plain-text empty state. Styled with the theme tokens only
// (flat, 1 px border, no shadow). DB-DENSE: a `dense` density (from `PanelDensity`, set by the Dashboard) gives
// smaller padding and a small-caps heading; `className` lets a page lay a panel out (the Control page keeps the
// default density and has no class).
import { Children, createContext, useContext, type CSSProperties, type ReactNode } from "react";

import { Button } from "../../components/ui";

export type PanelDensityValue = "normal" | "dense";

/** The density of every Panel below it (the Dashboard provides "dense"). */
export const PanelDensity = createContext<PanelDensityValue>("normal");

export interface PanelProps {
  title: string;
  /** The region's accessible name; the title by default. */
  ariaLabel?: string;
  /** When set, the panel shows this text (and Retry when `onRetry` is given) instead of its children. */
  error?: string | null;
  onRetry?: () => void;
  /** Shown as plain text when there are no children. */
  empty?: ReactNode | null;
  /** Shown beside the title (a "prices stale" badge, a count...). */
  badge?: ReactNode;
  /** Extra classes on the region (layout hooks for a page). */
  className?: string;
  children?: ReactNode;
}

const FRAME: CSSProperties = {
  background: "var(--surface)",
  border: "var(--panel-border)",
  borderRadius: "var(--panel-radius)",
  padding: "var(--space-3)",
  minWidth: 0,
};

const FRAME_DENSE: CSSProperties = { ...FRAME, padding: "var(--space-2)" };

const HEAD: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "var(--space-2)",
  flexWrap: "wrap",
  marginBottom: "var(--space-2)",
};

const HEAD_DENSE: CSSProperties = { ...HEAD, gap: "var(--space-1) var(--space-2)", marginBottom: "var(--space-1)" };

const TITLE: CSSProperties = { margin: 0, fontSize: "0.95rem", fontWeight: 600, color: "var(--text)" };

const TITLE_DENSE: CSSProperties = {
  margin: 0,
  fontSize: "0.72rem",
  fontWeight: 600,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  color: "var(--text-muted)",
};

const MUTED: CSSProperties = { margin: 0, color: "var(--text-muted)", overflowWrap: "anywhere" };

export function Panel({ title, ariaLabel, error, onRetry, empty, badge, className, children }: PanelProps) {
  const dense = useContext(PanelDensity) === "dense";
  const hasChildren = Children.toArray(children).length > 0;
  let body: ReactNode;
  if (error) {
    body = (
      <div className="panel-error" role="alert" style={{ display: "flex", flexDirection: dense ? "row" : "column", flexWrap: "wrap", gap: "var(--space-2)", alignItems: dense ? "center" : "flex-start" }}>
        <p style={MUTED}>{error}</p>
        {onRetry && (
          <Button variant="plain" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    );
  } else if (!hasChildren && empty !== undefined && empty !== null) {
    body = (
      <p className="panel-empty" style={MUTED}>
        {empty}
      </p>
    );
  } else {
    body = children;
  }
  return (
    <section className={["panel", className].filter(Boolean).join(" ")} aria-label={ariaLabel ?? title} style={dense ? FRAME_DENSE : FRAME}>
      <header className="panel-head" style={dense ? HEAD_DENSE : HEAD}>
        <h2 className="panel-title" style={dense ? TITLE_DENSE : TITLE}>
          {title}
        </h2>
        {badge}
      </header>
      <div className="panel-body">{body}</div>
    </section>
  );
}
