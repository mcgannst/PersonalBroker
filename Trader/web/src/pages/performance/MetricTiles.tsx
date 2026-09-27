// Metric tiles (BR-51): trades, win rate, expectancy R, profit factor, average slippage, max drawdown,
// adherence and total P&L.
import type { MetricsOut } from "../../api/types";
import { Stat } from "../../components/ui";
import { fmtMoney, fmtPct, fmtPrice, fmtR } from "../../lib/format";

/** A fraction as an unsigned percentage (`0.5` → `50.00%`), for rates that are never negative. */
export function fmtRate(fraction: string | null | undefined): string {
  return fmtPct(fraction).replace(/^\+/, "");
}

/** A plain decimal ratio (`1.2500` → `1.25`). */
function fmtRatio(value: string | null | undefined): string {
  return fmtPrice(value);
}

function fmtSlippage(value: string | null | undefined): string {
  const p = fmtPrice(value);
  if (p === "n/a") return p;
  return p.startsWith("-") ? `-$${p.slice(1)}` : `$${p}`;
}

export function MetricTiles({ metrics }: { metrics: MetricsOut }) {
  return (
    <div className="grid" aria-label="Metrics">
      <Stat label="Trades" value={String(metrics.trades)} sub={`${metrics.wins} won`} />
      <Stat label="Win rate" value={fmtRate(metrics.win_rate)} />
      <Stat label="Expectancy" value={fmtR(metrics.expectancy_r)} sub={`avg win ${fmtR(metrics.avg_win_r)}, avg loss ${fmtR(metrics.avg_loss_r)}`} />
      <Stat label="Profit factor" value={fmtRatio(metrics.profit_factor)} />
      <Stat label="Avg slippage" value={fmtSlippage(metrics.avg_slippage)} sub="per share" />
      <Stat label="Max drawdown" value={fmtRate(metrics.max_drawdown_pct)} />
      <Stat label="Adherence" value={fmtRate(metrics.adherence_pct)} sub="rules followed" />
      <Stat label="Total P&L" value={fmtMoney(metrics.total_pnl)} />
    </div>
  );
}
