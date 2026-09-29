// The shared panel frame of the Dashboard and Control pages (DB-T1, final: DB-T7/T8/T9 build on it and never
// edit it). A labelled region with a heading; its own error state with a 44 px Retry (plan S15) and a plain-text
// empty state. Styled with the theme tokens only (flat, 1 px border, no shadow).
import { Children, type CSSProperties, type ReactNode } from "react";

import { Button } from "../../components/ui";

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
  children?: ReactNode;
}

const FRAME: CSSProperties = {
  background: "var(--surface)",
  border: "var(--panel-border)",
  borderRadius: "var(--panel-radius)",
  padding: "var(--space-3)",
  minWidth: 0,
};

const HEAD: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "var(--space-2)",
  flexWrap: "wrap",
  marginBottom: "var(--space-2)",
};

const TITLE: CSSProperties = { margin: 0, fontSize: "0.95rem", fontWeight: 600, color: "var(--text)" };

const MUTED: CSSProperties = { margin: 0, color: "var(--text-muted)", overflowWrap: "anywhere" };

export function Panel({ title, ariaLabel, error, onRetry, empty, badge, children }: PanelProps) {
  const hasChildren = Children.toArray(children).length > 0;
  let body: ReactNode;
  if (error) {
    body = (
      <div className="panel-error" role="alert" style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)", alignItems: "flex-start" }}>
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
    <section className="panel" aria-label={ariaLabel ?? title} style={FRAME}>
      <header className="panel-head" style={HEAD}>
        <h2 className="panel-title" style={TITLE}>
          {title}
        </h2>
        {badge}
      </header>
      <div className="panel-body">{body}</div>
    </section>
  );
}
