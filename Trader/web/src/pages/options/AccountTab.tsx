// Options, Account tab (OPTSIM-T15): the account value at liquidation marks as the headline, the cash figures,
// premium collected as a secondary figure, the result per source and the benchmark comparison. It warns when
// the marks are incomplete or the options worker has gone quiet.
import { useQuery } from "@tanstack/react-query";

import { OPT_FAST_MS, oqk, useOptionsApi } from "../../api/optionsClient";
import type { OptAccountOut } from "../../api/types";
import { Card, Empty, Loading, Stat, Table } from "../../components/ui";
import { fmtDate, fmtDateTime, fmtMoney, fmtPct, fmtRate } from "../../lib/format";
import { serverSkewMs } from "../../layout/serverTime";
import { OptErrorBox } from "./shared";

/** The worker is reported as quiet when its last heartbeat is older than this. */
export const WORKER_QUIET_MS = 2 * 60_000;

/** The warnings to show for an account, in order. `nowMs` is the server's "now". */
export function accountWarnings(account: OptAccountOut, nowMs: number): string[] {
  const out: string[] = [];
  if (!account.marks_complete) {
    out.push("Some open positions have no fresh mark, so the account value and the unrealized result are incomplete.");
  }
  const beat = account.worker_beat_at ? Date.parse(account.worker_beat_at) : Number.NaN;
  if (Number.isNaN(beat)) {
    out.push("The options worker has not reported yet: orders will not fill and marks will not update.");
  } else if (nowMs - beat > WORKER_QUIET_MS) {
    out.push(`The options worker last reported at ${fmtDateTime(account.worker_beat_at)}: orders will not fill and marks are not updating.`);
  }
  return out;
}

export function AccountView({ account, nowMs }: { account: OptAccountOut; nowMs: number }) {
  const warnings = accountWarnings(account, nowMs);
  const bench = account.benchmark;
  return (
    <div className="stack opt-gap">
      {warnings.map((w) => (
        <p key={w} className="opt-notice tone-warn" role="alert">
          {w}
        </p>
      ))}
      <Card>
        <div className="opt-headline">
          <div className="stat-label">Account value</div>
          <div className="opt-headline-value num" data-testid="opt-account-value">
            {fmtMoney(account.account_value)}
          </div>
          <div className="stat-sub">
            At liquidation marks{account.marks_as_of ? `, as of ${fmtDateTime(account.marks_as_of)}` : ""} · started with {fmtMoney(account.starting_cash)}
          </div>
        </div>
        <div className="opt-stats">
          <Stat label="Cash" value={fmtMoney(account.cash)} />
          <Stat label="Reserved" value={fmtMoney(account.reserved)} sub="held as collateral" />
          <Stat label="Free cash" value={fmtMoney(account.free_cash)} />
          <Stat label="Positions value" value={fmtMoney(account.positions_value)} />
          <Stat label="Unrealized" value={fmtMoney(account.unrealized_pnl)} />
          <Stat label="Realized" value={fmtMoney(account.realized_pnl)} />
          <Stat label="Fees" value={fmtMoney(account.fees_total)} />
          <Stat label="Cap per underlying" value={fmtRate(account.max_position_pct)} sub="of account value" />
        </div>
        <p className="small muted">Premium collected: {fmtMoney(account.premium_collected)} (not a result: the account value above is)</p>
      </Card>
      <Card title="By source">
        {account.by_source.length === 0 ? (
          <Empty>Nothing traded yet</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Open</th>
                <th>Reserved</th>
                <th>Unrealized</th>
                <th>Realized</th>
                <th>Premium</th>
              </tr>
            </thead>
            <tbody>
              {account.by_source.map((s) => (
                <tr key={s.source}>
                  <td>{s.source}</td>
                  <td>{s.open_structures}</td>
                  <td>{fmtMoney(s.reserved)}</td>
                  <td>{fmtMoney(s.unrealized_pnl)}</td>
                  <td>{fmtMoney(s.realized_pnl)}</td>
                  <td>{fmtMoney(s.premium_collected)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
      <Card title="Benchmark">
        {bench ? (
          <div className="opt-stats">
            <Stat label="Account" value={fmtPct(bench.account_return)} sub={`since ${fmtDate(bench.since)}`} />
            <Stat label={`${bench.ticker} shares`} value={fmtPct(bench.benchmark_return)} sub="bought and held" />
          </div>
        ) : (
          <Empty>No benchmark yet</Empty>
        )}
      </Card>
    </div>
  );
}

export function AccountTab() {
  const api = useOptionsApi();
  const q = useQuery({ queryKey: oqk.account(), queryFn: () => api.optAccount(), refetchInterval: OPT_FAST_MS });
  if (q.isPending) return <Loading />;
  if (q.isError) return <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  if (q.data.run_id === null) return <OptErrorBox error={null} noRun />;
  return <AccountView account={q.data} nowMs={Date.now() + serverSkewMs()} />;
}
