// The option chain of one expiry (OPTSIM-T15): calls on the left, puts on the right, one row per strike.
// Clicking a bid adds a sell leg, an ask a buy leg. A stale quote (too old to fill, or the market is closed)
// is marked and dimmed.
import type { Money, OptChainQuotesOut, OptQuoteOut, OptRight, Side } from "../../api/types";
import { Badge, Button, Empty, Table } from "../../components/ui";
import { fmtPrice, fmtRate } from "../../lib/format";
import { contractLabel, type TicketLeg } from "./shared";

const DASH = "–";

function count(n: number | null): string {
  return n === null ? DASH : n.toLocaleString("en-US");
}

function PriceButton({ label, side, price, onLeg }: { label: string; side: Side; price: Money | null; onLeg: () => void }) {
  if (price === null) return <span className="muted">{DASH}</span>;
  const name = side === "sell" ? `Sell ${label} at the bid ${fmtPrice(price)}` : `Buy ${label} at the ask ${fmtPrice(price)}`;
  return (
    <Button className="opt-quote-btn" aria-label={name} onClick={onLeg}>
      {fmtPrice(price)}
    </Button>
  );
}

/** The seven cells of one side of a row. Calls read outward to the left, puts to the right. */
function SideCells({
  right,
  contractId,
  quote,
  label,
  onLeg,
}: {
  right: OptRight;
  contractId: number | null;
  quote: OptQuoteOut | null;
  label: string;
  onLeg: (leg: TicketLeg) => void;
}) {
  if (contractId === null || quote === null) {
    return (
      <td colSpan={7} className="muted">
        {contractId === null ? "not listed" : "no quote"}
      </td>
    );
  }
  const leg = (side: Side): TicketLeg => ({ instrument: "option", contract_id: contractId, label, side, effect: "open", ratio: 1 });
  const stale = quote.stale ? "opt-stale" : undefined;
  const cells = [
    <td key="bid" className={stale}>
      <PriceButton label={label} side="sell" price={quote.bid} onLeg={() => onLeg(leg("sell"))} />
    </td>,
    <td key="ask" className={stale}>
      <PriceButton label={label} side="buy" price={quote.ask} onLeg={() => onLeg(leg("buy"))} />
    </td>,
    <td key="last" className={stale}>
      {quote.last === null ? DASH : fmtPrice(quote.last)} {quote.stale && <Badge tone="warn">stale</Badge>}
    </td>,
    <td key="delta" className={stale}>
      {quote.delta === null ? DASH : fmtPrice(quote.delta)}
    </td>,
    <td key="iv" className={stale}>
      {quote.iv === null ? DASH : fmtRate(quote.iv)}
    </td>,
    <td key="oi" className={stale}>
      {count(quote.open_interest)}
    </td>,
    <td key="vol" className={stale}>
      {count(quote.volume)}
    </td>,
  ];
  return <>{right === "call" ? cells.reverse() : cells}</>;
}

const PUT_HEADS = ["Bid", "Ask", "Last", "Delta", "IV", "Open int.", "Volume"];
const CALL_HEADS = [...PUT_HEADS].reverse();

export function ChainTable({ quotes, onLeg }: { quotes: OptChainQuotesOut; onLeg: (leg: TicketLeg) => void }) {
  if (quotes.rows.length === 0) return <Empty>No strikes for this expiry</Empty>;
  return (
    <Table className="opt-chain" aria-label={`${quotes.underlying} ${quotes.expiry} chain`}>
      <thead>
        <tr>
          <th colSpan={7}>Calls</th>
          <th />
          <th colSpan={7}>Puts</th>
        </tr>
        <tr>
          {CALL_HEADS.map((h) => (
            <th key={`c-${h}`}>{h}</th>
          ))}
          <th>Strike</th>
          {PUT_HEADS.map((h) => (
            <th key={`p-${h}`}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {quotes.rows.map((row) => (
          <tr key={row.strike}>
            <SideCells
              right="call"
              contractId={row.call_contract_id}
              quote={row.call}
              label={contractLabel(quotes.underlying, quotes.expiry, "call", row.strike)}
              onLeg={onLeg}
            />
            <th scope="row" className="opt-strike">
              {fmtPrice(row.strike)}
            </th>
            <SideCells
              right="put"
              contractId={row.put_contract_id}
              quote={row.put}
              label={contractLabel(quotes.underlying, quotes.expiry, "put", row.strike)}
              onLeg={onLeg}
            />
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
