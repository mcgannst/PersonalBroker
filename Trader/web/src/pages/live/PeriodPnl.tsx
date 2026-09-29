// Gains by period (design D3, plan DB-T7): today | this week | since start, each with its trading P&L after
// fees, realised and open, trades, wins/losses, win rate and (run only) expectancy in R. Also home of the
// small money helpers the other A components share (`Money`, `periodLabel`): the only place green and red
// appear is a `.money` span (D9).
import type { ReactNode } from "react";

import type { PeriodKey, PeriodPnlOut } from "../../api/types";
import { fmtMoney, fmtR, fmtRate } from "../../lib/format";
import { moneyTone } from "../../theme/tokens";
import { Panel } from "./Panel";
import "./liveA.css";

export const DASH = "–";

const LABELS: Record<PeriodKey, string> = { today: "Today", week: "This week", run: "Since start" };
const ORDER: PeriodKey[] = ["today", "week", "run"];

export function periodLabel(key: PeriodKey): string {
  return LABELS[key] ?? key;
}

/** The periods in display order (today, week, run), whatever order the API sent. */
export function orderedPeriods(periods: PeriodPnlOut[] | null | undefined): PeriodPnlOut[] {
  if (!periods) return [];
  return ORDER.map((k) => periods.find((p) => p.period === k)).filter((p): p is PeriodPnlOut => p !== undefined);
}

/**
 * A money value (a decimal string) as a `.money` span with its tone: green/red by sign, or always `flat` for
 * balances and costs (`tone="flat"`). `signed` adds a plus to gains. A null value is a plain "–" (not money).
 */
export function Money({
  value,
  signed = false,
  tone = "auto",
  className,
}: {
  value: string | null | undefined;
  signed?: boolean;
  tone?: "auto" | "flat";
  className?: string;
}) {
  const text = value === null || value === undefined ? null : fmtMoney(value);
  if (text === null || !text.includes("$")) return <span className="num lva-dash">{DASH}</span>;
  const zero = /^\$0\.00$/.test(text);
  const t = tone === "flat" || zero ? "flat" : moneyTone(value);
  const shown = signed && t === "up" ? `+${text}` : text;
  return <span className={["money", t, className].filter(Boolean).join(" ")}>{shown}</span>;
}

/** The open P&L: "–" when no open position has a mark, "partial" when some lack one. */
function OpenPnl({ p }: { p: PeriodPnlOut }) {
  if (p.unrealized === null) return <span className="num lva-dash">{DASH}</span>;
  return (
    <>
      <Money value={p.unrealized} signed />
      {p.unrealized_partial && (
        <>
          {" "}
          <span className="lva-partial">partial</span>
        </>
      )}
    </>
  );
}

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div className="lva-fact">
      <dt>{label}</dt>
      <dd data-testid={testId}>{children}</dd>
    </div>
  );
}

function PeriodColumn({ p }: { p: PeriodPnlOut }) {
  return (
    <div className="lva-period" data-testid={`period-${p.period}`}>
      <div className="lva-label">{periodLabel(p.period)}</div>
      <div className="lva-headline">
        <Money value={p.pnl_after_fees} signed />
      </div>
      <dl className="lva-facts">
        <Fact label="Realised">
          <Money value={p.realized} signed />
        </Fact>
        <Fact label="Open" testId={`open-${p.period}`}>
          <OpenPnl p={p} />
        </Fact>
        <Fact label="Trades">
          <span className="num">{p.trades}</span>
        </Fact>
        <Fact label="W / L">
          <span className="num">
            {p.wins} / {p.losses}
          </span>
        </Fact>
        <Fact label="Win rate">
          <span className="num">{p.win_rate === null ? DASH : fmtRate(p.win_rate)}</span>
        </Fact>
        {p.period === "run" && (
          <Fact label="Expectancy">
            <span className="num">{p.expectancy_r === null ? DASH : fmtR(p.expectancy_r)}</span>
            {p.trades_without_r > 0 && <span className="lva-sub"> ({p.trades_without_r} without R)</span>}
          </Fact>
        )}
      </dl>
    </div>
  );
}

export function PeriodPnl({ periods, error, onRetry }: { periods: PeriodPnlOut[] | null; error?: string | null; onRetry?: () => void }) {
  const cols = orderedPeriods(periods);
  return (
    <Panel title="Gains by period" error={periods ? null : error} onRetry={onRetry} empty={cols.length === 0 ? "No P&L data" : null}>
      {cols.length > 0 && (
        <div className="lva-periods">
          {cols.map((p) => (
            <PeriodColumn key={p.period} p={p} />
          ))}
        </div>
      )}
    </Panel>
  );
}
