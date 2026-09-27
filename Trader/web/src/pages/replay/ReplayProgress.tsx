// A queued or running replay's progress (sessions done of total, the current date) and its Cancel button,
// which asks "Stop this replay after the current day?" before calling `cancelReplay` (the runner stops at the
// next day boundary).
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { ReplayOut } from "../../api/types";
import { Button, errorMessage } from "../../components/ui";
import { fmtDate } from "../../lib/format";

export function progressText(r: ReplayOut): string {
  const p = r.progress;
  const base = `${p.sessions_done} of ${p.sessions_total} sessions`;
  return p.current_date ? `${base}, at ${fmtDate(p.current_date)}` : base;
}

export function ReplayProgress({ replay }: { replay: ReplayOut }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const cancel = useMutation({
    mutationFn: () => api.cancelReplay(replay.id),
    onSuccess: (out) => {
      setConfirming(false);
      queryClient.setQueryData(qk.replay(out.id), out);
      void queryClient.invalidateQueries({ queryKey: ["replays"] });
    },
  });
  const p = replay.progress;
  const stopping = replay.cancel_requested || (cancel.isSuccess && cancel.data.cancel_requested);

  return (
    <div className="stack" role="group" aria-label="Replay progress">
      <progress aria-label="Progress" value={p.sessions_done} max={Math.max(p.sessions_total, 1)} style={{ width: "100%", height: 12 }} />
      <p className="small num">{progressText(replay)}</p>
      {p.trades > 0 && <p className="small muted">{`${p.trades} trade${p.trades === 1 ? "" : "s"} so far`}</p>}
      {stopping ? (
        <p className="small tone-warn" role="status">
          Stopping after the current day.
        </p>
      ) : confirming ? (
        <div className="confirm stack" role="alertdialog" aria-label="Stop replay">
          <p>Stop this replay after the current day?</p>
          <div className="row">
            <Button variant="danger" busy={cancel.isPending} onClick={() => cancel.mutate()}>
              Stop replay
            </Button>
            <Button
              variant="plain"
              disabled={cancel.isPending}
              onClick={() => {
                cancel.reset();
                setConfirming(false);
              }}
            >
              Keep running
            </Button>
          </div>
        </div>
      ) : (
        <div className="row">
          <Button variant="danger" onClick={() => setConfirming(true)}>
            Cancel replay
          </Button>
        </div>
      )}
      {cancel.isError && (
        <p className="small tone-bad" role="alert">
          {errorMessage(cancel.error)}
        </p>
      )}
    </div>
  );
}
