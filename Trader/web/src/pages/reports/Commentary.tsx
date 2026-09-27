// The weekly Claude commentary on the Reports page (P5-T13; BR-61, SPEC §12). The text comes from Claude and is
// untrusted: it is rendered only as React text children (escaped), split into paragraphs on blank lines, and
// never parsed as HTML, Markdown or links.
import { useQuery } from "@tanstack/react-query";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { CommentaryStatus, IsoDate } from "../../api/types";
import { Card, ErrorBox } from "../../components/ui";
import { fmtDateTime, fmtPrice } from "../../lib/format";

/** Why a stored report has no commentary, one line per non-`ok` status. */
export const STATUS_REASONS: Record<Exclude<CommentaryStatus, "ok">, string> = {
  budget: "The daily Claude budget was used up",
  disabled: "Claude is not configured",
  rejected: "The commentary quoted numbers that are not in the report, so it was withheld",
  error: "Claude failed",
};

export const NO_REPORT_TEXT = "No weekly report for this week yet (it is written on Saturday morning).";

/** The commentary's paragraphs: split on blank lines, each trimmed, empty ones dropped. */
export function commentaryParagraphs(text: string | null | undefined): string[] {
  if (!text) return [];
  return text
    .replace(/\r\n?/g, "\n")
    .split(/\n[ \t]*\n/)
    .map((p) => p.trim())
    .filter((p) => p !== "");
}

function Reason({ children }: { children: string }) {
  return <p className="small muted">{children}</p>;
}

/** The commentary of the weekly report for the week starting `monday` (refreshed by the `reports` topic). */
export function Commentary({ monday }: { monday: IsoDate }) {
  const api = useApi();
  const report = useQuery({ queryKey: qk.weeklyReport(monday), queryFn: () => api.weeklyReport(monday) });

  if (report.isPending) return null;
  if (report.isError) return <ErrorBox error={report.error} onRetry={() => void report.refetch()} />;
  const data = report.data;
  if (data === null) return <Reason>{NO_REPORT_TEXT}</Reason>;

  const paragraphs = commentaryParagraphs(data.commentary);
  if (data.commentary_status !== "ok") return <Reason>{STATUS_REASONS[data.commentary_status]}</Reason>;
  if (paragraphs.length === 0) return <Reason>{STATUS_REASONS.error}</Reason>;

  return (
    <div role="region" aria-label="Commentary">
      <Card title="Commentary">
        {paragraphs.map((p, i) => (
          <p key={i} className="commentary-paragraph" style={{ whiteSpace: "pre-line", overflowWrap: "anywhere" }}>
            {p}
          </p>
        ))}
        <p className="small muted">
          Generated {fmtDateTime(data.created_at)} by {data.model ?? "Claude"}, US${fmtPrice(data.cost_usd)}
        </p>
      </Card>
    </div>
  );
}
