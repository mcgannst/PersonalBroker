// Options, Orders tab (OPTSIM-T15): the working orders, with Cancel and Reprice on the ones entered by hand
// (a strategy's order is read-only here), and the history with the quote each leg filled against.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { OPT_FAST_MS, OPT_SLOW_MS, oqk, useOptionsApi, type OptOrdersQuery } from "../../api/optionsClient";
import type { JsonObject, OptLegOut, OptOrderOut, OptOrderStatus } from "../../api/types";
import { Badge, Button, Card, Empty, Loading, errorMessage, type Tone } from "../../components/ui";
import { fmtDateTime, fmtMoney, fmtPrice, fmtTime } from "../../lib/format";
import { NetInput, OptErrorBox, REJECT_WORDS, netAmount, netDirection, netWords, signedNet, useRefreshOptions, type NetDirection } from "./shared";

/** Orders placed on the web carry this source; only they can be cancelled or repriced here. */
export const MANUAL_SOURCE = "manual";

const WORKING: OptOrdersQuery = { status: "working" };
const HISTORY: OptOrdersQuery = { status: "history", limit: 50 };

const STATUS_TONE: Record<OptOrderStatus, Tone> = { working: "info", filled: "ok", cancelled: "muted", expired: "muted", rejected: "bad" };

function legWords(leg: OptLegOut, underlying: string): string {
  const what = leg.contract?.label ?? `${underlying} shares`;
  return `${leg.side === "buy" ? "Buy" : "Sell"} to ${leg.effect} ${what}${leg.ratio !== 1 ? ` × ${leg.ratio}` : ""}`;
}

function priceWords(order: OptOrderOut): string {
  if (order.order_type === "market") return "market";
  if (order.net_limit === null) return "limit from the midpoint";
  return `limit ${netWords(order.net_limit)}`;
}

function quotePart(quote: JsonObject, key: string): string {
  const v = quote[key];
  return typeof v === "string" || typeof v === "number" ? fmtPrice(v) : "n/a";
}

/** `filled 0.55 against bid 0.52, ask 0.55, last 0.53 (07:40 MT)`. */
export function fillWords(leg: OptLegOut): string | null {
  if (leg.fill_price === null) return null;
  const q = leg.fill_quote;
  if (!q) return `filled ${fmtPrice(leg.fill_price)}`;
  const at = typeof q.fetched_at === "string" ? ` (${fmtTime(q.fetched_at)})` : "";
  return `filled ${fmtPrice(leg.fill_price)} against bid ${quotePart(q, "bid")}, ask ${quotePart(q, "ask")}, last ${quotePart(q, "last")}${at}`;
}

function Reprice({ order, onDone }: { order: OptOrderOut; onDone: () => void }) {
  const api = useOptionsApi();
  const [amount, setAmount] = useState(() => netAmount(order.net_limit));
  const [direction, setDirection] = useState<NetDirection>(() => netDirection(order.net_limit));
  const net = signedNet(amount, direction);
  const save = useMutation({ mutationFn: (netLimit: string) => api.optReprice(order.id, { net_limit: netLimit }), onSuccess: onDone });
  return (
    <div className="stack" role="group" aria-label={`Reprice order ${order.id}`}>
      <NetInput id={`opt-reprice-${order.id}`} amount={amount} direction={direction} onAmount={setAmount} onDirection={setDirection} disabled={save.isPending} />
      <div className="row">
        <Button variant="primary" disabled={net === null} busy={save.isPending} onClick={() => net !== null && save.mutate(net)}>
          Set limit
        </Button>
        <Button disabled={save.isPending} onClick={onDone}>
          Back
        </Button>
      </div>
      {save.isError && (
        <p className="small tone-bad opt-notice" role="alert">
          {errorMessage(save.error)}
        </p>
      )}
    </div>
  );
}

function OrderBlock({ order }: { order: OptOrderOut }) {
  const api = useOptionsApi();
  const refresh = useRefreshOptions();
  const [repricing, setRepricing] = useState(false);
  const cancel = useMutation({ mutationFn: () => api.optCancel(order.id), onSettled: refresh });
  const working = order.status === "working";
  const manual = order.source === MANUAL_SOURCE;
  return (
    <article className="opt-block stack" aria-label={`Order ${order.id}`}>
      <header className="row">
        <strong>
          #{order.id} · {order.qty} × {order.underlying}
        </strong>
        <Badge tone={STATUS_TONE[order.status]}>{order.status}</Badge>
        <Badge tone={manual ? "muted" : "info"}>{order.source}</Badge>
      </header>
      <ul className="opt-legs">
        {order.legs.map((leg) => {
          const fill = fillWords(leg);
          return (
            <li key={leg.leg_no}>
              {legWords(leg, order.underlying)}
              {fill && <span className="small muted">{` · ${fill}`}</span>}
            </li>
          );
        })}
      </ul>
      <p className="small muted">
        {priceWords(order)}
        {order.walk ? ", walking" : ""} · {order.tif === "gtc" ? "good till cancelled" : "day"} · sent {fmtDateTime(order.submitted_at)}
        {order.closed_at ? ` · ended ${fmtDateTime(order.closed_at)}` : ""}
        {working ? ` · reserves ${fmtMoney(order.reserved_cash)}` : ""}
        {order.reason ? ` · ${order.reason}` : ""}
      </p>
      {order.fill_net !== null && (
        <p className="small">
          Filled at {netWords(order.fill_net)} per share{order.fees !== null ? ` · fees ${fmtMoney(order.fees)}` : ""}
        </p>
      )}
      {order.status === "rejected" && (
        <p className="small tone-bad opt-notice">
          {order.reject_reason ? REJECT_WORDS[order.reject_reason] : "Rejected."} {order.reject_detail ?? ""}
        </p>
      )}
      {working && !manual && <p className="small muted">Placed by a strategy: it manages this order itself.</p>}
      {working && manual && !repricing && (
        <div className="row">
          <Button variant="danger" busy={cancel.isPending} onClick={() => cancel.mutate()}>
            Cancel
          </Button>
          {order.order_type === "limit" && (
            <Button disabled={cancel.isPending} onClick={() => setRepricing(true)}>
              Reprice
            </Button>
          )}
        </div>
      )}
      {working && manual && repricing && (
        <Reprice
          order={order}
          onDone={() => {
            setRepricing(false);
            refresh();
          }}
        />
      )}
      {cancel.isError && (
        <p className="small tone-bad opt-notice" role="alert">
          {errorMessage(cancel.error)}
        </p>
      )}
    </article>
  );
}

function OrderList({ query, interval, empty }: { query: OptOrdersQuery; interval: number; empty: string }) {
  const api = useOptionsApi();
  const q = useQuery({ queryKey: oqk.orders(query), queryFn: () => api.optOrders(query), refetchInterval: interval });
  if (q.isPending) return <Loading />;
  if (q.isError) return <OptErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  if (q.data.items.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="stack opt-gap">
      {q.data.items.map((o) => (
        <OrderBlock key={o.id} order={o} />
      ))}
    </div>
  );
}

export function OrdersTab() {
  return (
    <div className="stack opt-gap">
      <Card title="Working">
        <OrderList query={WORKING} interval={OPT_FAST_MS} empty="No working orders" />
      </Card>
      <Card title="History">
        <OrderList query={HISTORY} interval={OPT_SLOW_MS} empty="No orders yet" />
      </Card>
    </div>
  );
}
