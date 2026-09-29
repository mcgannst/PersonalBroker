// The dashboard's top bar (design §3 item 1, D3/D4/D9, plan DB-T7): the region labelled "Session". Session
// date and phase; the three periods' trading P&L after fees with realised and open in small text; the costs
// (fees, Claude spend against its cap, net after AI); win rate, trades and expectancy of the run; the engine
// chip; the live indicator; the heartbeat badge when the worker is stale; and the books check. Money values
// are `.money` spans (the only green and red); chips and lights use status tokens.
import { useEffect, useState } from "react";

import type { LiveOut, PeriodPnlOut, TradingState } from "../../api/types";
import { fmtDate, fmtR, fmtRate } from "../../lib/format";
import { phaseLabel } from "../dashboard/labels";
import { BooksCheck } from "./BooksCheck";
import { CostBar } from "./CostBar";
import { DASH, Money, orderedPeriods, periodLabel } from "./PeriodPnl";
import { Panel } from "./Panel";
import "./liveA.css";

/** Data at most this old, with the stream connected, is "Live". */
export const LIVE_FRESH_MS = 10_000;
export const DEGRADED_TEXT = "Degraded: polling every 15 s";

const TRADING_TONE: Record<TradingState, string> = { running: "status-ok", paused: "status-warn", blocked: "status-bad" };

/** The message of a failed part (S15), if any. */
export function partError(live: LiveOut, part: string): string | null {
  return live.part_errors.find((e) => e.part === part)?.message ?? null;
}

/** `nowMs` when given (tests), else the browser clock, re-read every second. */
function useNow(nowMs: number | undefined): number {
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (nowMs !== undefined) return undefined;
    const id = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [nowMs]);
  return nowMs ?? tick;
}

export function liveIndicator(connected: boolean, updatedAt: number, nowMs: number): { text: string; tone: string } {
  if (!connected) return { text: DEGRADED_TEXT, tone: "status-warn" };
  const ageMs = Math.max(0, nowMs - updatedAt);
  if (ageMs <= LIVE_FRESH_MS) return { text: "Live", tone: "status-ok" };
  return { text: `Updated ${Math.round(ageMs / 1000)} s ago`, tone: "status-warn" };
}

function title(live: LiveOut): string {
  if (!live.session.is_session || live.session.date !== live.session_day) {
    return `${phaseLabel(live.session.phase)} · last session ${fmtDate(live.session_day)}`;
  }
  return `Session ${fmtDate(live.session_day)} · ${phaseLabel(live.session.phase)}`;
}

function PeriodTile({ p }: { p: PeriodPnlOut }) {
  return (
    <div className="lva-tile" data-testid={`topbar-period-${p.period}`}>
      <div className="lva-label">{periodLabel(p.period)}</div>
      <div className="lva-headline">
        <Money value={p.pnl_after_fees} signed />
      </div>
      <div className="lva-sub">
        realised <Money value={p.realized} signed /> · open{" "}
        {p.unrealized === null ? <span className="num lva-dash">{DASH}</span> : <Money value={p.unrealized} signed />}
        {p.unrealized !== null && p.unrealized_partial && <span className="lva-partial"> partial</span>}
      </div>
    </div>
  );
}

function RunStats({ run }: { run: PeriodPnlOut }) {
  return (
    <div className="lva-stats" data-testid="topbar-run-stats">
      <div className="lva-stat">
        <span className="lva-label">Win rate</span>
        <span className="num">{run.win_rate === null ? DASH : fmtRate(run.win_rate)}</span>
      </div>
      <div className="lva-stat">
        <span className="lva-label">Trades</span>
        <span className="num">{run.trades}</span>
      </div>
      <div className="lva-stat">
        <span className="lva-label">Expectancy</span>
        <span className="num">{run.expectancy_r === null ? DASH : fmtR(run.expectancy_r)}</span>
      </div>
    </div>
  );
}

export function TopBar({ live, connected, updatedAt, nowMs }: { live: LiveOut; connected: boolean; updatedAt: number; nowMs?: number }) {
  const now = useNow(nowMs);
  const indicator = liveIndicator(connected, updatedAt, now);
  const periods = orderedPeriods(live.periods);
  const run = periods.find((p) => p.period === "run");
  const age = live.worker.age_seconds;
  const status = (
    <div className="lva-status-row">
      <span className={`lva-chip ${TRADING_TONE[live.trading] ?? "status-muted"}`} data-testid="engine-chip">
        {`${live.approval_mode.toUpperCase()} · ${live.trading.toUpperCase()}`}
      </span>
      <span className={`lva-chip ${indicator.tone}`} data-testid="live-indicator" role="status">
        {indicator.text}
      </span>
      {live.worker_stale && (
        <span className="lva-chip status-bad" data-testid="heartbeat-badge">
          {age === null ? "Worker heartbeat stale" : `Worker heartbeat ${Math.round(age)} s old`}
        </span>
      )}
    </div>
  );
  return (
    <div className="lva-topbar">
      <Panel title={title(live)} ariaLabel="Session" badge={status}>
        <div className="lva-top-grid">
          {periods.length > 0 ? (
            <div className="lva-top-periods">
              {periods.map((p) => (
                <PeriodTile key={p.period} p={p} />
              ))}
            </div>
          ) : (
            <p className="lva-muted">{partError(live, "periods") ?? "P&L unavailable"}</p>
          )}
          {run && <RunStats run={run} />}
          <div className="lva-top-side">
            <CostBar claude={live.claude_today} periods={live.periods} />
            <BooksCheck books={live.books} error={partError(live, "books") ?? "Books check unavailable"} />
          </div>
        </div>
      </Panel>
    </div>
  );
}
