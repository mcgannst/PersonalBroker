// A table of closed trades; each row opens the position's detail (`/trades?position=<id>`).
import { Link, useNavigate } from "react-router-dom";

import type { TradeOut } from "../../api/types";
import { Table } from "../../components/ui";
import { fmtDate, fmtMoney, fmtPrice, fmtR } from "../../lib/format";

export function positionHref(positionId: number): string {
  return `/trades?position=${positionId}`;
}

function pnlClass(pnl: string): string {
  if (pnl.trim().startsWith("-")) return "num tone-bad";
  return /[1-9]/.test(pnl) ? "num tone-ok" : "num";
}

export function TradeList({ trades }: { trades: TradeOut[] }) {
  const navigate = useNavigate();
  return (
    <Table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Ticker</th>
          <th className="num">Qty</th>
          <th className="num">Entry</th>
          <th className="num">Exit</th>
          <th className="num">P&amp;L</th>
          <th className="num">R</th>
          <th>Exit reason</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id} onClick={() => navigate(positionHref(t.position_id))} style={{ cursor: "pointer" }}>
            <td>{fmtDate(t.session_date)}</td>
            <td>
              <Link className="link-touch" to={positionHref(t.position_id)} onClick={(e) => e.stopPropagation()}>
                {t.ticker}
              </Link>
            </td>
            <td className="num">{t.qty}</td>
            <td className="num">{fmtPrice(t.entry_price)}</td>
            <td className="num">{fmtPrice(t.exit_price)}</td>
            <td className={pnlClass(t.pnl)}>{fmtMoney(t.pnl)}</td>
            <td className="num">{fmtR(t.pnl_r)}</td>
            <td>{t.exit_reason}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
