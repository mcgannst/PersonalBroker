// The Replay page (SPEC §12 Replay; BR-54). Without `?id=`: the "New replay" button (disabled with the reason
// while a replay is running), the start form, and the list of replays. With `?id=<id>`: that replay's progress,
// results and comparison with the live run over the same dates. The `replays` SSE topic refreshes both.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useApi } from "../api/client";
import { qk } from "../api/queryKeys";
import { Button, Card, Empty, ErrorBox } from "../components/ui";
import { parseId } from "../lib/params";
import { ReplayDetail } from "./replay/ReplayDetail";
import { ReplayForm } from "./replay/ReplayForm";
import { ReplayList } from "./replay/ReplayList";

export const BUSY_REASON = "A replay is already running. Start a new one when it has finished.";

function ReplayHome() {
  const api = useApi();
  const [open, setOpen] = useState(false);
  const options = useQuery({ queryKey: qk.replayOptions(), queryFn: () => api.replayOptions() });
  const busy = options.data?.busy ?? false;
  return (
    <>
      <Card>
        <div className="row">
          <Button variant="primary" disabled={!options.data || busy || open} onClick={() => setOpen(true)}>
            New replay
          </Button>
          {busy && (
            <span className="small muted" role="note">
              {BUSY_REASON}
            </span>
          )}
        </div>
        {options.isError && <ErrorBox error={options.error} onRetry={() => void options.refetch()} />}
      </Card>
      {open && options.data && <ReplayForm options={options.data} onClose={() => setOpen(false)} />}
      <Card title="Replays">
        <ReplayList />
      </Card>
    </>
  );
}

export default function ReplayPage() {
  const [params] = useSearchParams();
  const raw = params.get("id");
  const id = parseId(raw);
  return (
    <main className="page">
      <h1>Replay</h1>
      {raw === null ? (
        <ReplayHome />
      ) : (
        <>
          <p>
            <Link className="link-touch" to="/replay">
              ← Back to replays
            </Link>
          </p>
          {id === null ? <Empty>“{raw}” is not a valid replay id.</Empty> : <ReplayDetail key={id} id={id} />}
        </>
      )}
    </main>
  );
}
