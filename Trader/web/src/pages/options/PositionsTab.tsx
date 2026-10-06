// Options, Positions tab (OPTSIM-T15): the structures grouped by underlying, each with its legs, quantity,
// entry, mark, result, days to expiry, delta, reserve and source. Close hands the ticket the closing legs.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { OPT_FAST_MS, OPT_SLOW_MS, oqk, useOptionsApi, type OptPositionsQuery } from "../../api/optionsClient";
import type { OptStructureOut } from "../../api/types";
import { Badge, Button, Card, Empty, Loading, Table } from "../../components/ui";
import { fmtDateTime, fmtMoney, fmtPrice } from "../../lib/format";
import { KIND_WORDS, OptErrorBox, closingTicket, netWords, type TicketDraft } from "./shared";

/** Structures by underlying, underlyings in alphabetical order, each group in the server's order. */
export function groupByUnderlying(items: readonly OptStructureOut[]): [string, OptStructureOut[]][] {
  const groups = new Map<string, OptStructureOut[]>();
  for (const s of items) {
    const list = groups.get(s.underlying) ?? [];
    list.push(s);
    groups.set(s.underlying, list);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function StructureBlock({ structure, onClose }: { structure: OptStructureOut; onClose: (draft: TicketDraft) => void }) {
  const open = structure.state === "open";
  return (
    <article className="opt-block stack" aria-label={`Position ${structure.id}`}>
      <header className="row">
        <strong>
          {structure.qty} × {KIND_WORDS[structure.kind]}
        </strong>
        <Badge tone={structure.source === "manual" ? "muted" : "info"}>{structure.source}</Badge>
        {structure.frozen && <Badge tone="warn">frozen</Badge>}
        {!open && <Badge tone="muted">{structure.close_reason ?? "closed"}</Badge>}
      </header>
      <p className="small muted">
        Entry {netWords(structure.entry_net)}
        {structure.dte !== null ? ` · ${structure.dte} days to expiry` : ""} · reserve {fmtMoney(structure.reserved_cash)}
        {structure.take_profit_net !== null ? ` · take profit at ${netWords(structure.take_profit_net)}` : ""} · opened {fmtDateTime(structure.opened_at)}
        {structure.closed_at ? ` · closed ${fmtDateTime(structure.closed_at)}` : ""}
      </p>
      <p className="small">
        {open ? `Unrealized ${fmtMoney(structure.unrealized_pnl)} · ` : ""}Realized {fmtMoney(structure.realized_pnl)} · fees {fmtMoney(structure.fees_total)}
      </p>
      <Table>
        <thead>
          <tr>
            <th>Leg</th>
            <th>Quantity</th>
            <th>Entry</th>
            <th>Mark</th>
            <th>P&amp;L</th>
            <th>Delta</th>
          </tr>
        </thead>
        <tbody>
          {structure.positions.map((p) => (
            <tr key={p.id}>
              <td>{p.contract?.label ?? `${structure.underlying} shares`}</td>
              <td>{p.qty}</td>
              <td>{fmtPrice(p.avg_price)}</td>
              <td>{fmtPrice(p.mark)}</td>
              <td>{fmtMoney(p.unrealized_pnl)}</td>
              <td>{fmtPrice(p.delta)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      {open && (
        <div className="row">
          <Button disabled={structure.frozen} onClick={() => onClose(closingTicket(structure))}>
            Close
          </Button>
          {structure.frozen && <span className="small muted">Frozen: it cannot be traded.</span>}
        </div>
      )}
    </article>
  );
}

export function PositionsTab({ onClose }: { onClose: (draft: TicketDraft) => void }) {
  const api = useOptionsApi();
  const [closed, setClosed] = useState(false);
  const query: OptPositionsQuery = closed ? { state: "closed", limit: 50 } : { state: "open" };
  const q = useQuery({ queryKey: oqk.positions(query), queryFn: () => api.optPositions(query), refetchInterval: closed ? OPT_SLOW_MS : OPT_FAST_MS });
  return (
    <div className="stack opt-gap">
      <label className="check-row">
        <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} /> Show closed positions
      </label>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.items.length === 0 ? (
        <Empty>{closed ? "No closed positions" : "No open positions"}</Empty>
      ) : (
        groupByUnderlying(q.data.items).map(([underlying, items]) => (
          <Card key={underlying} title={underlying}>
            <div className="stack opt-gap">
              {items.map((s) => (
                <StructureBlock key={s.id} structure={s} onClose={onClose} />
              ))}
            </div>
          </Card>
        ))
      )}
    </div>
  );
}
