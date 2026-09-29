// The equity chart (design §3 item 2, plan S6, DB-T7): the live run's equity for today or the whole run, the
// day's start line, fill markers (buy ▲, sell ▼) and a Today ⇄ Whole run toggle. Colours come from the theme
// tokens through `readToken`, so both themes work; no green or red here (money colours are for money values).
// DB-DENSE: on the Dashboard it is the hero card: the current equity in large type with today's change ($ and %)
// above a tall chart that fills the height it is given.
import type { SVGProps } from "react";
import { CartesianGrid, Line, LineChart, ReferenceDot, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";

import type { EquitySeriesOut, FillMarkerOut, LiveRange } from "../../api/types";
import { Button } from "../../components/ui";
import { fmtDateTime, fmtMoney, fmtPct } from "../../lib/format";
import { TOKENS, readToken } from "../../theme/tokens";
import { ChartFrame, plotNumber, plotTime, tickClock, tickDay } from "../performance/ChartFrame";
import { Money } from "./PeriodPnl";
import { Panel } from "./Panel";
import "./liveA.css";

export const EQUITY_CHART_HEIGHT = 220;
export const EMPTY_EQUITY = "No equity data for this range";

/** Today's change as a percentage of the equity at the day's start (`now - change`), or null. For display only. */
export function changePct(now: string | null | undefined, change: string | null | undefined): string | null {
  const n = plotNumber(now);
  const c = plotNumber(change);
  if (n === null || c === null) return null;
  const base = n - c;
  if (!(base > 0)) return null;
  return fmtPct((c / base).toFixed(8));
}

/** The current equity, large, with today's change in $ (green/red by sign, flat at zero) and %. */
function EquityHero({ now, change }: { now: string | null; change: string | null }) {
  const pct = changePct(now, change);
  return (
    <div className="lva-hero" data-testid="equity-hero">
      <span className="lva-hero-now">
        <Money value={now} tone="flat" />
      </span>
      <span className="lva-hero-change">
        <Money value={change} signed />
        {pct !== null && <span className="num lva-sub">{` (${pct})`}</span>}
        <span className="lva-label"> today</span>
      </span>
    </div>
  );
}

/** A token's current value, or the CSS variable itself when it cannot be read (tests, no stylesheet). */
function colour(name: keyof typeof TOKENS): string {
  return readToken(name) || `var(${TOKENS[name]})`;
}

interface Point {
  t: number;
  equity: number;
}

interface Marker {
  fill: FillMarkerOut;
  t: number;
  y: number;
}

/** The equity at time `t`: the latest point at or before it (the first point when `t` precedes them all). */
function equityAt(points: Point[], t: number): number {
  let y = points[0]!.equity;
  for (const p of points) {
    if (p.t > t) break;
    y = p.equity;
  }
  return y;
}

function markers(points: Point[], fills: FillMarkerOut[]): Marker[] {
  const first = points[0]!.t;
  const last = points[points.length - 1]!.t;
  const out: Marker[] = [];
  for (const fill of fills) {
    const t = plotTime(fill.ts);
    if (t === null || t < first || t > last) continue;
    out.push({ fill, t, y: equityAt(points, t) });
  }
  return out;
}

/** A buy is a triangle pointing up, a sell one pointing down. */
function FillMark({ cx, cy, side, fill }: { cx?: number; cy?: number; side: "buy" | "sell"; fill: string }) {
  if (cx === undefined || cy === undefined) return null;
  const s = 6;
  const d =
    side === "buy"
      ? `M${cx},${cy - s} L${cx + s},${cy + s * 0.8} L${cx - s},${cy + s * 0.8} Z`
      : `M${cx},${cy + s} L${cx + s},${cy - s * 0.8} L${cx - s},${cy - s * 0.8} Z`;
  return <path className={`lva-fill lva-fill-${side}`} d={d} fill={fill} stroke={colour("surface")} strokeWidth={1} />;
}

function Plot({ equity, range, tall }: { equity: EquitySeriesOut; range: LiveRange; tall: boolean }) {
  const points = equity.points
    .map((p) => ({ t: plotTime(p.ts), equity: plotNumber(p.equity) }))
    .filter((p): p is Point => p.t !== null && p.equity !== null)
    .sort((a, b) => a.t - b.t);
  if (points.length === 0) return <p className="lva-muted">{EMPTY_EQUITY}</p>;
  const start = plotNumber(equity.start_equity);
  const marks = markers(points, equity.fills);
  const muted = colour("textMuted");
  const buy = colour("chartEntry");
  const sell = colour("chartTarget");
  const domain: [number, number] = [points[0]!.t, points[points.length - 1]!.t];
  const last = equity.points[equity.points.length - 1];
  return (
    <figure className={tall ? "lva-figure is-tall" : "lva-figure"} aria-label={range === "today" ? "Equity today" : "Equity over the whole run"}>
      <div className="lva-plot">
      <ChartFrame height={EQUITY_CHART_HEIGHT} fill={tall}>
        <LineChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={colour("chartGrid")} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={domain}
            tickFormatter={range === "today" ? tickClock : tickDay}
            fontSize={11}
            stroke={muted}
            tick={{ fill: muted }}
          />
          <YAxis domain={["auto", "auto"]} width={64} fontSize={11} stroke={muted} tick={{ fill: muted }} tickFormatter={(v: number) => fmtMoney(v)} />
          <Tooltip
            labelFormatter={(t: number) => fmtDateTime(new Date(t).toISOString())}
            formatter={(v: number) => [fmtMoney(v), "Equity"]}
            contentStyle={{ background: colour("surface"), border: `1px solid ${colour("border")}`, color: colour("text") }}
          />
          {start !== null && <ReferenceLine y={start} className="lva-start-line" stroke={muted} strokeDasharray="4 4" ifOverflow="extendDomain" />}
          <Line dataKey="equity" name="Equity" stroke={colour("chartLine")} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          {marks.map((m) => {
            const side = m.fill.side === "sell" ? "sell" : "buy";
            return (
              <ReferenceDot
                key={m.fill.fill_id}
                x={m.t}
                y={m.y}
                r={6}
                ifOverflow="discard"
                shape={(props: SVGProps<SVGCircleElement>) => (
                  <FillMark cx={Number(props.cx)} cy={Number(props.cy)} side={side} fill={side === "buy" ? buy : sell} />
                )}
              />
            );
          })}
        </LineChart>
      </ChartFrame>
      </div>
      <figcaption className="lva-chart-note" data-testid="equity-legend">
        {start !== null && (
          <span>
            Start <Money value={equity.start_equity} tone="flat" />
          </span>
        )}
        {last && (
          <span>
            Now <Money value={last.equity} tone="flat" />
          </span>
        )}
        {marks.length > 0 && <span>{`▲ buy  ▼ sell (${marks.length} fills)`}</span>}
        {equity.downsampled && <span>{`Downsampled to ${equity.points.length} points`}</span>}
      </figcaption>
    </figure>
  );
}

export function EquityChart({
  equity,
  range,
  onRange,
  error,
  onRetry,
  now,
  changeToday = null,
  tall = false,
}: {
  equity: EquitySeriesOut | null;
  range: LiveRange;
  onRange: (r: LiveRange) => void;
  error?: string | null;
  onRetry?: () => void;
  /** DB-DENSE: the current equity; when given (even null), the hero line shows above the chart. */
  now?: string | null;
  /** Today's P&L after fees, shown beside the current equity. */
  changeToday?: string | null;
  /** The chart fills the height the page gives it (the Dashboard's hero card). */
  tall?: boolean;
}) {
  const toggle = (
    <div className="lva-toggle" role="group" aria-label="Equity range">
      <Button className="lva-seg" aria-pressed={range === "today"} onClick={() => onRange("today")}>
        Today
      </Button>
      <Button className="lva-seg" aria-pressed={range === "run"} onClick={() => onRange("run")}>
        Whole run
      </Button>
    </div>
  );
  const hasPoints = equity !== null && equity.points.length > 0;
  if (now === undefined) {
    return (
      <Panel title="Equity" className="lva-equity" badge={toggle} error={equity ? null : error} onRetry={onRetry} empty={hasPoints ? null : EMPTY_EQUITY}>
        {hasPoints && <Plot equity={equity} range={range} tall={tall} />}
      </Panel>
    );
  }
  return (
    <Panel title="Equity" className={tall && hasPoints ? "lva-equity is-tall" : "lva-equity"} badge={toggle} error={equity ? null : error} onRetry={onRetry}>
      <EquityHero now={now} change={changeToday} />
      {hasPoints ? <Plot equity={equity} range={range} tall={tall} /> : <p className="panel-empty lva-muted">{EMPTY_EQUITY}</p>}
    </Panel>
  );
}
