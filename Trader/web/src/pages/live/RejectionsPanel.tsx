// "Rejected today, and why" (DB-T8, design D5, plan S8): the day's rejection counts by stage and rule, largest
// first; a tap on a rule shows its tickers, each linking to Reports → Day filtered to it. Full per-candidate
// detail stays on the Day view. Every rule and ticker is plain text.
import { useState } from "react";

import type { RejectionRuleOut, RejectionsOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { fmtTime } from "../../lib/format";
import { Panel } from "./Panel";
import { SafeLink } from "./safeLink";

import "./liveB.css";

const TITLE = "Rejected today, and why";

function ruleKey(r: RejectionRuleOut): string {
  return `${r.stage}:${r.rule}`;
}

function ruleLabel(r: RejectionRuleOut): string {
  return `${r.stage} · ${r.rule}`;
}

/** The Day view link for one ticker of a rule (S8: the rule's link plus `&ticker=<T>`). */
export function tickerLink(rule: RejectionRuleOut, ticker: string): string {
  const sep = rule.link.includes("?") ? "&" : "?";
  return `${rule.link}${sep}ticker=${encodeURIComponent(ticker)}`;
}

function RuleRow({ rule, open, onToggle }: { rule: RejectionRuleOut; open: boolean; onToggle: () => void }) {
  const label = ruleLabel(rule);
  const id = `rej-${rule.stage}-${rule.rule}`.replace(/[^A-Za-z0-9_-]/g, "_");
  const more = rule.count - rule.tickers.length;
  return (
    <li className="rej-rule">
      <button type="button" className="rej-toggle" aria-expanded={open} aria-controls={open ? id : undefined} onClick={onToggle} style={{ minHeight: MIN_TOUCH_PX }}>
        <span className="rej-label">{label}</span>
        <span className="rej-count num">{rule.count}</span>
      </button>
      {open && (
        <div className="rej-tickers" id={id}>
          <ul className="rej-ticker-list" aria-label={`${label} tickers`}>
            {rule.tickers.map((t, i) => (
              // a ticker can repeat within a rule (two candidates, one symbol): the index keeps keys unique
              <li key={`${i}:${t}`}>
                <SafeLink className="link-touch" to={tickerLink(rule, t)}>
                  {t}
                </SafeLink>
              </li>
            ))}
          </ul>
          {rule.truncated && more > 0 && <p className="muted small">{`+${more} more`}</p>}
          <SafeLink className="link-touch small" to={rule.link} aria-label={`Open ${label} in Reports`}>
            Day view
          </SafeLink>
        </div>
      )}
    </li>
  );
}

export function RejectionsPanel({ rejections, error, onRetry }: { rejections: RejectionsOut | null; error?: string | null; onRetry?: () => void }) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());

  if (rejections === null) {
    return <Panel title={TITLE} error={error ?? "Rejections not available"} onRetry={onRetry} />;
  }

  if (rejections.rules.length === 0) {
    return <Panel title={TITLE} empty="No rejections recorded today" />;
  }

  // Largest count first; ties keep the server's order (Array.prototype.sort is stable).
  const rules = [...rejections.rules].sort((a, b) => b.count - a.count);
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  const badge = <span className="small muted num">{`${rejections.total} rejected`}</span>;

  return (
    <Panel title={TITLE} badge={badge}>
      {rejections.source === "candidates" && <p className="muted small rej-note">from candidates, decision log not recorded yet</p>}
      <ul className="rej-list" aria-label="Rejection rules">
        {rules.map((r) => (
          <RuleRow key={ruleKey(r)} rule={r} open={open.has(ruleKey(r))} onToggle={() => toggle(ruleKey(r))} />
        ))}
      </ul>
      {rejections.recorded_at && <p className="muted small rej-note">{`as of ${fmtTime(rejections.recorded_at)}`}</p>}
    </Panel>
  );
}
