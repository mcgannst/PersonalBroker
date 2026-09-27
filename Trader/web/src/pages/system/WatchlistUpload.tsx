// Watchlist upload (P4-T10, SPEC §4.2 manual fallback): a CSV of tickers used instead of FinViz for one
// session. An uploaded list replaces FinViz for its session; deleting it goes back to FinViz.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi, type WatchlistUploadOptions } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { WatchlistOut, WatchlistUploadOut } from "../../api/types";
import { Button, Card, Empty, ErrorBox, Loading, Table, errorMessage } from "../../components/ui";
import { fmtDate, fmtDateTime } from "../../lib/format";

function UploadResult({ result }: { result: WatchlistUploadOut }) {
  const wl = result.watchlist;
  return (
    <div className="stack">
      <p className="tone-ok">{`${wl.tickers.length} tickers stored for ${fmtDate(wl.session_date)}.`}</p>
      {result.rejected.length > 0 && (
        <Table aria-label="Rejected rows">
          <thead>
            <tr>
              <th className="num">Row</th>
              <th>Value</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {result.rejected.map((r) => (
              <tr key={`${r.row}-${r.reason}`}>
                <td className="num">{r.row}</td>
                <td>{r.value}</td>
                <td>{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {result.launched && <p className="small">{result.launched.message}</p>}
    </div>
  );
}

function CurrentWatchlist({ date }: { date: string | undefined }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: qk.watchlist(date), queryFn: () => api.watchlist(date) });
  const remove = useMutation({
    mutationFn: (wl: WatchlistOut) => api.deleteWatchlist(wl.session_date),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} onRetry={() => void query.refetch()} />;
  const wl = query.data;
  if (!wl) return <Empty>No uploaded watchlist: the nightly job uses FinViz for this session.</Empty>;
  return (
    <div className="stack">
      <p>
        {`${wl.tickers.length} tickers for ${fmtDate(wl.session_date)}`}
        <span className="small muted">
          {` (${wl.filename ?? "no file name"}, uploaded ${fmtDateTime(wl.uploaded_at)} by ${wl.uploaded_by})`}
        </span>
      </p>
      <p className="small" style={{ overflowWrap: "anywhere" }}>
        {wl.tickers.join(", ")}
      </p>
      <p className="small muted">It replaces FinViz for this session. Delete it to go back to FinViz.</p>
      <div>
        <Button variant="danger" busy={remove.isPending} onClick={() => remove.mutate(wl)}>
          Delete
        </Button>
      </div>
      {remove.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(remove.error)}
        </p>
      )}
    </div>
  );
}

export function WatchlistUpload() {
  const api = useApi();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [date, setDate] = useState("");
  const [runNightly, setRunNightly] = useState(false);
  const upload = useMutation({
    mutationFn: ({ file, opts }: { file: File; opts: WatchlistUploadOptions }) => api.uploadWatchlist(file, opts),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      void queryClient.invalidateQueries({ queryKey: qk.system() });
    },
  });

  function submit() {
    if (!file) return;
    const opts: WatchlistUploadOptions = {};
    if (date) opts.date = date;
    if (runNightly) opts.runNightly = true;
    upload.mutate({ file, opts });
  }

  return (
    <Card title="Watchlist upload">
      <div className="stack">
        <p className="small muted">
          When FinViz fails, upload a CSV of tickers (a <code>ticker</code> or <code>symbol</code> column, or one ticker
          per row). The nightly job then uses it instead of FinViz for that session.
        </p>
        <div className="row">
          <label htmlFor="watchlist-file">CSV file</label>
          <input
            id="watchlist-file"
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            style={{ maxWidth: "100%" }}
          />
        </div>
        <div className="row">
          <label htmlFor="watchlist-date">Session date (optional)</label>
          <input
            id="watchlist-date"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            style={{ minHeight: 44 }}
          />
        </div>
        <p className="small muted">Leave the date empty for the next nightly.</p>
        <label className="check-row">
          <input type="checkbox" checked={runNightly} onChange={(e) => setRunNightly(e.target.checked)} />
          Run nightly now
        </label>
        <div>
          <Button variant="primary" busy={upload.isPending} disabled={!file} onClick={submit}>
            Upload
          </Button>
        </div>
        {upload.isSuccess && <UploadResult result={upload.data} />}
        {upload.isError && (
          <p className="small tone-bad" role="alert">
            {errorMessage(upload.error)}
          </p>
        )}
        <h3 className="small">Current watchlist</h3>
        <CurrentWatchlist date={date || undefined} />
      </div>
    </Card>
  );
}
