// A position's full audit chain (BR-13, BR-23): signal, proposals, orders, fills (with the quote snapshot), the
// trade result and the 5-minute chart. An open position shows the same chain with "Open" and no result.
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { FillOut, JsonObject, OrderOut, PositionDetailOut, ProposalOut, SignalOut, TradeOut } from "../../api/types";
import { Badge, Card, Empty, ErrorBox, Loading } from "../../components/ui";
import { fmtDateTime, fmtDuration, fmtMoney, fmtPrice, fmtR, fmtTime } from "../../lib/format";
import { TradeChart } from "./TradeChart";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div role="region" aria-label={title}>
      <Card title={title}>{children}</Card>
    </div>
  );
}

/** A JSON value as short text: nested objects become `key value, key value`. */
function valueText(value: unknown): string {
  if (value === null || value === undefined) return "n/a";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(valueText).join(", ");
  if (typeof value === "object") {
    return Object.entries(value as JsonObject)
      .map(([k, v]) => `${k} ${valueText(v)}`)
      .join(", ");
  }
  return String(value);
}

function asText(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}

function SignalBlock({ signal }: { signal: SignalOut | null }) {
  return (
    <Section title="Signal">
      {signal === null ? (
        <Empty>No signal recorded.</Empty>
      ) : (
        <>
          <p>
            {signal.event_key} at {fmtDateTime(signal.ts)} · {signal.strategy_key} {signal.config_version} (revision{" "}
            {signal.config_revision})
          </p>
          <dl className="evidence">
            {Object.entries(signal.evidence).map(([key, value]) => (
              <div key={key} className="row">
                <dt className="muted">{key}</dt>
                <dd style={{ margin: 0 }}>{valueText(value)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
    </Section>
  );
}

function ProposalItem({ p }: { p: ProposalOut }) {
  const decided =
    p.decided_at !== null
      ? `decided ${fmtDateTime(p.decided_at)}${p.decided_via ? ` via ${p.decided_via}` : ""}${p.decided_by ? ` by ${p.decided_by}` : ""}`
      : "not decided";
  const latency = p.decision_latency_ms !== null ? ` · latency ${fmtDuration(p.decision_latency_ms / 1000)}` : "";
  return (
    <li>
      <strong>{p.kind.toUpperCase()}</strong> #{p.id} · {p.status} · created {fmtDateTime(p.created_at)} · {decided}
      {latency}
      {p.error ? <span className="tone-bad"> · {p.error}</span> : null}
    </li>
  );
}

function orderPrices(o: OrderOut): string {
  const parts: string[] = [];
  if (o.stop_price !== null) parts.push(`stop ${fmtPrice(o.stop_price)}`);
  if (o.limit_price !== null) parts.push(`limit ${fmtPrice(o.limit_price)}`);
  if (o.stop_loss !== null) parts.push(`stop loss ${fmtPrice(o.stop_loss)}`);
  return parts.length ? ` · ${parts.join(", ")}` : "";
}

function OrderItem({ o }: { o: OrderOut }) {
  return (
    <li>
      <strong>{o.purpose}</strong> #{o.id} · {o.side} {o.qty} {o.order_type}
      {orderPrices(o)} · {o.status}
      {o.cancel_reason ? ` · ${o.cancel_reason}` : ""}
    </li>
  );
}

function feesText(fees: JsonObject): string {
  const parts = Object.entries(fees)
    .map(([k, v]) => [k, asText(v)] as const)
    .filter((kv): kv is readonly [string, string] => kv[1] !== null)
    .map(([k, v]) => `${k} $${fmtPrice(v)}`);
  return parts.length ? parts.join(", ") : "none";
}

function quoteText(q: JsonObject): string {
  const parts: string[] = [];
  for (const key of ["bid", "ask", "last"]) {
    const v = asText(q[key]);
    if (v !== null) parts.push(`${key} ${fmtPrice(v)}`);
  }
  const at = asText(q.last_trade_time);
  const text = parts.length ? parts.join(", ") : "no quote";
  return at !== null ? `${text} (${fmtTime(at)})` : text;
}

function FillItem({ f }: { f: FillOut }) {
  return (
    <li>
      <strong>{f.purpose}</strong> {f.side} {f.qty} at {fmtPrice(f.price)} · {fmtTime(f.ts)} · slippage ${fmtPrice(f.slippage)} ·
      fees {feesText(f.fees)}
      <div className="small muted">Quote: {quoteText(f.quote_snapshot)}</div>
    </li>
  );
}

function ResultBlock({ trade }: { trade: TradeOut }) {
  return (
    <Section title="Result">
      <p>
        <strong>{fmtMoney(trade.pnl)}</strong> ({fmtR(trade.pnl_r)}) · {trade.qty} × {fmtPrice(trade.entry_price)} →{" "}
        {fmtPrice(trade.exit_price)} · exit reason {trade.exit_reason}
      </p>
      <p className="small muted">
        Planned risk {fmtMoney(trade.planned_risk)} · fees {fmtMoney(trade.fees_total)} · slippage ${fmtPrice(trade.slippage_total)} ·{" "}
        {fmtDateTime(trade.opened_at)} to {fmtDateTime(trade.closed_at)}
      </p>
    </Section>
  );
}

/** Chart inputs from the chain: entry = the entry fill price, stop = the entry order's stop loss, exit = the trade's. */
export function chartLevels(d: PositionDetailOut): { entry: string | null; stop: string | null; exit: string | null } {
  const entryFill = d.fills.find((f) => f.purpose === "entry");
  const entryOrder = d.orders.find((o) => o.purpose === "entry");
  const exitFill = [...d.fills].reverse().find((f) => f.purpose !== "entry");
  return {
    entry: entryFill?.price ?? d.position.entry,
    stop: entryOrder?.stop_loss ?? d.position.stop,
    exit: d.trade?.exit_price ?? exitFill?.price ?? null,
  };
}

export function PositionDetail({ id }: { id: number }) {
  const api = useApi();
  const q = useQuery({ queryKey: qk.position(id), queryFn: () => api.position(id) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  const d = q.data;
  const p = d.position;
  const levels = chartLevels(d);
  return (
    <div className="stack">
      <Card
        title={
          <>
            {p.ticker} · position #{p.id}
          </>
        }
        actions={p.status === "open" ? <Badge tone="info">Open</Badge> : <Badge tone="muted">Closed</Badge>}
      >
        <p>
          {p.strategy_key} · {p.qty} shares · entry {fmtPrice(p.entry)} · stop {fmtPrice(p.stop)} · opened {fmtDateTime(p.opened_at)}
          {p.closed_at ? ` · closed ${fmtDateTime(p.closed_at)}` : ""}
        </p>
        {p.status === "open" && (
          <p className="small muted">
            Last {fmtPrice(p.last)} · unrealized {fmtMoney(p.unrealized_pnl)}
          </p>
        )}
        <TradeChart
          candles={d.candles}
          entry={levels.entry}
          stop={levels.stop}
          exit={levels.exit}
          fills={d.fills}
          chartError={d.chart_error}
        />
      </Card>
      <SignalBlock signal={d.signal} />
      <Section title="Proposals">
        {d.proposals.length === 0 ? (
          <Empty>No proposals.</Empty>
        ) : (
          <ol className="chain">
            {d.proposals.map((x) => (
              <ProposalItem key={x.id} p={x} />
            ))}
          </ol>
        )}
      </Section>
      <Section title="Orders">
        {d.orders.length === 0 ? (
          <Empty>No orders.</Empty>
        ) : (
          <ol className="chain">
            {d.orders.map((o) => (
              <OrderItem key={o.id} o={o} />
            ))}
          </ol>
        )}
      </Section>
      <Section title="Fills">
        {d.fills.length === 0 ? (
          <Empty>No fills.</Empty>
        ) : (
          <ol className="chain">
            {d.fills.map((f) => (
              <FillItem key={f.id} f={f} />
            ))}
          </ol>
        )}
      </Section>
      {d.trade !== null && <ResultBlock trade={d.trade} />}
    </div>
  );
}
