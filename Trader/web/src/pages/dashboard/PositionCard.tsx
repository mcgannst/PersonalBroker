// One open position: prices, unrealized P&L, the stop (flagged when no stop order is working) and the
// time it has been unprotected (BR-33), with a link to its trade detail (P4-T13).
import { Link } from "react-router-dom";

import type { PositionOut } from "../../api/types";
import { fmtDuration, fmtMoney, fmtPrice } from "../../lib/format";

export default function PositionCard({ position }: { position: PositionOut }) {
  const p = position;
  return (
    <article className="card position-card" aria-label={`Position ${p.ticker}`}>
      <div className="row">
        <strong className="ticker">{p.ticker}</strong>
        <span className="num">{`${p.qty} shares`}</span>
        <span className="small muted">{p.strategy_key}</span>
      </div>
      <div className="row small num">
        <span>{`Entry ${fmtPrice(p.entry)}`}</span>
        <span>{`Last ${fmtPrice(p.last)}`}</span>
      </div>
      <div className="row">
        <span className="small muted">Unrealized</span>
        <span className="num">{fmtMoney(p.unrealized_pnl)}</span>
      </div>
      <div className="row small num">
        <span>{`Stop ${fmtPrice(p.stop)}`}</span>
        {!p.stop_working && <span className="text-tone tone-bad">(no stop order)</span>}
      </div>
      <p className={p.stop_working ? "small muted num" : "small num text-tone tone-bad"}>
        {`Unprotected ${fmtDuration(p.unprotected_seconds)}`}
      </p>
      <Link className="link-touch" to={`/trades?position=${p.id}`}>
        Details
      </Link>
    </article>
  );
}
