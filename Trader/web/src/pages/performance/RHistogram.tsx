// The R-multiple histogram (BR-51): one bar per bin of `MetricsOut.r_histogram`.
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from "recharts";

import type { HistogramBinOut } from "../../api/types";
import { ChartFrame, plotNumber } from "./ChartFrame";

/** A bin's axis label: `-1 to -0.5`; an open-ended first or last bin reads `< -3` or `≥ 5`. */
export function histogramLabel(bin: HistogramBinOut): string {
  const lo = plotNumber(bin.lo);
  const hi = plotNumber(bin.hi);
  if (lo === null && hi !== null) return `< ${hi}`;
  if (hi === null && lo !== null) return `≥ ${lo}`;
  if (lo === null || hi === null) return "n/a";
  return `${lo} to ${hi}`;
}

export function RHistogram({ bins, width, height = 180 }: { bins: HistogramBinOut[]; width?: number; height?: number }) {
  const data = bins.map((b) => ({ label: histogramLabel(b), count: b.count }));
  return (
    <figure aria-label="R multiples" style={{ margin: 0 }}>
      <ChartFrame height={height} width={width}>
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
          <XAxis dataKey="label" fontSize={10} interval={0} />
          <YAxis allowDecimals={false} width={32} fontSize={11} />
          <Tooltip formatter={(v: number) => [v, "Trades"]} />
          <Bar dataKey="count" name="Trades" fill="var(--chart-bar)" isAnimationActive={false} />
        </BarChart>
      </ChartFrame>
    </figure>
  );
}
