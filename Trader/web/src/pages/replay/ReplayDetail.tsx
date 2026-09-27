// One replay (`/replay?id=<id>`, SPEC §12 Replay): the header (label, range, status, data mode, catalyst mode,
// half spread), progress with Cancel while queued or running, the warnings (biased days, forced closes, missing
// bars, the error of a failed run), the pinned strategies, and when finished the comparison with the live run
// over the same dates and the equity curve; the replay's trades and its last events.
// Live updates: the `replays` SSE topic invalidates this query (TOPIC_KEYS); while the run is queued or running
// and the stream is down it refetches every 5 s.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, type ReactNode } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { ReplayOut, TradeOut } from "../../api/types";
import { Badge, Card, Empty, ErrorBox, Loading, Table } from "../../components/ui";
import { useLiveUpdates } from "../../live/useLiveUpdates";
import { fmtDate, fmtDateTime, fmtMoney, fmtPrice, fmtR } from "../../lib/format";
import { EquityChart } from "../performance/EquityChart";
import { CompareTable } from "./CompareTable";
import { replayLabel } from "./ReplayList";
import { ReplayProgress } from "./ReplayProgress";
import { isActive, STATUS_TONE } from "./shared";

/** How often an active replay is refetched while the live stream is down. */
export const DISCONNECTED_REPLAY_REFETCH_MS = 5_000;
/** How many of the replay's trades are listed. */
export const REPLAY_TRADES_LIMIT = 100;

/** The detail query's `refetchInterval`: 5 s while queued or running and the stream is down, else off. */
export function replayRefetchInterval(replay: ReplayOut | undefined, connected: boolean): number | false {
  return replay !== undefined && isActive(replay.status) && !connected ? DISCONNECTED_REPLAY_REFETCH_MS : false;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function pnlClass(pnl: string): string {
  if (pnl.trim().startsWith("-")) return "num tone-bad";
  return /[1-9]/.test(pnl) ? "num tone-ok" : "num";
}

function Warnings({ replay }: { replay: ReplayOut }) {
  const p = replay.progress;
  const items: ReactNode[] = [];
  if (replay.status === "failed" && replay.error) items.push(<p className="tone-bad">{`Failed: ${replay.error}`}</p>);
  if (p.biased_days.length > 0) {
    items.push(
      <p>
        {`${plural(p.biased_days.length, "biased day", "biased days")} (today's universe was used): ${p.biased_days.map(fmtDate).join(", ")}`}
      </p>,
    );
  }
  if (p.forced_closes > 0) items.push(<p>{`${plural(p.forced_closes, "forced close", "forced closes")} at the end of a day`}</p>);
  if (p.missing_opening_bars > 0 || p.missing_minute_bars > 0) {
    items.push(
      <p>
        {`Missing data: ${plural(p.missing_opening_bars, "opening bar", "opening bars")} and ${plural(p.missing_minute_bars, "symbol-day", "symbol-days")} of 1-minute bars`}
      </p>,
    );
  }
  if (items.length === 0 && !replay.biased) return null;
  return (
    <section className="card stack tone-warn" aria-label="Warnings">
      {replay.biased && (
        <span>
          <Badge tone="warn">biased universe</Badge>
        </span>
      )}
      {items.map((item, i) => (
        <div key={i} className="small">
          {item}
        </div>
      ))}
    </section>
  );
}

function Strategies({ replay }: { replay: ReplayOut }) {
  return (
    <section className="card stack" aria-label="Strategies">
      <h3>Strategies</h3>
      <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {replay.strategies.map((s) => (
          <li key={s.key} className="row">
            <strong>{s.key}</strong>
            <span className="small muted">{`v${s.version} · revision ${s.revision}`}</span>
            {s.scope === "replay" && <Badge tone="info">override</Badge>}
            {!s.enabled && <Badge tone="muted">disabled</Badge>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function TradesTable({ trades }: { trades: TradeOut[] }) {
  return (
    <Table aria-label="Replay trades">
      <thead>
        <tr>
          <th>Date</th>
          <th>Ticker</th>
          <th className="num">Entry</th>
          <th className="num">Exit</th>
          <th className="num">R</th>
          <th className="num">P&amp;L</th>
          <th>Exit reason</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td>{fmtDate(t.session_date)}</td>
            <td>{t.ticker}</td>
            <td className="num">{fmtPrice(t.entry_price)}</td>
            <td className="num">{fmtPrice(t.exit_price)}</td>
            <td className="num">{fmtR(t.pnl_r)}</td>
            <td className={pnlClass(t.pnl)}>{fmtMoney(t.pnl)}</td>
            <td>{t.exit_reason}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function Results({ replay }: { replay: ReplayOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const run = String(replay.id);
  const tradesQuery = { run, limit: REPLAY_TRADES_LIMIT };
  const equityQuery = { run };
  const finished = replay.metrics !== null;
  const trades = useQuery({ queryKey: qk.trades(tradesQuery), queryFn: () => api.trades(tradesQuery), enabled: replay.status !== "queued" });
  const equity = useQuery({ queryKey: qk.equity(equityQuery), queryFn: () => api.equity(equityQuery), enabled: finished });

  // Replay rows never move the trading topics' SSE watermarks, so refresh the trades and the equity curve when
  // the replay itself moves on (a new day done, or a new status).
  const step = `${replay.status}:${replay.progress.sessions_done}`;
  const lastStep = useRef(step);
  useEffect(() => {
    if (lastStep.current === step) return;
    lastStep.current = step;
    void queryClient.invalidateQueries({ queryKey: qk.trades(tradesQuery) });
    void queryClient.invalidateQueries({ queryKey: qk.equity(equityQuery) });
  }, [step]); // keyed on the replay's step only

  const points = equity.data?.points ?? [];
  return (
    <>
      {replay.metrics ? (
        <Card title="Compared with live">
          <CompareTable replay={replay.metrics} live={replay.live_metrics} />
        </Card>
      ) : isActive(replay.status) ? (
        <p className="small muted">The comparison with the live run appears when the replay has finished.</p>
      ) : null}
      {finished &&
        (equity.isError ? (
          <ErrorBox error={equity.error} onRetry={() => void equity.refetch()} />
        ) : points.length > 0 ? (
          <Card title="Equity">
            <EquityChart points={points} />
          </Card>
        ) : null)}
      {replay.status !== "queued" && (
        <Card title="Trades">
          {trades.isPending ? (
            <Loading />
          ) : trades.isError ? (
            <ErrorBox error={trades.error} onRetry={() => void trades.refetch()} />
          ) : trades.data.items.length === 0 ? (
            <Empty>No trades yet.</Empty>
          ) : (
            <TradesTable trades={trades.data.items} />
          )}
        </Card>
      )}
    </>
  );
}

function Events({ replay }: { replay: ReplayOut }) {
  return (
    <Card title="Last events">
      {replay.events.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <ul className="stack" aria-label="Events" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {replay.events.map((e) => (
            <li key={e.id} className="stack small" style={{ gap: 2 }}>
              <span className="row">
                <Badge tone={e.level === "error" || e.level === "critical" ? "bad" : e.level === "warning" ? "warn" : "muted"}>{e.level}</Badge>
                <span className="muted">{fmtDateTime(e.ts)}</span>
                <span className="muted">{e.source}</span>
              </span>
              <span>{e.message}</span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function ReplayDetail({ id }: { id: number }) {
  const api = useApi();
  const { connected } = useLiveUpdates();
  const q = useQuery({
    queryKey: qk.replay(id),
    queryFn: () => api.replay(id),
    refetchInterval: (query) => replayRefetchInterval(query.state.data, connected),
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  const r = q.data;
  const started = `Started ${fmtDateTime(r.created_at)}`;
  return (
    <>
      <section className="card stack" aria-label={`Replay ${r.id}`}>
        <h2>{replayLabel(r)}</h2>
        <div className="row">
          <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
          <span>{`${fmtDate(r.date_from)} → ${fmtDate(r.date_to)}`}</span>
        </div>
        <p className="small muted">{`Data: ${r.data_mode} · catalysts: ${r.catalyst_mode} · half spread ${r.half_spread_bps} bps`}</p>
        <p className="small muted">{r.finished_at ? `${started}, finished ${fmtDateTime(r.finished_at)}` : started}</p>
        {isActive(r.status) && <ReplayProgress replay={r} />}
      </section>
      <Warnings replay={r} />
      <Strategies replay={r} />
      <Results replay={r} />
      <Events replay={r} />
    </>
  );
}
