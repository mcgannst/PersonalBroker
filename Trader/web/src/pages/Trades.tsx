// Trades (SPEC §12): the trade history (newest first, 50 a page, date-range filter) and, at
// `/trades?position=<id>` (the Telegram entry-fill and trade links), one position's full chain.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useApi, type TradesQuery } from "../api/client";
import { qk } from "../api/queryKeys";
import { Button, Card, Empty, ErrorBox, Loading } from "../components/ui";
import { PositionDetail } from "./trades/PositionDetail";
import { TradeList } from "./trades/TradeList";

export const TRADES_PAGE_SIZE = 50;

/** A positive integer id from a query-string value, else null. */
export function parseId(value: string | null): number | null {
  if (value === null || !/^\d{1,15}$/.test(value)) return null;
  const n = Number(value);
  return n > 0 ? n : null;
}

function TradeHistory() {
  const api = useApi();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const query: TradesQuery = {
    ...(from ? { from } : {}),
    ...(to ? { to } : {}),
    limit: TRADES_PAGE_SIZE,
    offset,
  };
  const q = useQuery({ queryKey: qk.trades(query), queryFn: () => api.trades(query), placeholderData: keepPreviousData });
  const items = q.data?.items ?? [];
  const setRange = (which: "from" | "to", value: string) => {
    (which === "from" ? setFrom : setTo)(value);
    setOffset(0);
  };
  return (
    <Card title="History">
      <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
        <label>
          From
          <input type="date" value={from} onChange={(e) => setRange("from", e.target.value)} />
        </label>
        <label>
          To
          <input type="date" value={to} onChange={(e) => setRange("to", e.target.value)} />
        </label>
      </div>
      {q.isPending ? (
        <Loading />
      ) : q.isError ? (
        <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : items.length === 0 ? (
        <Empty>{offset > 0 ? "No more trades." : "No trades yet"}</Empty>
      ) : (
        <TradeList trades={items} />
      )}
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <Button disabled={offset === 0 || q.isFetching} onClick={() => setOffset(Math.max(0, offset - TRADES_PAGE_SIZE))}>
          Previous
        </Button>
        <Button disabled={items.length < TRADES_PAGE_SIZE || q.isFetching} onClick={() => setOffset(offset + TRADES_PAGE_SIZE)}>
          Next
        </Button>
      </div>
    </Card>
  );
}

export default function TradesPage() {
  const [params] = useSearchParams();
  const raw = params.get("position");
  const id = parseId(raw);
  return (
    <main className="page">
      <h1>Trades</h1>
      {raw === null ? (
        <TradeHistory />
      ) : (
        <>
          <p>
            <Link to="/trades">← Back to trades</Link>
          </p>
          {id === null ? <Empty>“{raw}” is not a valid position id.</Empty> : <PositionDetail id={id} />}
        </>
      )}
    </main>
  );
}
