// The Control page's Soak card (live dashboard design §4.6, plan DB-T9): the clean-day count n / target, day
// one, the earliest finish, today's verdict so far and the last final day, from the read-only soak report.
import type { SoakSummaryOut } from "../../api/types";
import { fmtDate } from "../../lib/format";
import { Panel } from "../live/Panel";
import { Facts, StatusChip, cardError } from "./parts";

function Today({ soak }: { soak: SoakSummaryOut }) {
  const t = soak.today;
  if (!t) return <span className="ctl-muted">no verdict for today</span>;
  return (
    <span className="ctl-stack">
      <span className="ctl-row">
        <span className="ctl-text">{t.verdict}</span>
        <span className="ctl-small ctl-muted">{`(${t.provisional ? "provisional" : "final"}, ${fmtDate(t.session_date)})`}</span>
      </span>
      {t.failed.length > 0 && <span className="ctl-small ctl-bad ctl-text">{`failed: ${t.failed.join(", ")}`}</span>}
    </span>
  );
}

export function SoakCard({ soak, error, onRetry }: { soak: SoakSummaryOut | null; error?: string | null; onRetry?: () => void }) {
  const done = soak !== null && soak.consecutive_clean >= soak.target;
  return (
    <Panel
      title="Soak"
      error={cardError(soak, error)}
      onRetry={onRetry}
      badge={soak ? <StatusChip tone={done ? "ok" : "muted"}>{done ? "target reached" : "in progress"}</StatusChip> : undefined}
    >
      {soak && (
        <div className="ctl-stack">
          <p className="ctl-big num">{`${soak.consecutive_clean} / ${soak.target} clean`}</p>
          <Facts
            rows={[
              ["Day 1", soak.day_one ? fmtDate(soak.day_one) : <span className="ctl-muted">no clean day yet</span>],
              ["Earliest finish", soak.earliest_finish ? fmtDate(soak.earliest_finish) : "n/a"],
              ["Today so far", <Today key="today" soak={soak} />],
              ["Last final day", soak.last_final ? fmtDate(soak.last_final) : "none yet"],
              ["Clean days in total", String(soak.total_clean)],
            ]}
          />
        </div>
      )}
    </Panel>
  );
}
