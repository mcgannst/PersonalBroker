// The replay compared with the live run over the same dates (BR-52, BR-54): one row per metric, columns
// Replay, Live (same dates) and Difference. Differences are exact decimal subtractions (`decimalDiff`), never
// floating point; win rate and drawdown differences are in percentage points.
import type { MetricsOut } from "../../api/types";
import { Table } from "../../components/ui";
import { fmtMoney, fmtPct, fmtPrice, fmtR, fmtRate } from "../../lib/format";
import { decimalDiff } from "./shared";

const NA = "n/a";

/** Adds a plus sign to a formatted positive value (formatters that are unsigned for positives). */
function plus(text: string, raw: string): string {
  if (text === NA || text.startsWith("-") || text.startsWith("+")) return text;
  return /[1-9]/.test(raw) ? `+${text}` : text;
}

function slippage(value: string | null | undefined): string {
  const p = fmtPrice(value);
  if (p === NA) return p;
  return p.startsWith("-") ? `-$${p.slice(1)}` : `$${p}`;
}

interface Row {
  label: string;
  value: (m: MetricsOut) => string | null;
  show: (v: string | null) => string;
  /** The difference as text (signed). */
  diff: (d: string | null) => string;
}

const ROWS: Row[] = [
  {
    label: "Trades",
    value: (m) => String(m.trades),
    show: (v) => v ?? NA,
    diff: (d) => (d === null ? NA : plus(d, d)),
  },
  { label: "Win rate", value: (m) => m.win_rate, show: fmtRate, diff: (d) => fmtPct(d) },
  { label: "Expectancy", value: (m) => m.expectancy_r, show: fmtR, diff: (d) => fmtR(d) },
  { label: "Profit factor", value: (m) => m.profit_factor, show: fmtPrice, diff: (d) => (d === null ? NA : plus(fmtPrice(d), d)) },
  { label: "Total P&L", value: (m) => m.total_pnl, show: fmtMoney, diff: (d) => (d === null ? NA : plus(fmtMoney(d), d)) },
  { label: "Max drawdown", value: (m) => m.max_drawdown_pct, show: fmtRate, diff: (d) => fmtPct(d) },
  { label: "Avg slippage", value: (m) => m.avg_slippage, show: slippage, diff: (d) => (d === null ? NA : plus(slippage(d), d)) },
];

export function CompareTable({ replay, live }: { replay: MetricsOut; live: MetricsOut | null }) {
  return (
    <Table aria-label="Replay compared with live">
      <thead>
        <tr>
          <th scope="col">Metric</th>
          <th scope="col" className="num">
            Replay
          </th>
          <th scope="col" className="num">
            Live (same dates)
          </th>
          <th scope="col" className="num">
            Difference
          </th>
        </tr>
      </thead>
      <tbody>
        {ROWS.map((row) => {
          const a = row.value(replay);
          const b = live ? row.value(live) : null;
          return (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              <td className="num">{row.show(a)}</td>
              <td className="num">{live ? row.show(b) : NA}</td>
              <td className="num">{row.diff(decimalDiff(a, b))}</td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
