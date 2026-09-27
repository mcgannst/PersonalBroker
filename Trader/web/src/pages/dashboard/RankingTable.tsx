// The 9:35 ranking: rank, ticker, rvol, passed or the reject reason, strategy (P4-T13). The table
// scrolls sideways inside its wrapper on a phone.
import type { CandidateOut } from "../../api/types";
import { Table } from "../../components/ui";
import { fmtPrice } from "../../lib/format";

function byRank(a: CandidateOut, b: CandidateOut): number {
  if (a.rank === b.rank) return a.id - b.id;
  if (a.rank === null) return 1;
  if (b.rank === null) return -1;
  return a.rank - b.rank;
}

export default function RankingTable({ ranking }: { ranking: CandidateOut[] }) {
  const rows = [...ranking].sort(byRank);
  return (
    <Table aria-label="Ranking">
      <thead>
        <tr>
          <th scope="col">Rank</th>
          <th scope="col">Ticker</th>
          <th scope="col">RVOL</th>
          <th scope="col">Result</th>
          <th scope="col">Strategy</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((c) => (
          <tr key={c.id}>
            <td>{c.rank ?? "–"}</td>
            <td>{c.ticker}</td>
            <td>{fmtPrice(c.rvol)}</td>
            <td className={c.passed ? "text-tone tone-ok" : "muted"}>{c.passed ? "✓" : (c.reject_reason ?? "rejected")}</td>
            <td>{c.strategy_key}</td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
