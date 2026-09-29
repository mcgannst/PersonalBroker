// Kill switches (BR-34, BR-41; SPEC §6.3): each switch's state and how it clears; a typed-reason reset
// (3–500 characters, then a confirm step) for tripped switches that need a web reset; Pause (with a
// confirm step) and Resume. Server messages such as 409 "Already paused." are shown as they come.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { KillSwitchesOut, KillSwitchOut } from "../../api/types";
import { Button, ErrorBox, Light, Loading, errorMessage, type Tone } from "../../components/ui";
import { fmtDateTime, fmtPct } from "../../lib/format";
import { Confirm } from "./Confirm";

export const REASON_MIN = 3;
export const REASON_MAX = 500;
const PAUSE = "manual_pause";

type Action = { kind: "reset"; sw: string; label: string; reason: string } | { kind: "pause" } | { kind: "resume" };

function tone(s: KillSwitchOut): Tone {
  if (!s.tripped) return "ok";
  return s.switch === PAUSE ? "warn" : "bad";
}

function stateWord(s: KillSwitchOut): string {
  if (s.switch === PAUSE) return s.tripped ? "on" : "off";
  return s.tripped ? "tripped" : "off";
}

/** The reason as the server will store it, or null when it is not 3–500 characters after trimming. */
export function validReason(text: string): string | null {
  const t = text.trim();
  return t.length >= REASON_MIN && t.length <= REASON_MAX ? t : null;
}

function ResetForm({ sw, busy, onSubmit, onCancel }: { sw: KillSwitchOut; busy: boolean; onSubmit: (reason: string) => void; onCancel: () => void }) {
  const [text, setText] = useState("");
  const [confirming, setConfirming] = useState(false);
  const reason = validReason(text);
  const inputId = `reset-reason-${sw.switch}`;
  return (
    <div className="stack">
      <label htmlFor={inputId}>Reason for the reset</label>
      <textarea
        id={inputId}
        rows={2}
        maxLength={REASON_MAX}
        value={text}
        disabled={busy || confirming}
        onChange={(e) => setText(e.target.value)}
      />
      <p className="small muted">
        {text.trim().length}/{REASON_MAX} characters (at least {REASON_MIN})
      </p>
      {confirming && reason ? (
        <Confirm
          message={`Reset ${sw.label}? New entries can start again.`}
          confirmLabel="Confirm reset"
          busy={busy}
          onConfirm={() => onSubmit(reason)}
          onCancel={() => setConfirming(false)}
        />
      ) : (
        <div className="row">
          <Button variant="danger" disabled={reason === null} onClick={() => setConfirming(true)}>
            Reset…
          </Button>
          <Button variant="plain" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}
    </div>
  );
}

/** A tone as a class: the legacy `tone-*` (Settings) or the `status-*` family (Control, design D9: never the
 * money green/red). */
function toneClass(t: Tone, statusTones: boolean): string {
  if (!statusTones) return `tone-${t}`;
  return t === "ok" || t === "warn" || t === "bad" ? `status-${t}` : "status-muted";
}

/** The shared Light's markup with a status-token class (Control). */
function StatusLight({ tone: t, label }: { tone: Tone; label: string }) {
  return (
    <span className={`light ${toneClass(t, true)}`} role="status" aria-label={label}>
      <span className="light-dot" aria-hidden="true" />
      <span className="light-label">{label}</span>
    </span>
  );
}

function SwitchRow({
  s,
  resetOpen,
  busy,
  statusTones,
  onOpenReset,
  onCloseReset,
  onReset,
}: {
  s: KillSwitchOut;
  resetOpen: boolean;
  busy: boolean;
  statusTones: boolean;
  onOpenReset: () => void;
  onCloseReset: () => void;
  onReset: (reason: string) => void;
}) {
  const label = `${s.label}: ${stateWord(s)}`;
  return (
    <li className="stack">
      <div className="row">
        {statusTones ? <StatusLight tone={tone(s)} label={label} /> : <Light tone={tone(s)} label={label} />}
        {s.tripped && s.tripped_at && <span className="small muted">since {fmtDateTime(s.tripped_at)}</span>}
        {s.tripped && s.value !== null && (
          <span className="small">
            {fmtPct(s.value)} (limit {fmtPct(s.threshold)})
          </span>
        )}
      </div>
      {s.clears && <p className="small">{s.clears}</p>}
      {s.tripped && s.automatic && s.needs_web_reset && !resetOpen && (
        <div>
          <Button variant="danger" aria-label={`Reset ${s.label}`} onClick={onOpenReset}>
            Reset
          </Button>
        </div>
      )}
      {resetOpen && <ResetForm sw={s} busy={busy} onSubmit={onReset} onCancel={onCloseReset} />}
    </li>
  );
}

/**
 * Props (DB-GWEB fix round 1, both optional so Settings is unchanged):
 * - `showTradingControls` (default true): the Pause/Resume row. Control passes false: its Engine card owns the
 *   one Pause/Resume pair, both behind a confirm (design §4.1).
 * - `statusTones` (default false): lights and notices in the `status-*` classes instead of the legacy
 *   `tone-*` (whose ok/bad are the money green/red). Control passes true (design D9).
 */
export function KillSwitchPanel({ showTradingControls = true, statusTones = false }: { showTradingControls?: boolean; statusTones?: boolean } = {}) {
  const api = useApi();
  const queryClient = useQueryClient();
  const state = useQuery({ queryKey: qk.killswitches(), queryFn: () => api.killswitches() });
  const [resetOpen, setResetOpen] = useState<string | null>(null);
  const [confirmPause, setConfirmPause] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: (a: Action): Promise<KillSwitchesOut> => {
      if (a.kind === "reset") return api.resetKillSwitch(a.sw, { reason: a.reason });
      return a.kind === "pause" ? api.pause() : api.resume();
    },
    onMutate: () => setNotice(null),
    onSuccess: (out, a) => {
      queryClient.setQueryData(qk.killswitches(), out);
      // The Dashboard (`live`, under `dashboard`) and Control (`control`, under `system`) draw the switch
      // lights from their own answers, so both refresh at once (DB-T11 fix round 1, B8).
      void queryClient.invalidateQueries({ queryKey: qk.dashboard() });
      void queryClient.invalidateQueries({ queryKey: qk.system() });
      setResetOpen(null);
      setConfirmPause(false);
      setNotice(a.kind === "reset" ? `${a.label} reset.` : a.kind === "pause" ? "Paused: no new entries." : "Resumed.");
    },
    onError: () => setConfirmPause(false),
  });

  if (state.isPending) return <Loading />;
  if (state.isError) return <ErrorBox error={state.error} onRetry={() => void state.refetch()} />;

  return (
    <div className="stack">
      <ul className="stack" style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {state.data.switches.map((s) => (
          <SwitchRow
            key={s.switch}
            s={s}
            resetOpen={resetOpen === s.switch}
            busy={act.isPending}
            statusTones={statusTones}
            onOpenReset={() => {
              act.reset();
              setResetOpen(s.switch);
            }}
            onCloseReset={() => setResetOpen(null)}
            onReset={(reason) => act.mutate({ kind: "reset", sw: s.switch, label: s.label, reason })}
          />
        ))}
      </ul>
      {!showTradingControls ? null : confirmPause ? (
        <Confirm
          message="Block new entries? Exits and stops keep working."
          confirmLabel="Pause new entries"
          busy={act.isPending}
          onConfirm={() => act.mutate({ kind: "pause" })}
          onCancel={() => setConfirmPause(false)}
        />
      ) : (
        <div className="row">
          <Button
            variant="danger"
            disabled={act.isPending}
            onClick={() => {
              act.reset();
              setNotice(null);
              setConfirmPause(true);
            }}
          >
            Pause
          </Button>
          <Button variant="plain" busy={act.isPending && act.variables?.kind === "resume"} onClick={() => act.mutate({ kind: "resume" })}>
            Resume
          </Button>
        </div>
      )}
      {notice && (
        <p className={`small ${toneClass("ok", statusTones)}`} role="status">
          {notice}
        </p>
      )}
      {act.isError && (
        <p className={`small ${toneClass("bad", statusTones)}`} role="alert">
          {errorMessage(act.error)}
        </p>
      )}
      {state.data.history.length > 0 && (
        <details>
          <summary>History</summary>
          <ul className="small">
            {state.data.history.map((h) => (
              <li key={h.id}>
                {h.switch} tripped {fmtDateTime(h.tripped_at)}
                {h.reset_at ? `, reset ${fmtDateTime(h.reset_at)}${h.reset_by ? ` by ${h.reset_by}` : ""}${h.reset_reason ? `: ${h.reset_reason}` : ""}` : ", not reset"}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
