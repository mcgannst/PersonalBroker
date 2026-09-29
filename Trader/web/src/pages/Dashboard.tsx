// The Dashboard (live dashboard design §3, plan DB-T11; D1-D5, D9): a read-only live monitor fed by
// `GET /api/live` (query `qk.live`, under the `dashboard` prefix, so every mutation and SSE topic that refreshes
// the dashboard refreshes it; the provider throttles the 2 s `marks` topic, plan S9). Dense layout (DB-DENSE,
// panels in the "dense" density; phone and tablet portrait: this order, stacked):
//   1. TopBar: the stat strip (P&L by period, costs, run stats, open risk, engine, books, live indicator)
//   2. main column (~2/3): the equity hero card (tall chart), then the positions (cards up to 6, else rows)
//   3. sidebar (~1/3, full height): "Pending approvals" (the one-tap flow, when the mode is manual or a proposal
//      waits), Risk (kill-switch bars), Activity (fills the rest, scrolls), Rejected today
//   4. TodayTimeline as one row of chips.
// `?range=run` and `?expand=12,15` live in the URL beside `?proposal=` (the Telegram deep link: a pending
// proposal is highlighted, a decided one opens its panel). Each part fails on its own (plan S15): its panel shows
// the error with Retry; a whole-request failure keeps the frame and the last good data under the error box.
// The only actions here are Approve/Reject of a pending proposal; the engine controls are on Control.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import type { LiveQuery } from "../api/client";
import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import type { DecisionOut, LiveOut, LiveRange } from "../api/types";
import { Button, ErrorBox, Loading } from "../components/ui";
import { skewFrom } from "../layout/serverTime";
import { fmtDuration } from "../lib/format";
import { parseId } from "../lib/params";
import { useLiveUpdates } from "../live/useLiveUpdates";
import "./dashboard/dashboard.css";
import "./live/dash.css";
import { proposalHeadline } from "./dashboard/labels";
import PendingProposal from "./dashboard/PendingProposal";
import ProposalPanel from "./dashboard/ProposalPanel";
import { ActivityFeed } from "./live/ActivityFeed";
import { EquityChart } from "./live/EquityChart";
import { Panel, PanelDensity } from "./live/Panel";
import { orderedPeriods } from "./live/PeriodPnl";
import { PositionsTable } from "./live/PositionsTable";
import { RejectionsPanel } from "./live/RejectionsPanel";
import { RiskPanel } from "./live/RiskPanel";
import { TodayTimeline } from "./live/TodayTimeline";
import { TopBar, partError } from "./live/TopBar";

/** At most this many positions are expanded (and sent as `?expand=`: a 4th id makes `/api/live` answer 422). */
export const EXPAND_MAX = 3;
/** One id as `/api/live` accepts it (`[1-9][0-9]{0,18}`). */
const EXPAND_ID = /^[1-9][0-9]{0,18}$/;

export const TELEGRAM_OFF_NOTICE = "Telegram is not configured: approve here";
export const NOTHING_PENDING = "Nothing waiting for approval";

/** The URL's `?range=`: `run` or else `today`. */
export function parseRange(value: string | null): LiveRange {
  return value === "run" ? "run" : "today";
}

/** The URL's `?expand=`: the valid, distinct position ids, the first `EXPAND_MAX` only (the rest dropped). */
export function parseExpand(value: string | null): number[] {
  if (!value) return [];
  const ids: number[] = [];
  for (const part of value.split(",")) {
    const raw = part.trim();
    if (!EXPAND_ID.test(raw)) continue;
    const id = Number(raw);
    if (!Number.isSafeInteger(id) || ids.includes(id)) continue;
    ids.push(id);
    if (ids.length === EXPAND_MAX) break;
  }
  return ids;
}

/** The `api.live` query for a range and the expanded ids (`expand` only when there are any). */
export function liveQuery(range: LiveRange, expand: readonly number[]): LiveQuery {
  const ids = expand.slice(0, EXPAND_MAX);
  return ids.length > 0 ? { range, expand: ids.join(",") } : { range };
}

function DecisionNotices({ decisions, onDismiss }: { decisions: DecisionOut[]; onDismiss: (id: number) => void }) {
  if (decisions.length === 0) return null;
  return (
    <ul className="plain-list decisions" aria-label="Decisions">
      {decisions.map((d) => (
        <li key={d.proposal.id} className="card row decision">
          <span className="grow">{`${d.proposal.ticker} ${proposalHeadline(d.proposal.kind)}: ${d.message}`}</span>
          <Button onClick={() => onDismiss(d.proposal.id)}>Dismiss</Button>
        </li>
      ))}
    </ul>
  );
}

/** Page-level notices: a stale or stopped worker (to Control), and positions without a working stop order. */
function Notices({ live }: { live: LiveOut }) {
  const unprotected = (live.positions ?? []).filter((p) => !p.stop_working);
  const workerProblem = live.worker_stale || !live.worker.ok;
  if (!workerProblem && unprotected.length === 0) return null;
  return (
    <div className="live-notices" aria-label="Notices" role="group">
      {workerProblem && (
        <p className="live-notice status-bad">
          <span>{live.worker_stale ? "The worker's heartbeat is stale." : "The worker reports a problem."}</span>{" "}
          <Link className="link-touch" to="/control">
            Check Control
          </Link>
        </p>
      )}
      {unprotected.map((p) => (
        <p key={p.id} className="live-notice status-bad" data-testid={`unprotected-${p.id}`}>
          <span>{`${p.ticker}: no working stop order`}</span>
          {p.unprotected_seconds > 0 && <span>{` (unprotected ${fmtDuration(p.unprotected_seconds)})`}</span>}{" "}
          <Link className="link-touch" to={`/trades?position=${p.id}`}>
            {`Open ${p.ticker}`}
          </Link>
        </p>
      ))}
    </div>
  );
}

function PendingApprovals({
  live,
  updatedAt,
  proposalId,
  onRetry,
}: {
  live: LiveOut;
  updatedAt: number;
  proposalId: number | null;
  onRetry: () => void;
}) {
  const [decisions, setDecisions] = useState<DecisionOut[]>([]);
  const panelRef = useRef<HTMLDivElement>(null);
  const pending = live.pending;
  const skew = updatedAt ? skewFrom(live.server_time, updatedAt) : 0;
  const pendingIds = new Set((pending ?? []).map((p) => p.id));
  const showPanel = proposalId !== null && pending !== null && !pendingIds.has(proposalId);

  // A Telegram link to a decided proposal: bring its panel into view (a pending card scrolls itself).
  useEffect(() => {
    if (showPanel) panelRef.current?.scrollIntoView?.({ block: "start" });
  }, [showPanel, proposalId]);

  const shown = live.approval_mode === "manual" || pending === null || pending.length > 0 || decisions.length > 0 || showPanel;
  if (!shown) return null;

  const remember = (result: DecisionOut) =>
    setDecisions((prev) => [result, ...prev.filter((d) => d.proposal.id !== result.proposal.id)]);
  const notices = decisions.filter((d) => !pendingIds.has(d.proposal.id));

  return (
    <div className="stack live-pending">
      <DecisionNotices decisions={notices} onDismiss={(id) => setDecisions((prev) => prev.filter((d) => d.proposal.id !== id))} />
      {showPanel && (
        <div ref={panelRef}>
          <ProposalPanel id={proposalId} />
        </div>
      )}
      <Panel title="Pending approvals" error={pending === null ? (partError(live, "pending") ?? "Pending proposals not available") : null} onRetry={onRetry}>
        {pending !== null && (
          <div className="stack">
            {!live.telegram_configured && <p className="live-notice status-warn">{TELEGRAM_OFF_NOTICE}</p>}
            {pending.length === 0 ? (
              <p className="panel-empty muted">{NOTHING_PENDING}</p>
            ) : (
              pending.map((p) => (
                <PendingProposal key={p.id} proposal={p} serverSkewMs={skew} highlighted={p.id === proposalId} onDecided={remember} />
              ))
            )}
          </div>
        )}
      </Panel>
    </div>
  );
}

function LiveBody({
  live,
  updatedAt,
  connected,
  range,
  expand,
  proposalId,
  onRange,
  onExpand,
  onRetry,
}: {
  live: LiveOut;
  updatedAt: number;
  connected: boolean;
  range: LiveRange;
  expand: number[];
  proposalId: number | null;
  onRange: (r: LiveRange) => void;
  onExpand: (ids: number[]) => void;
  onRetry: () => void;
}) {
  const err = (part: string) => partError(live, part);
  const today = orderedPeriods(live.periods).find((p) => p.period === "today");
  const lastPoint = live.equity?.points[live.equity.points.length - 1];
  const now = live.risk?.equity ?? lastPoint?.equity ?? null;
  return (
    <PanelDensity.Provider value="dense">
      <TopBar live={live} connected={connected} updatedAt={updatedAt} onRetry={onRetry} />
      <Notices live={live} />
      <div className={live.equity !== null && live.equity.points.length > 0 ? "dash-grid has-chart" : "dash-grid"}>
        <div className="dash-main">
          <EquityChart
            equity={live.equity}
            range={range}
            onRange={onRange}
            error={err("equity")}
            onRetry={onRetry}
            now={now}
            changeToday={today?.pnl_after_fees ?? null}
            tall
          />
          <PositionsTable
            positions={live.positions}
            closedToday={live.closed_today}
            staleAfterSeconds={live.marks_stale_seconds}
            expanded={expand}
            onExpand={onExpand}
            error={err("positions")}
            onRetry={onRetry}
            session={live.session}
            timeline={live.timeline}
          />
        </div>
        <aside className="dash-side" aria-label="Approvals, risk and activity">
          <div className="dash-side-inner">
            <PendingApprovals live={live} updatedAt={updatedAt} proposalId={proposalId} onRetry={onRetry} />
            <RiskPanel risk={live.risk} error={err("risk")} onRetry={onRetry} />
            <ActivityFeed items={live.activity} error={err("activity")} onRetry={onRetry} />
            <RejectionsPanel rejections={live.rejections} error={err("rejections")} onRetry={onRetry} />
          </div>
        </aside>
      </div>
      <TodayTimeline timeline={live.timeline} session={live.session} error={err("timeline")} onRetry={onRetry} />
    </PanelDensity.Provider>
  );
}

export default function DashboardPage() {
  const api = useApi();
  const { connected } = useLiveUpdates();
  const [params, setParams] = useSearchParams();
  const proposalId = parseId(params.get("proposal"));
  const range = parseRange(params.get("range"));
  const expand = parseExpand(params.get("expand"));
  const q = liveQuery(range, expand);
  // The previous answer stays on screen while a new range or expansion loads (no blank page).
  const live = useQuery({ queryKey: qk.live(q), queryFn: () => api.live(q), placeholderData: keepPreviousData });
  const retry = () => void live.refetch();

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };
  const onRange = (r: LiveRange) => setParam("range", r === "run" ? "run" : null);
  const onExpand = (ids: number[]) => {
    const kept = parseExpand(ids.join(","));
    setParam("expand", kept.length > 0 ? kept.join(",") : null);
  };

  return (
    <main className="page live-page dash-page">
      <h1 className="dash-h1">Dashboard</h1>
      {live.isError && <ErrorBox error={live.error} onRetry={retry} />}
      {live.data ? (
        <LiveBody
          live={live.data}
          updatedAt={live.dataUpdatedAt}
          connected={connected}
          range={range}
          expand={expand}
          proposalId={proposalId}
          onRange={onRange}
          onExpand={onExpand}
          onRetry={retry}
        />
      ) : (
        live.isPending && <Loading />
      )}
    </main>
  );
}
