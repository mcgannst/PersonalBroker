// A strategy plug-in's panel, drawn from `OptPanelOut` alone (OPTSIM-T15; task plan §3.5): the summary, the
// tables and the actions. Nothing here knows which strategy it is showing.
//
// Where an action appears: an action key listed on a row is offered on that row (sent with the row's id);
// an action no row lists is offered once for the whole panel (sent without a row id).
// A toggle on a row starts from the row's cell of the same key when that cell is a boolean.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { OPT_SLOW_MS, oqk, useOptionsApi } from "../../api/optionsClient";
import type { OptKeyValueOut, OptPanelActionIn, OptPanelActionOut, OptPanelActionResultOut, OptPanelColumnOut, OptPanelOut, OptPanelRowOut, OptPanelTableOut } from "../../api/types";
import { Badge, Button, Empty, Loading, Table, errorMessage } from "../../components/ui";
import { fmtDate, fmtDateTime, fmtMoney } from "../../lib/format";
import { Confirm } from "../settings/Confirm";
import { OptErrorBox } from "./shared";

const DASH = "–";

/** One cell, shown by its column's kind. */
export function cellText(kind: OptPanelColumnOut["kind"], value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return DASH;
  switch (kind) {
    case "money":
      return typeof value === "string" || typeof value === "number" ? fmtMoney(value) : DASH;
    case "date":
      if (typeof value !== "string") return DASH;
      return value.includes("T") ? fmtDateTime(value) : fmtDate(value);
    case "bool":
      return value === true ? "Yes" : value === false ? "No" : String(value);
    case "badge":
      return <Badge tone="info">{String(value)}</Badge>;
    default:
      return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
}

function KeyValues({ items }: { items: readonly OptKeyValueOut[] }) {
  return (
    <dl className="opt-kv">
      {items.map((kv, i) => (
        <div key={`${kv.label}-${i}`} className="opt-kv-item">
          <dt className="stat-label">{kv.label}</dt>
          <dd className={kv.tone ? `tone-${kv.tone} opt-toned` : undefined}>{kv.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Sends one action; resolves true when the plug-in accepted it. */
type Send = (body: OptPanelActionIn) => Promise<boolean>;

/** One action's control. `rowId` is null for a panel-wide action; `current` is a toggle's present state. */
function ActionControl({ action, rowId, current, busy, send }: { action: OptPanelActionOut; rowId: string | null; current: boolean; busy: boolean; send: Send }) {
  const [text, setText] = useState("");
  const [choice, setChoice] = useState(action.choices[0] ?? "");
  const [pending, setPending] = useState<OptPanelActionIn | null>(null);
  const id = `opt-action-${action.key}-${rowId ?? "panel"}`;

  const run = async (body: OptPanelActionIn) => {
    const ok = await send(body);
    setPending(null);
    if (ok) setText("");
  };
  const ask = (value: OptPanelActionIn["value"]) => {
    const body: OptPanelActionIn = { action: action.key, row_id: rowId, value };
    if (action.confirm) setPending(body);
    else void run(body);
  };

  if (pending) {
    return (
      <Confirm message={`${action.label}${rowId ? ` (${rowId})` : ""}?`} confirmLabel={action.label} busy={busy} onConfirm={() => void run(pending)} onCancel={() => setPending(null)} />
    );
  }

  switch (action.kind) {
    case "toggle":
      return (
        <label className="check-row">
          <input type="checkbox" checked={current} disabled={busy} onChange={(e) => ask(e.target.checked)} /> {action.label}
        </label>
      );
    case "text":
      return (
        <div className="row">
          <label htmlFor={id}>
            {action.label}
            <input id={id} type="text" autoComplete="off" value={text} disabled={busy} onChange={(e) => setText(e.target.value)} />
          </label>
          <Button className="opt-form-btn" disabled={text.trim() === ""} busy={busy} aria-label={`${action.label}: save`} onClick={() => ask(text.trim())}>
            Save
          </Button>
        </div>
      );
    case "choice":
      return (
        <div className="row">
          <label htmlFor={id}>
            {action.label}
            <select id={id} value={choice} disabled={busy} onChange={(e) => setChoice(e.target.value)}>
              {action.choices.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <Button className="opt-form-btn" disabled={choice === ""} busy={busy} aria-label={`${action.label}: set`} onClick={() => ask(choice)}>
            Set
          </Button>
        </div>
      );
    default:
      return (
        <Button variant={action.confirm ? "danger" : "plain"} busy={busy} onClick={() => ask(null)}>
          {action.label}
        </Button>
      );
  }
}

function RowView({ table, row, actions, busy, send }: { table: OptPanelTableOut; row: OptPanelRowOut; actions: readonly OptPanelActionOut[]; busy: boolean; send: Send }) {
  const [open, setOpen] = useState(false);
  const offered = actions.filter((a) => row.actions.includes(a.key));
  const expandable = offered.length > 0 || row.detail.length > 0;
  return (
    <>
      <tr>
        {table.columns.map((c) => (
          <td key={c.key}>{cellText(c.kind, row.cells[c.key])}</td>
        ))}
        <td>
          {expandable && (
            <Button aria-expanded={open} aria-label={`${open ? "Hide" : "Show"} ${row.id}`} onClick={() => setOpen((o) => !o)}>
              {open ? "Hide" : "More"}
            </Button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={table.columns.length + 1} className="opt-row-detail">
            <div className="stack" role="group" aria-label={`${row.id} details`}>
              {row.detail.length > 0 && <KeyValues items={row.detail} />}
              {offered.map((a) => (
                <ActionControl key={a.key} action={a} rowId={row.id} current={row.cells[a.key] === true} busy={busy} send={send} />
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

/** The panel itself. `onAction` sends one action and resolves with the plug-in's answer. */
export function PanelBody({ panel, onAction }: { panel: OptPanelOut; onAction: (body: OptPanelActionIn) => Promise<OptPanelActionResultOut> }) {
  const [result, setResult] = useState<OptPanelActionResultOut | null>(null);
  const act = useMutation({ mutationFn: onAction, onSuccess: setResult });
  const send: Send = async (body) => {
    setResult(null);
    try {
      return (await act.mutateAsync(body)).ok;
    } catch {
      return false; // shown below from the mutation's error
    }
  };
  const onRows = new Set(panel.tables.flatMap((t) => t.rows.flatMap((r) => r.actions)));
  const panelActions = panel.actions.filter((a) => !onRows.has(a.key));

  return (
    <div className="stack opt-gap">
      {panel.summary.length > 0 && <KeyValues items={panel.summary} />}
      {panel.tables.map((t) => (
        <section key={t.key} className="stack" aria-label={t.title}>
          <h4 className="opt-subhead">{t.title}</h4>
          {t.rows.length === 0 ? (
            <Empty>{t.empty_text || "Nothing to show"}</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  {t.columns.map((c) => (
                    <th key={c.key}>{c.label}</th>
                  ))}
                  <th />
                </tr>
              </thead>
              <tbody>
                {t.rows.map((r) => (
                  <RowView key={r.id} table={t} row={r} actions={panel.actions} busy={act.isPending} send={send} />
                ))}
              </tbody>
            </Table>
          )}
        </section>
      ))}
      {panelActions.length > 0 && (
        <div className="stack" role="group" aria-label="Actions">
          {panelActions.map((a) => (
            <ActionControl key={a.key} action={a} rowId={null} current={false} busy={act.isPending} send={send} />
          ))}
        </div>
      )}
      {result && (
        <p className={`small opt-notice ${result.ok ? "tone-ok" : "tone-bad"}`} role={result.ok ? "status" : "alert"}>
          {result.message}
        </p>
      )}
      {act.isError && (
        <p className="small tone-bad opt-notice" role="alert">
          {errorMessage(act.error)}
        </p>
      )}
    </div>
  );
}

export function PanelView({ strategyKey }: { strategyKey: string }) {
  const api = useOptionsApi();
  const queryClient = useQueryClient();
  const q = useQuery({ queryKey: oqk.panel(strategyKey), queryFn: () => api.optPanel(strategyKey), refetchInterval: OPT_SLOW_MS });
  if (q.isPending) return <Loading />;
  if (q.isError) return <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  return (
    <PanelBody
      panel={q.data}
      onAction={async (body) => {
        const result = await api.optPanelAction(strategyKey, body);
        void queryClient.invalidateQueries({ queryKey: oqk.panel(strategyKey) });
        return result;
      }}
    />
  );
}
