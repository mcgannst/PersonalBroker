// Today's P&L tiles: realized, unrealized (marked partial when a quote failed), week to date, equity and
// drawdown (P4-T13).
import type { PnlOut } from "../../api/types";
import { Stat } from "../../components/ui";
import { fmtMoney, fmtPct } from "../../lib/format";
import { unsignedPct } from "./labels";

export default function PnlTiles({ pnl }: { pnl: PnlOut }) {
  return (
    <section className="card" aria-label="P&L">
      <h2 className="card-title">P&amp;L</h2>
      <div className="tiles">
        <Stat label="Realized today" value={fmtMoney(pnl.realized_today)} />
        <Stat label="Unrealized" value={fmtMoney(pnl.unrealized)} sub={pnl.unrealized_partial ? "partial" : undefined} />
        <Stat label="Week to date" value={fmtMoney(pnl.week_to_date)} />
        <Stat label="Equity" value={fmtMoney(pnl.equity)} sub={`peak ${fmtMoney(pnl.peak_equity)}`} />
        <Stat label="Drawdown" value={unsignedPct(fmtPct(pnl.drawdown_pct))} />
      </div>
    </section>
  );
}
