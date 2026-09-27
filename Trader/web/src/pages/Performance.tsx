// Performance (SPEC §12, BR-51, BR-52, BR-62): run and date filters, metric tiles, the equity curve and
// drawdown on one time axis, the R histogram, and the trades CSV export with the same filters.
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { useApi, type RunRangeQuery } from "../api/client";
import { qk } from "../api/queryKeys";
import { Card, Empty, ErrorBox, Loading, Button } from "../components/ui";
import { DrawdownChart } from "./performance/DrawdownChart";
import { EquityChart } from "./performance/EquityChart";
import { MetricTiles } from "./performance/MetricTiles";
import { RHistogram } from "./performance/RHistogram";

export const LIVE_RUN = "live";

/** `live` or a run number; anything else is refused. */
export function validRun(value: string): string | null {
  const v = value.trim().toLowerCase();
  if (v === "" || v === LIVE_RUN) return LIVE_RUN;
  return /^\d{1,15}$/.test(v) && Number(v) > 0 ? String(Number(v)) : null;
}

/** The query for a run and range; `live` and empty dates are left out (the server's defaults). */
export function runRangeQuery(run: string, from: string, to: string): RunRangeQuery {
  return {
    ...(run !== LIVE_RUN ? { run } : {}),
    ...(from ? { from } : {}),
    ...(to ? { to } : {}),
  };
}

export default function PerformancePage() {
  const api = useApi();
  const [run, setRun] = useState(LIVE_RUN);
  const [runText, setRunText] = useState(LIVE_RUN);
  const [runError, setRunError] = useState<string | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const query = runRangeQuery(run, from, to);
  const metrics = useQuery({ queryKey: qk.metrics(query), queryFn: () => api.metrics(query) });
  const equity = useQuery({ queryKey: qk.equity(query), queryFn: () => api.equity(query) });

  const applyRun = (e: FormEvent) => {
    e.preventDefault();
    const v = validRun(runText);
    if (v === null) {
      setRunError('Run is "live" or a run number.');
      return;
    }
    setRunError(null);
    setRunText(v);
    setRun(v);
  };

  const points = equity.data?.points ?? [];
  const empty = metrics.data !== undefined && metrics.data.trades === 0;

  return (
    <main className="page">
      <h1>Performance</h1>
      <Card>
        <form className="row" style={{ flexWrap: "wrap", gap: 8, alignItems: "flex-end" }} onSubmit={applyRun}>
          <label>
            Run
            <input
              value={runText}
              onChange={(e) => setRunText(e.target.value)}
              inputMode="text"
              autoComplete="off"
              style={{ width: "7em" }}
            />
          </label>
          <Button type="submit">Show</Button>
          <label>
            From
            <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label>
            To
            <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          <a className="btn btn-plain" style={{ minHeight: 44, display: "inline-flex", alignItems: "center" }} href={api.exportTradesUrl(query)} download>
            Export CSV
          </a>
        </form>
        {runError && (
          <p className="tone-bad small" role="status">
            {runError}
          </p>
        )}
      </Card>

      {metrics.isPending ? (
        <Loading />
      ) : metrics.isError ? (
        <ErrorBox error={metrics.error} onRetry={() => void metrics.refetch()} />
      ) : empty ? (
        <Empty>No trades yet</Empty>
      ) : (
        <Card title="Metrics">
          <MetricTiles metrics={metrics.data} />
        </Card>
      )}

      {equity.isError ? (
        <ErrorBox error={equity.error} onRetry={() => void equity.refetch()} />
      ) : points.length > 0 ? (
        <Card title="Equity and drawdown">
          <EquityChart points={points} />
          <DrawdownChart points={points} />
        </Card>
      ) : null}

      {metrics.data && !empty && metrics.data.r_histogram.length > 0 && (
        <Card title="R multiples">
          <RHistogram bins={metrics.data.r_histogram} />
        </Card>
      )}
    </main>
  );
}
