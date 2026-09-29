// An expanded position's chart (DB-T8): the 1-minute closes since entry, the entry, stop and target lines and
// a dot at each fill. Bars built from the worker's quotes (`source: "marks"`) are flagged with a small note.
import { CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { LivePositionOut } from "../../api/types";
import { fmtPrice, fmtTime } from "../../lib/format";
import { ChartFrame, plotNumber, plotTime, tickClock } from "../performance/ChartFrame";
import { SafeLink } from "./safeLink";

import "./liveB.css";

const CHART_HEIGHT = 200;

const LINES = [
  { key: "entry", label: "Entry", color: "var(--chart-entry)", dash: "4 3" },
  { key: "stop", label: "Stop", color: "var(--chart-stop)", dash: undefined },
  { key: "target", label: "Target", color: "var(--chart-target)", dash: "4 3" },
] as const;

interface Point {
  t: number;
  close: number;
}

function toPoints(p: LivePositionOut): Point[] {
  const out: Point[] = [];
  for (const b of p.bars ?? []) {
    const t = plotTime(b.start);
    const close = plotNumber(b.close);
    if (t !== null && close !== null) out.push({ t, close });
  }
  return out.sort((a, b) => a.t - b.t);
}

export function PositionChart({ p, width }: { p: LivePositionOut; /** A fixed width in px (tests). */ width?: number }) {
  const points = toPoints(p);
  const fromQuotes = (p.bars ?? []).some((b) => b.source === "marks");
  const prices = { entry: p.entry, stop: p.stop, target: p.target };
  const open = (
    <SafeLink className="link-touch" to={p.link}>
      Open trade
    </SafeLink>
  );

  if (points.length === 0) {
    return (
      <div className="pos-chart pos-chart-empty">
        <p className="muted small">Chart data not available yet</p>
        {open}
      </div>
    );
  }

  return (
    <div className="pos-chart">
      <figure aria-label={`${p.ticker} 1-minute chart`} style={{ margin: 0 }}>
        <ChartFrame height={CHART_HEIGHT} width={width}>
          <ComposedChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={tickClock} fontSize={11} stroke="var(--text-muted)" />
            <YAxis domain={["auto", "auto"]} width={48} fontSize={11} tickFormatter={(v: number) => fmtPrice(v)} stroke="var(--text-muted)" />
            <Tooltip
              labelFormatter={(t: number) => fmtTime(new Date(t).toISOString())}
              formatter={(value: number, name: string) => [fmtPrice(value), name]}
              contentStyle={{ background: "var(--surface)", border: "var(--panel-border)", color: "var(--text)" }}
            />
            <Line dataKey="close" name="Close" stroke="var(--chart-line)" dot={false} strokeWidth={1.5} isAnimationActive={false} />
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
            {p.fills.map((f) => {
              const x = plotTime(f.ts);
              const y = plotNumber(f.price);
              if (x === null || y === null) return null;
              return (
                <ReferenceDot
                  key={f.fill_id}
                  x={x}
                  y={y}
                  r={4}
                  fill={f.purpose === "entry" ? "var(--chart-entry)" : "var(--status-warn)"}
                  stroke="var(--surface)"
                  ifOverflow="extendDomain"
                />
              );
            })}
          </ComposedChart>
        </ChartFrame>
      </figure>
      <div className="pos-chart-foot">
        {fromQuotes && <span className="muted small">bars from quotes</span>}
        {open}
      </div>
    </div>
  );
}
