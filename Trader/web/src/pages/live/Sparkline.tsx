// A position's price since entry as a tiny inline SVG (DB-T8; no chart library): the prices as one polyline
// scaled to the box, the entry as a dashed line and the stop as a solid line in the status-bad token (a stop
// line is a status, not money). Fewer than 2 points draw a flat "no data" placeholder.
import type { SparkPointOut } from "../../api/types";
import { plotNumber, plotTime } from "../performance/ChartFrame";

import "./liveB.css";

export interface SparklineProps {
  points: SparkPointOut[];
  /** The entry price (a decimal string). */
  entry: string;
  stop: string | null;
  width?: number;
  height?: number;
  /** The accessible name, e.g. "AAA since entry". */
  label: string;
}

/** Inner padding, so a line at the edge of the range is not clipped. */
const PAD = 2;

function round2(n: number): string {
  return String(Math.round(n * 100) / 100);
}

export function Sparkline({ points, entry, stop, width = 96, height = 28, label }: SparklineProps) {
  const plotted: { t: number; y: number }[] = [];
  for (const p of points) {
    const t = plotTime(p.ts);
    const y = plotNumber(p.price);
    if (t !== null && y !== null) plotted.push({ t, y });
  }
  const box = { width, height, viewBox: `0 0 ${width} ${height}` };

  if (plotted.length < 2) {
    const mid = round2(height / 2);
    return (
      <svg className="sparkline sparkline-empty" role="img" aria-label={`${label}: no data`} {...box}>
        <line className="spark-empty" x1={PAD} x2={width - PAD} y1={mid} y2={mid} stroke="var(--status-muted)" strokeDasharray="2 3" strokeWidth={1} />
      </svg>
    );
  }

  const entryY = plotNumber(entry);
  const stopY = plotNumber(stop);
  const values = plotted.map((p) => p.y);
  if (entryY !== null) values.push(entryY);
  if (stopY !== null) values.push(stopY);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (hi === lo) {
    lo -= 1;
    hi += 1;
  }
  const t0 = plotted[0]!.t;
  const t1 = plotted[plotted.length - 1]!.t;
  const span = t1 - t0;
  const innerW = width - 2 * PAD;
  const innerH = height - 2 * PAD;
  const xOf = (t: number, i: number): number => PAD + (span > 0 ? ((t - t0) / span) * innerW : (i / (plotted.length - 1)) * innerW);
  const yOf = (v: number): number => PAD + ((hi - v) / (hi - lo)) * innerH;
  const coords = plotted.map((p, i) => `${round2(xOf(p.t, i))},${round2(yOf(p.y))}`).join(" ");

  return (
    <svg className="sparkline" role="img" aria-label={label} {...box}>
      {entryY !== null && (
        <line className="spark-entry" x1={PAD} x2={width - PAD} y1={round2(yOf(entryY))} y2={round2(yOf(entryY))} stroke="var(--chart-entry)" strokeDasharray="3 2" strokeWidth={1} />
      )}
      {stopY !== null && (
        <line className="spark-stop" x1={PAD} x2={width - PAD} y1={round2(yOf(stopY))} y2={round2(yOf(stopY))} stroke="var(--status-bad)" strokeWidth={1} />
      )}
      <polyline points={coords} fill="none" stroke="var(--chart-line)" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
