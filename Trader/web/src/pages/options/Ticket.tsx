// The order ticket (OPTSIM-T15): legs, quantity, market or limit, day or good-till-cancelled, walk. It says
// the limit as a debit or a credit in words, and shows the server's preview (net at the market, max loss,
// breakevens, reserve, cash after, and the collateral engine's verdict) before Submit. Submit is enabled only
// for an accepted preview of exactly the order on screen: any change makes the preview out of date.
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { isApiError } from "../../api/client";
import { useOptionsApi } from "../../api/optionsClient";
import type { OptOrderIn, OptOrderOut, OptOrderType, OptPreviewOut, OptTif } from "../../api/types";
import { Badge, Button, Stat, errorMessage } from "../../components/ui";
import { fmtMoney, fmtPrice } from "../../lib/format";
import { KIND_WORDS, NetInput, REJECT_WORDS, netWords, signedNet, ticketIntent, useRefreshOptions, type NetDirection, type TicketDraft, type TicketLeg } from "./shared";

export const MAX_TICKET_LEGS = 4;

export interface TicketTerms {
  orderType: OptOrderType;
  amount: string;
  direction: NetDirection;
  tif: OptTif;
  walk: boolean;
}

/** The order to send, or the reason (in words) there is none yet. */
export function buildOrder(draft: TicketDraft, terms: TicketTerms): { order: OptOrderIn } | { problem: string } {
  if (draft.legs.length === 0) return { problem: "Add a leg: click a bid to sell or an ask to buy." };
  if (draft.legs.length > MAX_TICKET_LEGS) return { problem: `An order has at most ${MAX_TICKET_LEGS} legs.` };
  if (!Number.isInteger(draft.qty) || draft.qty < 1 || draft.qty > 100) return { problem: "Quantity must be a whole number from 1 to 100." };
  let netLimit: string | null = null;
  const walk = terms.orderType === "limit" && terms.walk;
  if (terms.orderType === "limit" && !(walk && terms.amount.trim() === "")) {
    netLimit = signedNet(terms.amount, terms.direction);
    if (netLimit === null) return { problem: walk ? "The limit is not a price (or leave it empty to start at the midpoint)." : "Enter the limit price." };
  }
  const closes = draft.legs.some((l) => l.effect === "close");
  return {
    order: {
      underlying: draft.underlying,
      intent: ticketIntent(draft.legs),
      structure_id: closes ? draft.structure_id : null,
      legs: draft.legs.map((l) => ({ instrument: l.instrument, contract_id: l.contract_id, side: l.side, effect: l.effect, ratio: l.ratio })),
      qty: draft.qty,
      order_type: terms.orderType,
      net_limit: netLimit,
      tif: terms.tif,
      walk,
    },
  };
}

/** The order's price in words: "Market order", "Limit: credit 0.45 per share", ... */
export function termsWords(order: OptOrderIn): string {
  if (order.order_type === "market") return "Market order: fills at the bid or the ask of each leg.";
  if (order.net_limit === null || order.net_limit === undefined) return "Limit order starting at the midpoint, then walking toward the market.";
  return `Limit: ${netWords(order.net_limit)} per share${order.walk ? ", then walking toward the market" : ""}.`;
}

function problemsOf(error: unknown): string[] {
  if (isApiError(error) && error.fields?.length) return error.fields.map((f) => f.msg);
  return [errorMessage(error)];
}

export function PreviewView({ preview }: { preview: OptPreviewOut }) {
  return (
    <div className="stack" aria-label="Preview">
      <div className="row">
        {preview.accepted ? <Badge tone="ok">Accepted</Badge> : <Badge tone="bad">Rejected</Badge>}
        <span>{KIND_WORDS[preview.kind]}</span>
      </div>
      {!preview.accepted && (
        <p className="tone-bad opt-notice" role="alert">
          {preview.reject_reason ? REJECT_WORDS[preview.reject_reason] : "The order was refused."}
        </p>
      )}
      {preview.detail && <p className="small muted">{preview.detail}</p>}
      <div className="opt-stats">
        <Stat label="Net at the market" value={netWords(preview.net_at_market)} sub="per share" />
        <Stat label="Max loss" value={fmtMoney(preview.max_loss)} />
        <Stat label="Max profit" value={preview.max_profit === null ? "unlimited or n/a" : fmtMoney(preview.max_profit)} />
        <Stat label="Breakeven" value={preview.breakevens.length ? preview.breakevens.map((b) => fmtPrice(b)).join(", ") : "n/a"} />
        <Stat label="Reserve" value={fmtMoney(preview.reserve_cash)} sub="cash held as collateral" />
        <Stat label="Fees" value={fmtMoney(preview.fees)} />
        <Stat label="Cash after" value={fmtMoney(preview.cash_after)} sub={`free ${fmtMoney(preview.free_cash_after)}`} />
        <Stat label="In this underlying after" value={fmtMoney(preview.exposure_after)} sub={`cap ${fmtMoney(preview.cap_limit)}`} />
      </div>
    </div>
  );
}

function LegRow({ leg, index, onChange, onRemove }: { leg: TicketLeg; index: number; onChange: (leg: TicketLeg) => void; onRemove: () => void }) {
  const n = index + 1;
  return (
    <li className="row opt-leg">
      <span className="opt-leg-label">
        {leg.label}
        {leg.ratio !== 1 ? ` × ${leg.ratio}` : ""}
      </span>
      <select aria-label={`Side of leg ${n}`} value={leg.side} onChange={(e) => onChange({ ...leg, side: e.target.value === "sell" ? "sell" : "buy" })}>
        <option value="buy">Buy</option>
        <option value="sell">Sell</option>
      </select>
      <select aria-label={`Leg ${n} opens or closes`} value={leg.effect} onChange={(e) => onChange({ ...leg, effect: e.target.value === "close" ? "close" : "open" })}>
        <option value="open">to open</option>
        <option value="close">to close</option>
      </select>
      <Button aria-label={`Remove leg ${n}`} onClick={onRemove}>
        Remove
      </Button>
    </li>
  );
}

export function Ticket({ draft, onChange }: { draft: TicketDraft; onChange: (draft: TicketDraft) => void }) {
  const api = useOptionsApi();
  const refresh = useRefreshOptions();
  const [orderType, setOrderType] = useState<OptOrderType>("limit");
  const [amount, setAmount] = useState("");
  const [chosenDirection, setChosenDirection] = useState<NetDirection | null>(null);
  const [tif, setTif] = useState<OptTif>("day");
  const [walk, setWalk] = useState(false);
  const [previewed, setPreviewed] = useState<{ key: string; data: OptPreviewOut } | null>(null);
  const [sent, setSent] = useState<OptOrderOut | null>(null);

  // Until Stephen picks a side, a ticket that starts with a sale is a credit and one that starts with a buy a debit.
  const direction: NetDirection = chosenDirection ?? (draft.legs[0]?.side === "sell" ? "credit" : "debit");
  const built = buildOrder(draft, { orderType, amount, direction, tif, walk });
  const order = "order" in built ? built.order : null;
  const key = order ? JSON.stringify(order) : "";

  const preview = useMutation({
    mutationFn: (body: OptOrderIn) => api.optPreview(body),
    onSuccess: (data, body) => setPreviewed({ key: JSON.stringify(body), data }),
  });
  const submit = useMutation({
    mutationFn: (body: OptOrderIn) => api.optSubmit(body),
    onSuccess: (result) => {
      setSent(result);
      setPreviewed(null);
      refresh();
      if (result.status !== "rejected") onChange({ ...draft, legs: [], structure_id: null });
    },
  });

  const touch = () => {
    setSent(null);
    if (preview.isError) preview.reset();
    if (submit.isError) submit.reset();
  };
  const setLegs = (legs: TicketLeg[]) => {
    touch();
    onChange({ ...draft, legs, structure_id: legs.some((l) => l.effect === "close") ? draft.structure_id : null });
  };

  const current = previewed !== null && order !== null && previewed.key === key;
  const canSubmit = current && previewed.data.accepted;

  return (
    <div className="stack opt-gap">
      {draft.legs.length === 0 ? (
        <p className="muted">No legs yet. Click a bid in the chain to sell, or an ask to buy.</p>
      ) : (
        <ul className="opt-legs" aria-label="Legs">
          {draft.legs.map((leg, i) => (
            <LegRow
              key={`${leg.instrument}-${leg.contract_id ?? "shares"}`}
              leg={leg}
              index={i}
              onChange={(next) => setLegs(draft.legs.map((l, j) => (j === i ? next : l)))}
              onRemove={() => setLegs(draft.legs.filter((_, j) => j !== i))}
            />
          ))}
        </ul>
      )}
      <div className="row">
        <label htmlFor="opt-ticket-qty">
          Quantity
          <input
            id="opt-ticket-qty"
            className="opt-narrow"
            type="number"
            min={1}
            max={100}
            step={1}
            value={Number.isFinite(draft.qty) ? String(draft.qty) : ""}
            onChange={(e) => {
              touch();
              onChange({ ...draft, qty: e.target.value.trim() === "" ? Number.NaN : Number(e.target.value) });
            }}
          />
        </label>
        <label htmlFor="opt-ticket-type">
          Order type
          <select
            id="opt-ticket-type"
            value={orderType}
            onChange={(e) => {
              touch();
              setOrderType(e.target.value === "market" ? "market" : "limit");
            }}
          >
            <option value="limit">Limit</option>
            <option value="market">Market</option>
          </select>
        </label>
        <label htmlFor="opt-ticket-tif">
          Good for
          <select
            id="opt-ticket-tif"
            value={tif}
            onChange={(e) => {
              touch();
              setTif(e.target.value === "gtc" ? "gtc" : "day");
            }}
          >
            <option value="day">Day</option>
            <option value="gtc">Good till cancelled</option>
          </select>
        </label>
      </div>
      {orderType === "limit" && (
        <>
          <NetInput
            id="opt-ticket-limit"
            amount={amount}
            direction={direction}
            onAmount={(v) => {
              touch();
              setAmount(v);
            }}
            onDirection={(v) => {
              touch();
              setChosenDirection(v);
            }}
          />
          <label className="check-row">
            <input
              type="checkbox"
              checked={walk}
              onChange={(e) => {
                touch();
                setWalk(e.target.checked);
              }}
            />{" "}
            Walk the limit toward the market, one tick at a time
          </label>
        </>
      )}
      <p className="opt-terms" data-testid="opt-ticket-terms">
        {order ? termsWords(order) : "problem" in built ? built.problem : ""}
      </p>
      <div className="row">
        <Button disabled={order === null} busy={preview.isPending} onClick={() => order && preview.mutate(order)}>
          Preview
        </Button>
        <Button variant="primary" disabled={!canSubmit} busy={submit.isPending} onClick={() => order && canSubmit && submit.mutate(order)}>
          Submit
        </Button>
        {draft.legs.length > 0 && (
          <Button
            onClick={() => {
              touch();
              setPreviewed(null);
              onChange({ ...draft, legs: [], structure_id: null });
            }}
          >
            Clear
          </Button>
        )}
      </div>
      {previewed !== null && !current && (
        <p className="small tone-warn opt-notice" role="status">
          The order changed since the preview. Preview it again before submitting.
        </p>
      )}
      {(preview.isError || submit.isError) && (
        <div className="small tone-bad opt-notice" role="alert">
          {problemsOf(preview.isError ? preview.error : submit.error).map((m, i) => (
            <p key={i}>{m}</p>
          ))}
        </div>
      )}
      {previewed !== null && current && <PreviewView preview={previewed.data} />}
      {sent !== null &&
        (sent.status === "rejected" ? (
          <p className="tone-bad opt-notice" role="alert">
            Order {sent.id} was rejected. {sent.reject_reason ? REJECT_WORDS[sent.reject_reason] : ""} {sent.reject_detail ?? ""}
          </p>
        ) : (
          <p className="tone-ok opt-notice" role="status">
            Order {sent.id} is {sent.status}. It is on the Orders tab.
          </p>
        ))}
    </div>
  );
}
