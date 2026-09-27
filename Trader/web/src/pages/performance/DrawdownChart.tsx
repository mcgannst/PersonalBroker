// Drawdown from the equity peak, as a percentage below zero (BR-51). Shares the time axis with EquityChart.
import { Area, AreaChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts";

import type { EquityPointOut } from "../../api/types";
import { fmtDateTime } from "../../lib/format";
import { ChartFrame, plotNumber, plotTime, tickDay } from "./ChartFrame";
import { PERFORMANCE_SYNC_ID, timeDomain } from "./EquityChart";

function pctLabel(v: number): string {
  return `${v.toFixed(2)}%`;
}

export function DrawdownChart({ points, width, height = 140 }: { points: EquityPointOut[]; width?: number; height?: number }) {
  const data = points
    .map((p) => {
      const dd = plotNumber(p.drawdown_pct);
      // Plotted only: drawdown_pct is a fraction of the peak; shown as a negative percentage.
      return { t: plotTime(p.ts), drawdown: dd === null ? null : -Math.abs(dd) * 100 };
    })
    .filter((d): d is { t: number; drawdown: number } => d.t !== null && d.drawdown !== null);
  return (
    <figure aria-label="Drawdown" style={{ margin: 0 }}>
      <ChartFrame height={height} width={width}>
        <AreaChart data={data} syncId={PERFORMANCE_SYNC_ID} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="t" type="number" scale="time" domain={timeDomain(points)} tickFormatter={tickDay} fontSize={11} />
          <YAxis domain={["auto", 0]} width={56} fontSize={11} tickFormatter={pctLabel} />
          <Tooltip labelFormatter={(t: number) => fmtDateTime(new Date(t).toISOString())} formatter={(v: number) => [pctLabel(v), "Drawdown"]} />
          <Area dataKey="drawdown" name="Drawdown" stroke="var(--chart-stop)" fill="var(--chart-stop-fill)" isAnimationActive={false} />
        </AreaChart>
      </ChartFrame>
    </figure>
  );
}
