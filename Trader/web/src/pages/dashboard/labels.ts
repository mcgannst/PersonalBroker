// Small display helpers shared by the Dashboard and Candidates pages (P4-T13).
import type { ProposalOut, SessionPhase, TimelineStatus } from "../../api/types";
import type { Tone } from "../../components/ui";
import { fmtPrice } from "../../lib/format";

const PHASES: Record<SessionPhase, string> = {
  open: "Open",
  pre_market: "Pre-market",
  after_close: "After close",
  closed_day: "Market closed today",
};

export function phaseLabel(phase: string): string {
  return PHASES[phase as SessionPhase] ?? phase;
}

const HEADLINES: Record<string, string> = {
  entry: "ENTRY",
  stop: "PROTECTIVE STOP",
  exit: "EXIT",
  cancel: "CANCEL",
};

/** The card headline for a proposal kind (as in Telegram). */
export function proposalHeadline(kind: string): string {
  return HEADLINES[kind] ?? kind.toUpperCase();
}

function capitalise(s: string): string {
  return s ? s[0]!.toUpperCase() + s.slice(1) : s;
}

/** "Buy stop 21.56", "Sell market", "Buy limit 21.60", "Buy stop 21.56 limit 21.60". */
export function orderLine(p: Pick<ProposalOut, "side" | "order_type" | "stop" | "limit">): string {
  const side = capitalise(p.side);
  switch (p.order_type) {
    case "stop":
      return p.stop ? `${side} stop ${fmtPrice(p.stop)}` : `${side} stop`;
    case "limit":
      return p.limit ? `${side} limit ${fmtPrice(p.limit)}` : `${side} limit`;
    case "stop_limit":
      return `${side} stop ${fmtPrice(p.stop)} limit ${fmtPrice(p.limit)}`;
    case "market":
      return `${side} market`;
    default:
      return `${side} ${p.order_type.replace(/_/g, " ")}`;
  }
}

export interface StatusLook {
  text: string;
  tone: Tone | null;
}

const TIMELINE: Record<TimelineStatus, StatusLook> = {
  done: { text: "✓", tone: "ok" },
  failed: { text: "✗ failed", tone: "bad" },
  missed: { text: "missed", tone: "bad" },
  running: { text: "running", tone: "info" },
  skipped: { text: "skipped", tone: "muted" },
  next: { text: "next", tone: "info" },
  upcoming: { text: "", tone: null },
};

export function timelineLook(status: string): StatusLook {
  return TIMELINE[status as TimelineStatus] ?? { text: status, tone: null };
}

/** The tone of an event level. */
export function levelTone(level: string): Tone {
  switch (level.toLowerCase()) {
    case "critical":
    case "error":
      return "bad";
    case "warning":
    case "warn":
      return "warn";
    case "info":
      return "info";
    default:
      return "muted";
  }
}

/**
 * A headline URL is scraped third-party data: it becomes a link only when it starts with `http://` or
 * `https://` (anything else, such as `javascript:`, is shown as plain text). Returns the URL or null.
 */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (typeof url !== "string") return null;
  return /^https?:\/\//i.test(url) ? url : null;
}

/** A fraction shown as an unsigned percentage (drawdown): "0.0138" → "1.38%". */
export function unsignedPct(formatted: string): string {
  return formatted.replace(/^[+-]/, "");
}
