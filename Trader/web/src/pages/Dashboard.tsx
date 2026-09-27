// The Dashboard (P4-T13; SPEC §12; BR-30, BR-31, BR-33, BR-50): today at a glance and the one-tap
// approvals away from Telegram.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import type { DashboardOut, DecisionOut } from "../api/types";
import { Badge, Button, Empty, ErrorBox, Light, Loading } from "../components/ui";
import { fmtDate } from "../lib/format";
import "./dashboard/dashboard.css";
import EventList from "./dashboard/EventList";
import KillSwitchLights from "./dashboard/KillSwitchLights";
import { phaseLabel, proposalHeadline } from "./dashboard/labels";
import PendingProposal from "./dashboard/PendingProposal";
import PnlTiles from "./dashboard/PnlTiles";
import PositionCard from "./dashboard/PositionCard";
import ProposalPanel from "./dashboard/ProposalPanel";
import Timeline from "./dashboard/Timeline";

/** A positive whole-number id from a query parameter, else null. */
function parseId(raw: string | null): number | null {
  if (!raw || !/^\d{1,15}$/.test(raw)) return null;
  const id = Number(raw);
  return id > 0 ? id : null;
}

function HeaderRow({ data }: { data: DashboardOut }) {
  const problem = !data.token.ok || !data.worker.ok;
  return (
    <section className="card dash-header" aria-label="Session">
      <div className="row">
        <strong className="num">{fmtDate(data.session.date)}</strong>
        <span>{phaseLabel(data.session.phase)}</span>
        {data.approval_mode === "auto" ? <Badge tone="warn">AUTO</Badge> : <Badge tone="info">MANUAL</Badge>}
      </div>
      <div className="row">
        <Light tone={data.token.ok ? "ok" : "bad"} label={data.token.ok ? "Token OK" : "Token problem"} />
        <Light tone={data.worker.ok ? "ok" : "bad"} label={data.worker.ok ? "Worker OK" : "Worker problem"} />
        {problem && (
          <Link className="link-touch" to="/system">
            Check System
          </Link>
        )}
      </div>
      {!data.telegram_configured && <p className="notice text-tone tone-warn">Telegram is not configured: approve here</p>}
    </section>
  );
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

function CandidatesSummary({ data }: { data: DashboardOut }) {
  const top = data.candidates_top.slice(0, 5);
  return (
    <section className="card" aria-label="Candidates today">
      <header className="card-head">
        <h2 className="card-title">Candidates</h2>
        <Link className="link-touch" to="/candidates">
          View all
        </Link>
      </header>
      <p className="small muted">{`${data.candidates_count} ranked today`}</p>
      {top.length > 0 && (
        <ol className="plain-list row small">
          {top.map((c) => (
            <li key={c.id} className={c.passed ? "chip" : "chip muted"}>
              {`${c.rank ?? "–"}. ${c.ticker}`}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function DashboardBody({ data, updatedAt, proposalId }: { data: DashboardOut; updatedAt: number; proposalId: number | null }) {
  const [decisions, setDecisions] = useState<DecisionOut[]>([]);
  const parsed = Date.parse(data.server_time);
  const skew = Number.isNaN(parsed) || !updatedAt ? 0 : parsed - updatedAt;
  const pendingIds = new Set(data.pending.map((p) => p.id));
  const showPanel = proposalId !== null && !pendingIds.has(proposalId);

  function remember(result: DecisionOut) {
    setDecisions((prev) => [result, ...prev.filter((d) => d.proposal.id !== result.proposal.id)]);
  }

  return (
    <>
      <HeaderRow data={data} />
      <DecisionNotices
        decisions={decisions.filter((d) => !pendingIds.has(d.proposal.id))}
        onDismiss={(id) => setDecisions((prev) => prev.filter((d) => d.proposal.id !== id))}
      />
      {showPanel && <ProposalPanel id={proposalId} />}
      <section className="stack" aria-label="Pending approvals">
        <h2 className="section-title">Pending approvals</h2>
        {data.pending.length === 0 ? (
          <Empty>Nothing waiting for approval</Empty>
        ) : (
          data.pending.map((p) => (
            <PendingProposal key={p.id} proposal={p} serverSkewMs={skew} highlighted={p.id === proposalId} onDecided={remember} />
          ))
        )}
      </section>
      <section className="stack" aria-label="Open positions">
        <h2 className="section-title">Open positions</h2>
        {data.positions.length === 0 ? (
          <Empty>No open positions</Empty>
        ) : (
          <div className="grid">
            {data.positions.map((p) => (
              <PositionCard key={p.id} position={p} />
            ))}
          </div>
        )}
      </section>
      <PnlTiles pnl={data.pnl} />
      <div className="grid">
        <KillSwitchLights switches={data.killswitches} />
        <section className="card" aria-label="Today">
          <h2 className="card-title">Today</h2>
          <Timeline items={data.timeline} />
        </section>
      </div>
      <CandidatesSummary data={data} />
      <section className="card" aria-label="Latest events">
        <h2 className="card-title">Latest events</h2>
        <EventList events={data.events} />
      </section>
    </>
  );
}

export default function DashboardPage() {
  const api = useApi();
  const [params] = useSearchParams();
  const proposalId = parseId(params.get("proposal"));
  const q = useQuery({ queryKey: qk.dashboard(), queryFn: () => api.dashboard() });

  return (
    <main className="page dashboard">
      <h1>Dashboard</h1>
      {q.isError && <ErrorBox error={q.error} onRetry={() => void q.refetch()} />}
      {q.data ? (
        <DashboardBody data={q.data} updatedAt={q.dataUpdatedAt} proposalId={proposalId} />
      ) : (
        q.isPending && <Loading />
      )}
    </main>
  );
}
