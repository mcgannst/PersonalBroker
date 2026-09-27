// Journal (SPEC §12, BR-60): the last 30 session days (newest first) with trades, realized P&L, the
// Rules-followed answer and notes. Tapping a day, or opening `/journal?date=YYYY-MM-DD` (the daily-summary
// link), opens that day's editor.
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import type { JournalDayOut } from "../api/types";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";
import { fmtDate, fmtMoney } from "../lib/format";
import { isIsoDate } from "./performance/dates";
import { JournalEditor } from "./performance/JournalEditor";

const VIA_LABELS: Record<string, string> = { telegram: "Telegram", web: "web" };

/** `Yes (Telegram)`, `No (web)`, `Not answered`. */
export function answerText(day: Pick<JournalDayOut, "rules_followed" | "answered_via">): string {
  if (day.rules_followed === null) return "Not answered";
  const answer = day.rules_followed ? "Yes" : "No";
  const via = day.answered_via ? (VIA_LABELS[day.answered_via] ?? day.answered_via) : null;
  return via ? `${answer} (${via})` : answer;
}

export function tradesText(n: number): string {
  return `${n} ${n === 1 ? "trade" : "trades"}`;
}

export default function JournalPage() {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const date = params.get("date");
  const q = useQuery({ queryKey: qk.journal(), queryFn: () => api.journal({}) });
  const days = q.data?.items ?? [];

  const open = (d: string) => setParams({ date: d });
  const close = () => setParams({});

  return (
    <main className="page">
      <h1>Journal</h1>
      {date !== null && !isIsoDate(date) && <Empty>“{date}” is not a date (use YYYY-MM-DD).</Empty>}
      {date !== null && isIsoDate(date) && !q.isPending && (
        <JournalEditor key={date} date={date} day={days.find((d) => d.session_date === date)} onClose={close} />
      )}
      <Card title="Last 30 sessions">
        {q.isPending ? (
          <Loading />
        ) : q.isError ? (
          <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
        ) : days.length === 0 ? (
          <Empty>No session days yet.</Empty>
        ) : (
          <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {days.map((d) => (
              <li key={d.session_date} className={d.session_date === date ? "is-selected" : undefined}>
                <button
                  type="button"
                  className="btn btn-plain"
                  onClick={() => open(d.session_date)}
                  style={{ width: "100%", minHeight: 44, textAlign: "left", display: "block" }}
                >
                  <span className="row" style={{ justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
                    <strong>{fmtDate(d.session_date)}</strong>
                    <span>{tradesText(d.trades)}</span>
                    <span className="num">{d.realized_pnl === null ? "" : fmtMoney(d.realized_pnl)}</span>
                    <span className={d.rules_followed === false ? "tone-bad" : d.rules_followed ? "tone-ok" : "muted"}>
                      {answerText(d)}
                    </span>
                  </span>
                  {d.notes && <span className="small muted" style={{ display: "block", whiteSpace: "pre-wrap" }}>{d.notes}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </main>
  );
}
