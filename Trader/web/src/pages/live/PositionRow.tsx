// One open position on the dashboard (DB-T8): a compact tappable row (ticker, side and qty; entry → mark;
// stop / target; unrealised $ and R; time held; sparkline) that expands to its details and chart. A phone shows
// the ticker, unrealised $ and the sparkline; the rest (`.pos-detail`) is in the expansion (liveB.css).
import type { ReactNode } from "react";

import type { LivePositionOut } from "../../api/types";
import { MIN_TOUCH_PX } from "../../components/ui";
import { fmtDuration, fmtPrice, fmtR } from "../../lib/format";
import { Money as DecimalMoney } from "./PeriodPnl";
import { PositionChart } from "./PositionChart";
import { Sparkline } from "./Sparkline";

import "./liveB.css";

const DASH = "–";

/** A price, or a dash when there is none (a missing mark, `orb_sip`'s absent target: open question 8). */
function price(value: string | null): string {
  return value === null ? DASH : fmtPrice(value);
}

/** Unrealised $ through the shared decimal Money (a value that rounds to $0.00 is flat, never red). */
function Money({ value }: { value: string | null }) {
  if (value === null) return <span className="num muted">{DASH}</span>;
  return <DecimalMoney value={value} />;
}

export function StaleBadge({ title }: { title?: string }) {
  return (
    <span className="live-badge warn" title={title}>
      prices stale
    </span>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="pos-dl-item">
      <dt>{label}</dt>
      <dd className="num">{children}</dd>
    </div>
  );
}

export function PositionRow({ p, expanded, onToggle }: { p: LivePositionOut; expanded: boolean; onToggle: () => void }) {
  const stale = p.mark_state !== "live";
  const classes = ["pos-row", p.near_stop ? "near-stop" : "", stale ? "is-stale" : "", expanded ? "is-expanded" : ""].filter(Boolean).join(" ");
  const panelId = `pos-${p.id}-details`;
  return (
    <li className={classes} data-ticker={p.ticker} data-id={p.id}>
      <button type="button" className="pos-summary" aria-expanded={expanded} aria-controls={expanded ? panelId : undefined} onClick={onToggle} style={{ minHeight: MIN_TOUCH_PX }}>
        <span className="pos-cell pos-name">
          <span className="pos-ticker">{p.ticker}</span>{" "}
          <span className="pos-detail small muted">{`${p.side} ${p.qty}`}</span>{" "}
          {p.near_stop && <span className="live-sr-only">near stop</span>}{" "}
          {stale && <StaleBadge />}
        </span>{" "}
        <span className="pos-cell pos-detail num">{`${price(p.entry)} → ${price(p.mark)}`}</span>{" "}
        <span className="pos-cell pos-detail num">{`${price(p.stop)} / ${price(p.target)}`}</span>{" "}
        <span className="pos-cell pos-pnl">
          <Money value={p.unrealized} />{" "}
          <span className="pos-detail num small muted">{p.unrealized_r === null ? DASH : fmtR(p.unrealized_r)}</span>
        </span>{" "}
        <span className="pos-cell pos-detail num small muted">{fmtDuration(p.held_seconds)}</span>{" "}
        <span className="pos-cell pos-spark">
          <Sparkline points={p.spark} entry={p.entry} stop={p.stop} label={`${p.ticker} since entry`} />
        </span>
      </button>
      {expanded && (
        <div className="pos-expanded" id={panelId}>
          <dl className="pos-dl" role="group" aria-label={`${p.ticker} details`}>
            <Detail label="Side">{`${p.side} ${p.qty}`}</Detail>
            <Detail label="Entry">{price(p.entry)}</Detail>
            <Detail label="Mark">{price(p.mark)}</Detail>
            <Detail label="Stop">{price(p.stop)}</Detail>
            <Detail label="Target">{price(p.target)}</Detail>
            <Detail label="Unrealised">
              <Money value={p.unrealized} />
            </Detail>
            <Detail label="R">{p.unrealized_r === null ? DASH : fmtR(p.unrealized_r)}</Detail>
            <Detail label="To stop">{p.distance_to_stop_r === null ? DASH : fmtR(p.distance_to_stop_r).replace(/^\+/, "")}</Detail>
            <Detail label="Held">{fmtDuration(p.held_seconds)}</Detail>
            <Detail label="Strategy">{p.strategy_key}</Detail>
          </dl>
          <PositionChart p={p} />
        </div>
      )}
    </li>
  );
}
