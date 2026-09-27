// One catalyst from the pre-market scan (P4-T13). Headline URLs are scraped third-party data: only
// http(s) URLs become links (new tab, `rel="noopener noreferrer"`); anything else is plain text.
import type { CatalystOut } from "../../api/types";
import { Badge } from "../../components/ui";
import { fmtDate, fmtPct } from "../../lib/format";
import { safeHttpUrl } from "./labels";

export const MAX_HEADLINES = 5;

function confirmedText(confirmed: boolean | null): string {
  if (confirmed === null) return "Confirmed: unknown";
  return confirmed ? "Confirmed: yes" : "Confirmed: no";
}

export default function CatalystCard({ catalyst }: { catalyst: CatalystOut }) {
  const c = catalyst;
  return (
    <article className="card catalyst-card" aria-label={`Catalyst ${c.ticker}`}>
      <div className="row">
        <strong className="ticker">{c.ticker}</strong>
        <Badge tone="info">{c.catalyst_type}</Badge>
        <span className="small">{`Direction ${c.direction}`}</span>
      </div>
      <div className="row small num">
        <span>{`Quality ${c.quality ?? "n/a"}`}</span>
        <span>{confirmedText(c.confirmed)}</span>
        <span>{`Gap ${fmtPct(c.gap_pct)}`}</span>
        {c.earnings_date && <span>{`Earnings ${fmtDate(c.earnings_date)}`}</span>}
      </div>
      {c.reason && <p className="catalyst-reason">{c.reason}</p>}
      {c.headlines.length > 0 && (
        <ul className="headlines small">
          {c.headlines.slice(0, MAX_HEADLINES).map((h, i) => {
            const url = safeHttpUrl(h.url);
            return (
              <li key={i}>
                {url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer">
                    {h.title}
                  </a>
                ) : (
                  <span>{h.title}</span>
                )}
                {h.source && <span className="muted">{` · ${h.source}`}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </article>
  );
}
