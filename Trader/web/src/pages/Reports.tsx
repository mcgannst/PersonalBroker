// Reports (contract refinement 1: `/reports?week=YYYY-MM-DD`, the week-ending date the Telegram weekly link
// carries; default the current week): the Monday-Friday trading week containing that date, with its metrics,
// trades and each day's journal answer, headed by the week's Claude commentary (P5-T13), or why there is none.
// P6-T12: `/reports?day=YYYY-MM-DD[&run=<id>]` opens the "Day" view instead (the decision log of one session;
// `?day=` alone opens the latest day with rows). The week view is unchanged apart from its link to it.
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import type { JournalDayOut } from "../api/types";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";
import { fmtDate } from "../lib/format";
import { parseId } from "../lib/params";
import { answerText } from "./Journal";
import { addDays, isIsoDate, todayEt, tradingWeek, type TradingWeek } from "./performance/dates";
import { MetricTiles } from "./performance/MetricTiles";
import { Commentary } from "./reports/Commentary";
import { DayDecisions } from "./reports/DayDecisions";
import { TradeList } from "./trades/TradeList";

/** At most this many trades are listed for a week (a week has far fewer). */
export const WEEK_TRADES_LIMIT = 50;

function weekHref(date: string): string {
  return `/reports?week=${date}`;
}

function WeekReport({ week }: { week: TradingWeek }) {
  const api = useApi();
  const range = { from: week.monday, to: week.friday };
  const tradesQuery = { ...range, limit: WEEK_TRADES_LIMIT };
  const metrics = useQuery({ queryKey: qk.metrics(range), queryFn: () => api.metrics(range) });
  const trades = useQuery({ queryKey: qk.trades(tradesQuery), queryFn: () => api.trades(tradesQuery) });
  const journal = useQuery({ queryKey: qk.journal(range), queryFn: () => api.journal(range) });
  const byDate = new Map<string, JournalDayOut>((journal.data?.items ?? []).map((d) => [d.session_date, d]));

  return (
    <>
      <h2>
        Week of {week.monday} to {week.friday}
      </h2>
      <nav className="row" style={{ gap: 12, flexWrap: "wrap" }} aria-label="Weeks">
        <Link className="link-touch" to={weekHref(addDays(week.friday, -7))}>← Previous week</Link>
        <Link className="link-touch" to={weekHref(addDays(week.friday, 7))}>Next week →</Link>
        <Link className="link-touch" to="/reports?day=">Decisions by day</Link>
      </nav>
      <Commentary monday={week.monday} />

      <Card title="Metrics">
        {metrics.isPending ? (
          <Loading />
        ) : metrics.isError ? (
          <ErrorBox error={metrics.error} onRetry={() => void metrics.refetch()} />
        ) : metrics.data.trades === 0 ? (
          <Empty>No trades yet</Empty>
        ) : (
          <MetricTiles metrics={metrics.data} />
        )}
      </Card>

      <Card title="Trades">
        {trades.isPending ? (
          <Loading />
        ) : trades.isError ? (
          <ErrorBox error={trades.error} onRetry={() => void trades.refetch()} />
        ) : trades.data.items.length === 0 ? (
          <Empty>No trades this week.</Empty>
        ) : (
          <TradeList trades={trades.data.items} />
        )}
      </Card>

      <div role="region" aria-label="Journal">
        <Card title="Journal">
          {journal.isPending ? (
            <Loading />
          ) : journal.isError ? (
            <ErrorBox error={journal.error} onRetry={() => void journal.refetch()} />
          ) : (
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }} className="stack">
              {week.days.map((day) => {
                const row = byDate.get(day);
                return (
                  <li key={day} className="row" style={{ gap: 8, flexWrap: "wrap", minHeight: 44, alignItems: "center" }}>
                    <Link className="link-touch" to={`/journal?date=${day}`}>{fmtDate(day)}</Link>
                    <span>{row ? answerText(row) : "Not answered"}</span>
                    {row?.notes && <span className="small muted">{row.notes}</span>}
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

function DayView({ raw, run }: { raw: string; run: string | null }) {
  const runId = run === null ? null : parseId(run);
  if (run !== null && runId === null) return <Empty>“{run}” is not a run id.</Empty>;
  if (raw !== "" && !isIsoDate(raw)) return <Empty>“{raw}” is not a date (use YYYY-MM-DD).</Empty>;
  return <DayDecisions day={raw === "" ? null : raw} runId={runId} />;
}

export default function ReportsPage() {
  const [params] = useSearchParams();
  const day = params.get("day");
  const raw = params.get("week");
  const date = raw ?? todayEt();
  return (
    <main className="page">
      <h1>Reports</h1>
      {day !== null ? (
        <DayView raw={day} run={params.get("run")} />
      ) : isIsoDate(date) ? (
        <WeekReport week={tradingWeek(date)} />
      ) : (
        <Empty>“{raw}” is not a date (use YYYY-MM-DD).</Empty>
      )}
    </main>
  );
}
