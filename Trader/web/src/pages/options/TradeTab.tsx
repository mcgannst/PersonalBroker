// Options, Trade tab (OPTSIM-T15): pick an underlying, then an expiry; the chain shows calls and puts and a
// click on a bid or an ask adds a leg to the ticket below it. The chain's quotes refresh every 5 s while the
// tab is open.
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { OPT_FAST_MS, OPT_SLOW_MS, oqk, useOptionsApi } from "../../api/optionsClient";
import { Badge, Button, Card, Empty, Loading } from "../../components/ui";
import { fmtPrice, fmtTime } from "../../lib/format";
import { ChainTable } from "./ChainTable";
import { OptErrorBox, type TicketDraft, type TicketLeg } from "./shared";
import { Ticket } from "./Ticket";

const TICKER_RE = /^[A-Z][A-Z0-9.-]{0,9}$/;

/** The draft with one more leg. A second click on the same contract replaces its leg (bid then ask flips it). */
export function withLeg(draft: TicketDraft, underlying: string, leg: TicketLeg): TicketDraft {
  const base: TicketDraft = draft.underlying === underlying ? draft : { underlying, structure_id: null, legs: [], qty: 1 };
  const same = (l: TicketLeg) => l.instrument === leg.instrument && l.contract_id === leg.contract_id;
  const legs = base.legs.some(same) ? base.legs.map((l) => (same(l) ? { ...leg, effect: l.effect } : l)) : [...base.legs, leg];
  return { ...base, legs };
}

function Chain({ underlying, onLeg }: { underlying: string; onLeg: (leg: TicketLeg) => void }) {
  const api = useOptionsApi();
  const [picked, setPicked] = useState<string | null>(null);
  const chain = useQuery({ queryKey: oqk.chain(underlying), queryFn: () => api.optChain({ underlying }), refetchInterval: OPT_SLOW_MS });
  const expiries = chain.data?.expiries ?? [];
  const expiry = expiries.some((e) => e.expiry === picked) ? picked : (expiries[0]?.expiry ?? null);
  const quotesQuery = { underlying, expiry: expiry ?? "" };
  const quotes = useQuery({
    queryKey: oqk.chainQuotes(quotesQuery),
    queryFn: () => api.optChainQuotes(quotesQuery),
    enabled: expiry !== null,
    refetchInterval: OPT_FAST_MS,
  });

  if (chain.isPending) return <Loading />;
  if (chain.isError) return <OptErrorBox error={chain.error} onRetry={() => void chain.refetch()} />;
  const data = chain.data;
  return (
    <div className="stack opt-gap">
      <div className="row">
        <strong>{data.underlying}</strong>
        <span className="num">{data.underlying_price === null ? "no price" : fmtPrice(data.underlying_price)}</span>
        {data.price_time && <span className="small muted">at {fmtTime(data.price_time)}</span>}
        <Badge tone={data.market_open ? "ok" : "muted"}>{data.market_open ? "market open" : "market closed"}</Badge>
        <Button onClick={() => onLeg({ instrument: "shares", contract_id: null, label: `${data.underlying} shares`, side: "buy", effect: "open", ratio: 100 })}>
          Add 100 shares
        </Button>
      </div>
      {!data.market_open && <p className="small muted">The market is closed: quotes are stale and nothing fills until it opens.</p>}
      {expiries.length === 0 ? (
        <Empty>No option expiries for {data.underlying}</Empty>
      ) : (
        <>
          <label htmlFor="opt-expiry">
            Expiry
            <select id="opt-expiry" value={expiry ?? ""} onChange={(e) => setPicked(e.target.value)}>
              {expiries.map((e) => (
                <option key={e.expiry} value={e.expiry}>
                  {`${e.expiry} · ${e.dte} days${e.is_monthly ? " · monthly" : ""}`}
                </option>
              ))}
            </select>
          </label>
          {quotes.isPending ? (
            <Loading />
          ) : quotes.isError ? (
            <OptErrorBox error={quotes.error} onRetry={() => void quotes.refetch()} />
          ) : (
            <>
              <p className="small muted">
                Quotes from {fmtTime(quotes.data.fetched_at)}. Click a bid to sell that contract, an ask to buy it.
              </p>
              <ChainTable quotes={quotes.data} onLeg={onLeg} />
            </>
          )}
        </>
      )}
    </div>
  );
}

export function TradeTab({ draft, onDraft }: { draft: TicketDraft; onDraft: (draft: TicketDraft) => void }) {
  const [text, setText] = useState(draft.underlying);
  const wanted = text.trim().toUpperCase();
  const valid = TICKER_RE.test(wanted);
  const underlying = draft.underlying;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!valid || wanted === underlying) return;
    onDraft({ underlying: wanted, structure_id: null, legs: [], qty: 1 });
  };

  return (
    <div className="stack opt-gap">
      <Card title="Chain">
        <form className="row" onSubmit={onSubmit}>
          <label htmlFor="opt-underlying">
            Underlying
            <input id="opt-underlying" className="opt-narrow" type="text" autoComplete="off" autoCapitalize="characters" value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <Button type="submit" variant="primary" disabled={!valid} className="opt-form-btn">
            Load chain
          </Button>
        </form>
        {underlying ? <Chain underlying={underlying} onLeg={(leg) => onDraft(withLeg(draft, underlying, leg))} /> : <Empty>Type a ticker to see its option chain</Empty>}
      </Card>
      <Card title={underlying ? `Ticket: ${underlying}` : "Ticket"}>
        <Ticket draft={draft} onChange={onDraft} />
      </Card>
    </div>
  );
}
