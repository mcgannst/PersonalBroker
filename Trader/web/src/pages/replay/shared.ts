// Small helpers shared by the Replay page's components (P5-T8): status badges, the default date range, exact
// decimal differences for the comparison, and the 422 field paths of `POST /api/replays`.
import { isApiError } from "../../api/client";
import type { IsoDate, ReplayStatus } from "../../api/types";
import type { Tone } from "../../components/ui";
import { addDays, isIsoDate } from "../performance/dates";

/** The badge tone of each replay status (queued and running info, completed ok, failed bad, cancelled muted). */
export const STATUS_TONE: Record<ReplayStatus, Tone> = {
  queued: "info",
  running: "info",
  completed: "ok",
  failed: "bad",
  cancelled: "muted",
};

/** True while a replay may still change (the detail polls while the live stream is down). */
export function isActive(status: ReplayStatus): boolean {
  return status === "queued" || status === "running";
}

/** The number of weekdays in the default range (the web has no session calendar; the server counts sessions). */
export const DEFAULT_RANGE_WEEKDAYS = 20;

/**
 * The default range: the `DEFAULT_RANGE_WEEKDAYS` weekdays ending at `latest` (the last allowed session).
 * Holidays are not known here, so the range may hold a session or two fewer; the server validates it.
 */
export function defaultRange(latest: IsoDate): { from: IsoDate; to: IsoDate } {
  if (!isIsoDate(latest)) return { from: latest, to: latest };
  let day = latest;
  // Step back to a weekday first (latest_allowed is a session, so this is a no-op in practice).
  while (isWeekend(day)) day = addDays(day, -1);
  const to = day;
  let counted = 1;
  while (counted < DEFAULT_RANGE_WEEKDAYS) {
    day = addDays(day, -1);
    if (!isWeekend(day)) counted += 1;
  }
  return { from: day, to };
}

function isWeekend(date: IsoDate): boolean {
  const dow = new Date(`${date}T00:00:00Z`).getUTCDay();
  return dow === 0 || dow === 6;
}

// ---------------------------------------------------------------- exact decimal difference

const DEC_RE = /^\s*([+-])?(\d*)(?:\.(\d*))?\s*$/;

function parse(value: string): { v: bigint; scale: number } | null {
  const m = DEC_RE.exec(value);
  if (!m) return null;
  const int = m[2] ?? "";
  const frac = m[3] ?? "";
  if (int === "" && frac === "") return null;
  const digits = BigInt((int + frac).replace(/^0+(?=\d)/, "") || "0");
  return { v: m[1] === "-" ? -digits : digits, scale: frac.length };
}

/**
 * `a - b` of two decimal strings, exactly (BigInt, never floating point), as a decimal string with the larger
 * of the two scales; null when either is null or not a plain decimal.
 */
export function decimalDiff(a: string | null | undefined, b: string | null | undefined): string | null {
  if (a === null || a === undefined || b === null || b === undefined) return null;
  const x = parse(a);
  const y = parse(b);
  if (!x || !y) return null;
  const scale = Math.max(x.scale, y.scale);
  const d = x.v * 10n ** BigInt(scale - x.scale) - y.v * 10n ** BigInt(scale - y.scale);
  const neg = d < 0n;
  const s = (neg ? -d : d).toString().padStart(scale + 1, "0");
  const body = scale === 0 ? s : `${s.slice(0, s.length - scale)}.${s.slice(s.length - scale)}`;
  return neg ? `-${body}` : body;
}

// ---------------------------------------------------------------- 422 field paths

/**
 * The field messages of a failed start, keyed by their path after `body` joined with dots (`date_to`,
 * `overrides.risk_pct`, `overrides.fees.commission`, `strategies.orb_sip.params.top_n`). The server's `loc`
 * is `["body", ...path.split(".")]` (P5-T7). Messages without a location are under "".
 */
export function fieldPaths(error: unknown): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  if (!isApiError(error) || !error.fields) return out;
  for (const fe of error.fields) {
    const parts = fe.loc.map(String);
    const path = (parts[0] === "body" ? parts.slice(1) : parts).join(".");
    (out[path] ??= []).push(fe.msg);
  }
  return out;
}
