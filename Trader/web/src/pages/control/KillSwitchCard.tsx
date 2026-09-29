// The Control page's Kill switches card (live dashboard design §4.2, plan DB-T9): each switch's state, value
// vs threshold and last trip, then the reused KillSwitchPanel for resets (typed reason, confirm, audited by
// the server) and the history. No new engine action.
import type { KillSwitchEventOut, KillSwitchLightOut } from "../../api/types";
import { fmtDateTime, fmtR, fmtRate } from "../../lib/format";
import { Panel } from "../live/Panel";
import { KillSwitchPanel } from "../settings/KillSwitchPanel";
import { PartError, StatusChip, cardError, type ChipTone } from "./parts";

const PAUSE = "manual_pause";

function fmtValue(value: string | null, unit: KillSwitchLightOut["unit"]): string {
  if (value === null) return "n/a";
  return unit === "pct" ? fmtRate(value) : unit === "r" ? fmtR(value) : value;
}

/** "0.00% of 5.00% limit", "+0.71R (limit 0.00R), 1 of 20 trades", or null for a switch without a value. */
export function valueLine(l: KillSwitchLightOut): string | null {
  if (l.unit === "none" || (l.value === null && l.threshold === null)) return null;
  let text = l.unit === "pct" ? `${fmtValue(l.value, l.unit)} of ${fmtValue(l.threshold, l.unit)} limit` : `${fmtValue(l.value, l.unit)} (limit ${fmtValue(l.threshold, l.unit)})`;
  if (l.count !== null && l.count_min !== null) text += `, ${l.count} of ${l.count_min} trades`;
  return text;
}

function stateOf(l: KillSwitchLightOut): { word: string; tone: ChipTone } {
  if (l.switch === PAUSE) return l.tripped ? { word: "On", tone: "warn" } : { word: "Off", tone: "muted" };
  return l.tripped ? { word: "Tripped", tone: "bad" } : { word: "OK", tone: "ok" };
}

/** The trip line: the current trip with its trip value, else the latest trip in the history, else none. */
export function tripLine(l: KillSwitchLightOut, history: readonly KillSwitchEventOut[]): string {
  if (l.tripped) {
    const when = l.tripped_at ? `tripped ${fmtDateTime(l.tripped_at)}` : "tripped";
    const value = l.trip_value ?? l.value;
    const threshold = l.trip_threshold ?? l.threshold;
    if (value !== null && l.unit !== "none") return `${when} at ${fmtValue(value, l.unit)} (limit ${fmtValue(threshold, l.unit)})`;
    return when;
  }
  const last = history
    .filter((h) => h.switch === l.switch)
    .reduce<KillSwitchEventOut | null>((best, h) => (best === null || Date.parse(h.tripped_at) > Date.parse(best.tripped_at) ? h : best), null);
  return last ? `last trip ${fmtDateTime(last.tripped_at)}` : "never tripped";
}

export function KillSwitchCard({
  lights,
  history,
  error,
  onRetry,
}: {
  lights: KillSwitchLightOut[] | null;
  history: KillSwitchEventOut[] | null;
  error?: string | null;
  onRetry?: () => void;
}) {
  const failed = cardError(lights, error);
  return (
    <Panel title="Kill switches">
      <div className="ctl-stack">
        {failed || lights === null ? (
          <PartError message={failed ?? ""} onRetry={onRetry} />
        ) : (
          <ul className="ctl-list" aria-label="Kill switch lights">
            {lights.map((l) => {
              const s = stateOf(l);
              const value = valueLine(l);
              return (
                <li key={l.switch}>
                  <div className="ctl-row ctl-spread">
                    <span className="ctl-strong ctl-text">{l.label}</span>
                    <StatusChip tone={s.tone}>{s.word}</StatusChip>
                  </div>
                  {value && <div className="ctl-small num">{value}</div>}
                  <div className="ctl-small ctl-muted">{tripLine(l, history ?? [])}</div>
                  {l.clears && <div className="ctl-small ctl-muted ctl-text">{l.clears}</div>}
                </li>
              );
            })}
          </ul>
        )}
        <h3 className="ctl-sub">Reset and history</h3>
        <KillSwitchPanel />
      </div>
    </Panel>
  );
}
