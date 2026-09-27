// A proposal a deep link (`/dashboard?proposal=<id>`) points at that is no longer pending: its final
// status, when and how it was decided, and any error (P4-T13).
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import { ErrorBox, Loading } from "../../components/ui";
import { fmtDateTime, fmtMoney, fmtPrice } from "../../lib/format";
import { orderLine, proposalHeadline } from "./labels";

export default function ProposalPanel({ id }: { id: number }) {
  const api = useApi();
  const q = useQuery({ queryKey: qk.proposal(id), queryFn: () => api.proposal(id) });
  const p = q.data;

  return (
    <section className="card proposal-panel" aria-label={`Proposal ${id}`}>
      <header className="card-head">
        <h2 className="card-title">{p ? `Proposal ${id}: ${proposalHeadline(p.kind)} ${p.ticker}` : `Proposal ${id}`}</h2>
        <Link className="link-touch" to="/dashboard">
          Close
        </Link>
      </header>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : p ? (
        <div className="stack">
          <p className="num">{`Status: ${p.status}`}</p>
          <p className="small num">
            {`${p.side.toUpperCase()} ${p.qty} · ${orderLine(p)}`}
            {p.stop_loss !== null && ` · Stop loss ${fmtPrice(p.stop_loss)}`}
            {p.risk_usd !== null && ` · Risk ${fmtMoney(p.risk_usd)}`}
          </p>
          <p className="small">
            {p.decided_at
              ? `Decided ${fmtDateTime(p.decided_at)} via ${p.decided_via ?? "n/a"} by ${p.decided_by ?? "n/a"}`
              : `Not decided (expires ${fmtDateTime(p.expires_at)})`}
          </p>
          {p.error && <p className="small text-tone tone-bad">{`Error: ${p.error}`}</p>}
          <p className="small muted">{p.reason}</p>
        </div>
      ) : null}
    </section>
  );
}
