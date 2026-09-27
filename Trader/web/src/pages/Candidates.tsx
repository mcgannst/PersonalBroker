// The Candidates page (P4-T13; SPEC §12): the pre-market brief, the catalyst cards and the 9:35 ranking
// for a session (`?date=YYYY-MM-DD`, default the current session).
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import type { CandidatesOut } from "../api/types";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";
import CatalystCard from "./dashboard/CatalystCard";
import "./dashboard/dashboard.css";
import RankingTable from "./dashboard/RankingTable";
import { isIsoDate } from "./performance/dates";

function CandidatesBody({ data }: { data: CandidatesOut }) {
  return (
    <>
      <Card title="Pre-market brief">{data.brief ? <pre className="brief">{data.brief}</pre> : <Empty>No brief for this session</Empty>}</Card>
      <section className="stack" aria-label="Catalysts">
        <h2 className="section-title">Catalysts</h2>
        {data.catalysts.length === 0 ? (
          <Empty>No catalysts for this session</Empty>
        ) : (
          <div className="grid">
            {data.catalysts.map((c) => (
              <CatalystCard key={`${c.symbol_id}-${c.session_date}`} catalyst={c} />
            ))}
          </div>
        )}
      </section>
      <Card title="Ranking">
        {data.ranking.length === 0 ? <Empty>No ranking for this session</Empty> : <RankingTable ranking={data.ranking} />}
      </Card>
    </>
  );
}

export default function CandidatesPage() {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const raw = params.get("date") || undefined;
  // A malformed ?date= never reaches the API (the same check as Journal and Reports).
  const badDate = raw !== undefined && !isIsoDate(raw);
  const date = badDate ? undefined : raw;
  const q = useQuery({ queryKey: qk.candidates(date), queryFn: () => api.candidates(date), enabled: !badDate });
  const pickerValue = date ?? q.data?.session_date ?? "";

  function changeDate(value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set("date", value);
    else next.delete("date");
    setParams(next);
  }

  return (
    <main className="page candidates">
      <h1>Candidates</h1>
      <div className="row">
        <label>
          Session date
          <input type="date" value={pickerValue} onChange={(e) => changeDate(e.target.value)} />
        </label>
      </div>
      {badDate ? (
        <Empty>“{raw}” is not a date (use YYYY-MM-DD).</Empty>
      ) : (
        <>
          {q.isError && <ErrorBox error={q.error} onRetry={() => void q.refetch()} />}
          {q.data ? <CandidatesBody data={q.data} /> : q.isPending && <Loading />}
        </>
      )}
    </main>
  );
}
