// The risk panel (design §3 item 2, DB-T7): the four kill-switch lights with value vs threshold in their units
// (percent to 1 dp, R to 2 dp, trade count vs its minimum), tripped switches first with their time in MT and a
// link to reset them on Control; open risk against its cap as a bar; slots used and open positions. Lights use
// status tokens only (accent, amber, orange), never green or red.
import { Link } from "react-router-dom";

import type { KillSwitchLightOut, RiskOut } from "../../api/types";
import { fmtTime } from "../../lib/format";
import { plotNumber } from "../performance/ChartFrame";
import { Meter } from "./CostBar";
import { DASH, Money } from "./PeriodPnl";
import { Panel } from "./Panel";
import "./liveA.css";

/** A fraction as a percentage to 1 dp (`0.0013` → `0.1%`), for display only. */
export function pct1(value: string | null | undefined): string {
  const n = plotNumber(value);
  return n === null ? DASH : `${(n * 100).toFixed(1)}%`;
}

/** An R value to 2 dp (`0.7091` → `0.71 R`). */
export function r2(value: string | null | undefined): string {
  const n = plotNumber(value);
  return n === null ? DASH : `${n.toFixed(2)} R`;
}

/** "value of threshold" (percent: a ceiling), "value vs threshold" (R: a floor), yes/no for a pause. */
export function switchValue(k: KillSwitchLightOut): string {
  const value = k.tripped && k.trip_value !== null ? k.trip_value : k.value;
  const threshold = k.tripped && k.trip_threshold !== null ? k.trip_threshold : k.threshold;
  switch (k.unit) {
    case "pct":
      return `${pct1(value)} of ${pct1(threshold)}`;
    case "r":
      return `${r2(value)} vs ${r2(threshold)}`;
    default:
      return k.tripped ? "Yes" : "No";
  }
}

function lightTone(k: KillSwitchLightOut): string {
  if (!k.tripped) return "status-ok";
  return k.automatic ? "status-bad" : "status-warn";
}

function SwitchLight({ k }: { k: KillSwitchLightOut }) {
  return (
    <li className={k.tripped ? "lva-switch is-tripped" : "lva-switch"} data-testid={`killswitch-${k.switch}`}>
      <div className="lva-switch-head">
        <span className={`lva-dot ${lightTone(k)}`} role="img" aria-label={k.tripped ? "tripped" : "not tripped"} />
        <span className="lva-switch-label">{k.label}</span>
        <span className="num lva-switch-value">{switchValue(k)}</span>
      </div>
      {k.count_min !== null && (
        <div className="lva-sub">
          {k.count ?? 0} of {k.count_min} trades
        </div>
      )}
      {k.tripped && (
        <div className="lva-trip">
          <span className="lva-trip-at">Tripped {fmtTime(k.tripped_at)}</span>
          <span className="lva-sub">{k.clears}</span>
          <Link className="link-touch" to="/control">
            Reset on Control
          </Link>
        </div>
      )}
    </li>
  );
}

function RiskBody({ risk }: { risk: RiskOut }) {
  // tripped switches first; otherwise the API's order
  const switches = [...risk.killswitches].sort((a, b) => Number(b.tripped) - Number(a.tripped));
  const cap = plotNumber(risk.open_risk_cap);
  const used = plotNumber(risk.open_risk) ?? 0;
  return (
    <>
      <ul className="lva-switches">
        {switches.map((k) => (
          <SwitchLight key={k.switch} k={k} />
        ))}
      </ul>
      <div className="lva-risk-facts">
        <div className="lva-open-risk" data-testid="open-risk">
          <div className="lva-row">
            <span className="lva-label">Open risk</span>
            <span className="lva-value">
              {cap !== null && cap > 0 ? (
                <>
                  <Money value={risk.open_risk} tone="flat" /> of <Money value={risk.open_risk_cap} tone="flat" />
                </>
              ) : (
                <>
                  <Money value={risk.open_risk} tone="flat" /> (no cap)
                </>
              )}
            </span>
          </div>
          {cap !== null && cap > 0 && <Meter fraction={used / cap} label="Open risk against the cap" />}
        </div>
        <div className="lva-row" data-testid="slots">
          <span className="lva-label">Slots</span>
          <span className="num">
            {risk.slots_used} / {risk.slots_max}
          </span>
        </div>
        <div className="lva-row" data-testid="open-positions">
          <span className="lva-label">Open positions</span>
          <span className="num">{risk.open_positions}</span>
        </div>
      </div>
    </>
  );
}

export function RiskPanel({ risk, error, onRetry }: { risk: RiskOut | null; error?: string | null; onRetry?: () => void }) {
  return (
    <Panel title="Risk" error={risk ? null : error} onRetry={onRetry} empty={risk ? null : "No risk data"}>
      {risk && <RiskBody risk={risk} />}
    </Panel>
  );
}
