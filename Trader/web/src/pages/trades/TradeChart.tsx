// The 5-minute chart of a position's day: closes as a line, the high-low range as a band, horizontal lines at
// entry (fill price), stop (stop loss) and exit, and a dot at each fill.
import { Area, CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { CandleOut, FillOut, Money } from "../../api/types";
import { fmtPrice, fmtTime } from "../../lib/format";
import { ChartFrame, plotNumber, plotTime, tickClock } from "../performance/ChartFrame";

export interface TradeChartProps {
  candles: CandleOut[];
  /** Entry fill price. */
  entry: Money | null;
  /** The stop loss. */
  stop: Money | null;
  /** Exit price (null while the position is open). */
  exit: Money | null;
  fills: Pick<FillOut, "id" | "ts" | "price" | "purpose">[];
  chartError: string | null;
  /** A fixed width in px (tests); the container's width otherwise. */
  width?: number;
  height?: number;
}

interface Point {
  t: number;
  close: number;
  range: [number, number];
}

const LINES = [
  { key: "entry", label: "Entry", color: "#2563eb", dash: "4 3" },
  { key: "stop", label: "Stop", color: "#dc2626", dash: "4 3" },
  { key: "exit", label: "Exit", color: "#16a34a", dash: "4 3" },
] as const;

function toPoints(candles: CandleOut[]): Point[] {
  const out: Point[] = [];
  for (const c of candles) {
    const t = plotTime(c.start);
    const close = plotNumber(c.close);
    const high = plotNumber(c.high);
    const low = plotNumber(c.low);
    if (t === null || close === null || high === null || low === null) continue;
    out.push({ t, close, range: [low, high] });
  }
  return out.sort((a, b) => a.t - b.t);
}

export function TradeChart({ candles, entry, stop, exit, fills, chartError, width, height = 240 }: TradeChartProps) {
  const points = toPoints(candles);
  if (chartError !== null || points.length === 0) {
    return <p className="muted">Chart unavailable</p>;
  }
  const prices: Record<(typeof LINES)[number]["key"], Money | null> = { entry, stop, exit };
  return (
    <figure aria-label="5-minute chart" style={{ margin: 0 }}>
      <ChartFrame height={height} width={width}>
        <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            tickFormatter={tickClock}
            fontSize={11}
          />
          <YAxis domain={["auto", "auto"]} width={48} fontSize={11} tickFormatter={(v: number) => fmtPrice(v)} />
          <Tooltip
            labelFormatter={(t: number) => fmtTime(new Date(t).toISOString())}
            formatter={(value: number | number[], name: string) =>
              Array.isArray(value) ? [`${fmtPrice(value[0])} to ${fmtPrice(value[1])}`, "Low to high"] : [fmtPrice(value), name]
            }
          />
          <Area dataKey="range" name="Range" stroke="none" fill="#93c5fd" fillOpacity={0.35} isAnimationActive={false} />
          <Line dataKey="close" name="Close" stroke="#1f2937" dot={false} strokeWidth={1.5} isAnimationActive={false} />
          {LINES.map((line) => {
            const y = plotNumber(prices[line.key]);
            if (y === null) return null;
            return (
              <ReferenceLine
                key={line.key}
                y={y}
                stroke={line.color}
                strokeDasharray={line.dash}
                ifOverflow="extendDomain"
                label={{ value: `${line.label} ${fmtPrice(prices[line.key])}`, position: "insideTopLeft", fontSize: 11, fill: line.color }}
              />
            );
          })}
          {fills.map((f) => {
            const x = plotTime(f.ts);
            const y = plotNumber(f.price);
            if (x === null || y === null) return null;
            return (
              <ReferenceDot
                key={f.id}
                x={x}
                y={y}
                r={4}
                fill={f.purpose === "entry" ? "#2563eb" : "#16a34a"}
                stroke="#fff"
                ifOverflow="extendDomain"
              />
            );
          })}
        </ComposedChart>
      </ChartFrame>
    </figure>
  );
}
