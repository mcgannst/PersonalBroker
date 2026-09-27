// The replay list (SPEC §12 Replay): newest first, each with its label, range, status (a running replay with
// its progress), a "biased universe" badge, trades, expectancy, total P&L and the created time in MT. Tapping
// a row opens `/replay?id=<id>`.
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { ReplaySummaryOut } from "../../api/types";
import { Badge, Empty, ErrorBox, Loading, Table } from "../../components/ui";
import { fmtDate, fmtDateTime, fmtMoney, fmtR } from "../../lib/format";
import { STATUS_TONE } from "./shared";

/** How many replays the list shows. */
export const REPLAY_LIST_LIMIT = 50;

export function replayHref(id: number): string {
  return `/replay?id=${id}`;
}

export function replayLabel(r: { id: number; label: string | null }): string {
  return r.label ?? `Replay ${r.id}`;
}

/** The progress of the running replay (the summary has none; one replay runs at a time, so one query). */
function RunningProgress({ id }: { id: number }) {
  const api = useApi();
  const q = useQuery({ queryKey: qk.replay(id), queryFn: () => api.replay(id) });
  if (!q.data) return null;
  const p = q.data.progress;
  return <span className="small muted num">{`${p.sessions_done}/${p.sessions_total}`}</span>;
}

function Row({ r }: { r: ReplaySummaryOut }) {
  const navigate = useNavigate();
  const href = replayHref(r.id);
  return (
    <tr onClick={() => navigate(href)} style={{ cursor: "pointer" }}>
      <td>
        <Link className="link-touch" to={href} onClick={(e) => e.stopPropagation()}>
          {replayLabel(r)}
        </Link>
      </td>
      <td>{`${fmtDate(r.date_from)} → ${fmtDate(r.date_to)}`}</td>
      <td>
        <span className="row">
          <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
          {r.status === "running" && <RunningProgress id={r.id} />}
          {r.biased && <Badge tone="warn">biased universe</Badge>}
        </span>
      </td>
      <td className="num">{r.trades}</td>
      <td className="num">{fmtR(r.expectancy_r)}</td>
      <td className="num">{fmtMoney(r.total_pnl)}</td>
      <td>{fmtDateTime(r.created_at)}</td>
    </tr>
  );
}

export function ReplayList() {
  const api = useApi();
  const query = { limit: REPLAY_LIST_LIMIT };
  const q = useQuery({ queryKey: qk.replays(query), queryFn: () => api.replays(query) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  if (q.data.items.length === 0) return <Empty>No replays yet.</Empty>;
  return (
    <Table aria-label="Replays">
      <thead>
        <tr>
          <th>Label</th>
          <th>Range</th>
          <th>Status</th>
          <th className="num">Trades</th>
          <th className="num">Expectancy</th>
          <th className="num">Total P&amp;L</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        {q.data.items.map((r) => (
          <Row key={r.id} r={r} />
        ))}
      </tbody>
    </Table>
  );
}
