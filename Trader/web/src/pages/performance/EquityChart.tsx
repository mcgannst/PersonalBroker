// The equity curve (BR-51). Shares its time axis (and tooltip, through `syncId`) with DrawdownChart.
import { CartesianGrid, Line, LineChart, Tooltip, XAxis, YAxis } from "recharts";

import type { EquityPointOut } from "../../api/types";
import { fmtDateTime, fmtMoney } from "../../lib/format";
import { ChartFrame, plotNumber, plotTime, tickDay } from "./ChartFrame";

export const PERFORMANCE_SYNC_ID = "performance";

/** The shared x-axis domain of the equity and drawdown charts: first to last snapshot time. */
export function timeDomain(points: EquityPointOut[]): [number, number] | ["dataMin", "dataMax"] {
  const times = points.map((p) => plotTime(p.ts)).filter((t): t is number => t !== null);
  if (times.length === 0) return ["dataMin", "dataMax"];
  return [Math.min(...times), Math.max(...times)];
}

export function EquityChart({ points, width, height = 200 }: { points: EquityPointOut[]; width?: number; height?: number }) {
  const data = points
    .map((p) => ({ t: plotTime(p.ts), equity: plotNumber(p.equity) }))
    .filter((d): d is { t: number; equity: number } => d.t !== null && d.equity !== null);
  return (
    <figure aria-label="Equity" style={{ margin: 0 }}>
      <ChartFrame height={height} width={width}>
        <LineChart data={data} syncId={PERFORMANCE_SYNC_ID} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="t" type="number" scale="time" domain={timeDomain(points)} tickFormatter={tickDay} fontSize={11} />
          <YAxis domain={["auto", "auto"]} width={56} fontSize={11} tickFormatter={(v: number) => fmtMoney(v)} />
          <Tooltip labelFormatter={(t: number) => fmtDateTime(new Date(t).toISOString())} formatter={(v: number) => [fmtMoney(v), "Equity"]} />
          <Line dataKey="equity" name="Equity" stroke="var(--chart-entry)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
        </LineChart>
      </ChartFrame>
    </figure>
  );
}
