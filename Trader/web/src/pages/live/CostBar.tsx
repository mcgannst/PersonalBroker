// Costs (design D4, plan DB-T7): today's Claude spend against its daily cap as a bar (capped visually at
// 100 %, amber from 80 %, the exact numbers as text; text only when there is no cap), then fees, Claude spend
// and net after AI for each period. Fees and spend are costs, shown in the neutral money tone; net after AI
// is P&L, green or red by sign.
import type { ClaudeTodayOut, PeriodPnlOut } from "../../api/types";
import { fmtRate } from "../../lib/format";
import { plotNumber } from "../performance/ChartFrame";
import { Money, orderedPeriods, periodLabel } from "./PeriodPnl";
import { Panel } from "./Panel";
import "./liveA.css";

/** From this share of the cap on, a meter turns amber. */
export const METER_WARN_FRACTION = 0.8;

/**
 * A horizontal bar for a used share (a decimal string or number; 1 = full). The bar never goes past 100 %;
 * the caller shows the exact numbers as text. Status tokens only (accent, then amber from 80 %).
 */
export function Meter({ fraction, label }: { fraction: string | number; label: string }) {
  const f = plotNumber(fraction) ?? 0;
  const pct = Math.min(100, Math.max(0, Math.round(f * 1000) / 10));
  const tone = f >= METER_WARN_FRACTION ? "status-warn" : "status-ok";
  return (
    <div className="lva-meter" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
      <div className={`lva-meter-fill ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function ClaudeToday({ claude }: { claude: ClaudeTodayOut }) {
  const cap = plotNumber(claude.cap_usd) ?? 0;
  const capped = cap > 0 && claude.used_fraction !== null;
  return (
    <div className="lva-claude">
      <div className="lva-row">
        <span className="lva-label">Claude today</span>
        <span className="lva-value" data-testid="claude-today">
          {capped ? (
            <>
              <Money value={claude.spent_usd} tone="flat" className="lva-spent" /> of <Money value={claude.cap_usd} tone="flat" className="lva-cap" /> cap (
              {fmtRate(claude.used_fraction)})
            </>
          ) : (
            <>
              <Money value={claude.spent_usd} tone="flat" className="lva-spent" /> today (no cap set)
            </>
          )}
        </span>
      </div>
      {capped && <Meter fraction={claude.used_fraction ?? 0} label="Claude spend today against the cap" />}
    </div>
  );
}

function CostRow({ p }: { p: PeriodPnlOut }) {
  return (
    <div className="lva-cost-row" data-testid={`costs-${p.period}`}>
      <span className="lva-label">{periodLabel(p.period)}</span>
      <span className="lva-cost">
        <span className="lva-k">Fees</span>
        <Money value={p.fees} tone="flat" />
      </span>
      <span className="lva-cost">
        <span className="lva-k">Claude</span>
        <Money value={p.claude_usd} tone="flat" />
      </span>
      <span className="lva-cost">
        <span className="lva-k">Net after AI</span>
        <Money value={p.net_after_ai} signed />
      </span>
    </div>
  );
}

export function CostBar({ claude, periods }: { claude: ClaudeTodayOut | null; periods: PeriodPnlOut[] | null }) {
  const rows = orderedPeriods(periods);
  const nothing = claude === null && rows.length === 0;
  return (
    <Panel title="Costs" empty={nothing ? "No cost data" : null}>
      {claude && <ClaudeToday claude={claude} />}
      {rows.length > 0 && (
        <div className="lva-costs">
          {rows.map((p) => (
            <CostRow key={p.period} p={p} />
          ))}
        </div>
      )}
    </Panel>
  );
}
