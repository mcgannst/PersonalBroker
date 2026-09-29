// The dashboard's stat strip (design §3 item 1, D3/D4/D9, plan DB-T7; dense layout DB-DENSE): the region
// labelled "Session", one row of compact tiles (wrapping on narrower screens): the three periods' trading P&L
// after fees with realised and open in small text; the costs (fees, Claude spend against its cap, net after AI);
// win rate, trades and expectancy of the run; open risk and slots; the engine chip under the session date and
// phase; the books check; the live indicator with the last update time and the heartbeat badge when the worker
// is stale. A failed part (periods, Claude spend, books) shows its message in its tile with Retry. Money values
// are `.money` spans (the only green and red); chips and lights use status tokens.
import { QueryClientContext } from "@tanstack/react-query";
import { useContext, useEffect, useState } from "react";

import type { LiveOut, PeriodPnlOut, RiskOut, TradingState } from "../../api/types";
import { fmtDate, fmtR, fmtRate, fmtTime } from "../../lib/format";
import { plotNumber } from "../performance/ChartFrame";
import { phaseLabel } from "../dashboard/labels";
import { BooksCheck } from "./BooksCheck";
import { CostBar, InlinePartError, Meter } from "./CostBar";
import { ACCOUNT_CURRENCY } from "./EquityChart";
import { DASH, Money, orderedPeriods, periodLabel } from "./PeriodPnl";
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
    <div className="st st-period lva-tile" data-testid={`topbar-period-${p.period}`}>
      <div className="st-label lva-label">
        <span>{periodLabel(p.period)}</span>
        <span className="lva-ccy">{` · ${ACCOUNT_CURRENCY}`}</span>
      </div>
      <div className="st-value lva-headline">
        <Money value={p.pnl_after_fees} signed />
      </div>
      <div className="st-sub lva-sub">
        realised <Money value={p.realized} signed /> · open{" "}
        {p.unrealized === null ? <span className="num lva-dash">{DASH}</span> : <Money value={p.unrealized} signed />}
        {p.unrealized !== null && p.unrealized_partial && <span className="lva-partial"> partial</span>}
      </div>
    </div>
  );
}

function RunStats({ run }: { run: PeriodPnlOut }) {
  return (
    <div className="st st-run lva-stats" data-testid="topbar-run-stats">
      <div className="lva-stat">
        <span className="st-label lva-label">Win rate</span>
        <span className="st-value num">{run.win_rate === null ? DASH : fmtRate(run.win_rate)}</span>
      </div>
      <div className="lva-stat">
        <span className="st-label lva-label">Trades</span>
        <span className="st-value num">{run.trades}</span>
      </div>
      <div className="lva-stat">
        <span className="st-label lva-label">Expectancy</span>
        <span className="st-value num">{run.expectancy_r === null ? DASH : fmtR(run.expectancy_r)}</span>
      </div>
    </div>
  );
}

/** Open risk against its cap and the slots in use. A failed risk part shows a dash here (its error is on Risk). */
function RiskTile({ risk }: { risk: RiskOut | null }) {
  const cap = risk ? plotNumber(risk.open_risk_cap) : null;
  const used = risk ? (plotNumber(risk.open_risk) ?? 0) : 0;
  return (
    <div className="st st-risk" data-testid="strip-risk">
      <div className="st-label lva-label">Open risk</div>
      <div className="st-value">{risk ? <Money value={risk.open_risk} tone="flat" /> : <span className="num lva-dash">{DASH}</span>}</div>
      {risk && (
        <div className="st-sub lva-sub">
          {cap !== null && cap > 0 ? (
            <>
              of <Money value={risk.open_risk_cap} tone="flat" /> ·{" "}
            </>
          ) : null}
          <span className="num">{`slots ${risk.slots_used}/${risk.slots_max}`}</span>
        </div>
      )}
      {risk && cap !== null && cap > 0 && <Meter fraction={used / cap} label="Open risk used of its cap" />}
    </div>
  );
}

/**
 * Retry for the top bar's failed parts (fix round 1, DB-GWEB): the given `onRetry` (DB-T11 wires the page's
 * refetch), else a refetch of every `["dashboard"]` query (`qk.live` lives under it) when a QueryClient is
 * mounted, else none (no Retry button, the error text still shows).
 */
function useTopBarRetry(onRetry: (() => void) | undefined): (() => void) | undefined {
  const client = useContext(QueryClientContext);
  if (onRetry) return onRetry;
  if (!client) return undefined;
  return () => void client.invalidateQueries({ queryKey: ["dashboard"] });
}

export function TopBar({
  live,
  connected,
  updatedAt,
  nowMs,
  onRetry,
}: {
  live: LiveOut;
  connected: boolean;
  updatedAt: number;
  nowMs?: number;
  /** Fix round 1: Retry for a failed `periods`, `claude_today` or `books` part. */
  onRetry?: () => void;
}) {
  const now = useNow(nowMs);
  const retry = useTopBarRetry(onRetry);
  const indicator = liveIndicator(connected, updatedAt, now);
  const periods = orderedPeriods(live.periods);
  const run = periods.find((p) => p.period === "run");
  const age = live.worker.age_seconds;
  return (
    <section className="dash-strip lva-topbar" aria-label="Session">
      {periods.length > 0 ? (
        periods.map((p) => <PeriodTile key={p.period} p={p} />)
      ) : (
        <div className="st st-periods-error">
          <div className="st-label lva-label">Trading P&amp;L</div>
          <InlinePartError message={partError(live, "periods") ?? "P&L unavailable"} onRetry={retry} />
        </div>
      )}
      <CostBar claude={live.claude_today} periods={live.periods} error={partError(live, "claude_today") ?? "Claude spend unavailable"} onRetry={retry} />
      {run && <RunStats run={run} />}
      <RiskTile risk={live.risk} />
      <div className="st st-engine">
        <h2 className="st-label st-title">{title(live)}</h2>
        <div className="st-value">
          <span className={`lva-chip ${TRADING_TONE[live.trading] ?? "status-muted"}`} data-testid="engine-chip">
            {`${live.approval_mode.toUpperCase()} · ${live.trading.toUpperCase()}`}
          </span>
        </div>
      </div>
      <BooksCheck books={live.books} error={partError(live, "books") ?? "Books check unavailable"} onRetry={retry} />
      <div className="st st-live">
        <div className="st-label lva-label">Feed</div>
        <div className="st-value">
          <span className={`lva-chip ${indicator.tone}`} data-testid="live-indicator" role="status">
            {indicator.text}
          </span>
        </div>
        {updatedAt > 0 && <div className="st-sub lva-sub num">{`last update ${fmtTime(new Date(updatedAt).toISOString())}`}</div>}
        {live.worker_stale && (
          <span className="lva-chip status-bad" data-testid="heartbeat-badge">
            {age === null ? "Worker heartbeat stale" : `Worker heartbeat ${Math.round(age)} s old`}
          </span>
        )}
      </div>
    </section>
  );
}
