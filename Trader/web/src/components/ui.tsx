// Stateless UI primitives shared by every page (P4 plan, task P4-T2). Plain CSS from src/styles.css;
// no component library.
import type { ButtonHTMLAttributes, CSSProperties, ReactNode, TableHTMLAttributes } from "react";

import { isApiError } from "../api/client";

export type Tone = "ok" | "warn" | "bad" | "info" | "muted";

/** The minimum touch-target height (SPEC §12: phone-friendly; P4 Global Constraints: at least 44 px). */
export const MIN_TOUCH_PX = 44;

export function Card({
  title,
  actions,
  children,
  id,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  id?: string;
  className?: string;
}) {
  return (
    <section className={className ? `card ${className}` : "card"} id={id}>
      {(title !== undefined || actions !== undefined) && (
        <header className="card-head">
          {title !== undefined && <h2 className="card-title">{title}</h2>}
          {actions !== undefined && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Badge({ tone, children }: { tone: Tone; children?: ReactNode }) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "danger" | "plain";
  /** While true the button is disabled and marked `aria-busy`. */
  busy?: boolean;
};

export function Button({ variant = "plain", busy = false, disabled, className, style, type, children, ...rest }: ButtonProps) {
  const merged: CSSProperties = { minHeight: MIN_TOUCH_PX, ...style };
  const classes = ["btn", `btn-${variant}`, busy ? "is-busy" : "", className ?? ""].filter(Boolean).join(" ");
  return (
    <button
      {...rest}
      type={type ?? "button"}
      className={classes}
      style={merged}
      disabled={Boolean(disabled) || busy}
      aria-busy={busy ? "true" : undefined}
    >
      {children}
    </button>
  );
}

export function Stat({ label, value, sub }: { label: ReactNode; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub !== undefined && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

/** A `<table>` in a horizontally scrollable wrapper, so wide tables never scroll the page on a phone. */
export function Table({ children, className, ...rest }: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="table-scroll">
      <table {...rest} className={className ? `table ${className}` : "table"}>
        {children}
      </table>
    </div>
  );
}

/** The text an error may show: an `ApiError`'s message (written by the server for Stephen), else a generic line. */
export function errorMessage(error: unknown): string {
  if (isApiError(error) && error.message) return error.message;
  return "Something went wrong.";
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="error-box" role="alert">
      <p className="error-text">{errorMessage(error)}</p>
      {onRetry && (
        <Button variant="plain" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

export function Loading() {
  return (
    <div className="loading" role="status" aria-label="Loading">
      Loading…
    </div>
  );
}

export function Empty({ children }: { children?: ReactNode }) {
  return <p className="empty">{children}</p>;
}

/** A status light (kill switches, token, worker): a coloured dot and its label. */
export function Light({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`light tone-${tone}`} role="status" aria-label={label}>
      <span className="light-dot" aria-hidden="true" />
      <span className="light-label">{label}</span>
    </span>
  );
}
