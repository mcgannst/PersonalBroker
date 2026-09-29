// The Reports page "Day" view (P6-T12; SPEC §12): one session's decision log. `/reports?day=YYYY-MM-DD`
// (`&run=<id>` for another run, e.g. a replay; without it the server serves only a live run), `?day=` alone
// opens the latest day with rows. A date picker and the recent days, the day's summary, filters (stage,
// outcome, ticker), a table (MT time, stage, ticker, outcome, rule, reason) whose rows expand to their
// checks and data, and a "Download CSV" link. Every stored text (reasons, Claude's rationale, notes) is
// rendered as plain text: React escapes it, nothing is ever inserted as HTML.
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useApi, type DecisionDayQuery, type DecisionDaysQuery } from "../../api/client";
import { qk } from "../../api/queryKeys";
import {
  DECISION_STAGES,
  type CheckOut,
  type DecisionDayOut,
  type DecisionOutcome,
  type DecisionRowOut,
  type DecisionStage,
  type DecisionSummaryOut,
  type IsoDate,
} from "../../api/types";
import { Badge, Button, Card, Empty, ErrorBox, Loading, Stat, Table } from "../../components/ui";
import { fmtDateTime, fmtMoney, fmtPrice, fmtR, fmtTime } from "../../lib/format";
import { isIsoDate } from "../performance/dates";

/** Rows per page (a day has ~800 scan rows). */
export const DAY_PAGE_SIZE = 200;
/** How many recent days the picker lists. */
export const RECENT_DAYS = 60;

/** Every `DecisionOutcome`, in the schema's order (for the filter). */
export const DECISION_OUTCOMES: readonly DecisionOutcome[] = [
  "info",
  "listed",
  "classified",
  "passed",
  "rejected",
  "proposed",
  "approved",
  "auto_approved",
  "declined",
  "expired",
  "blocked",
  "submitted",
  "filled",
  "cancelled",
  "exited",
  "tripped",
  "reset",
  "error",
];

const STAGE_LABEL: Record<DecisionStage, string> = {
  universe: "Universe",
  premarket: "Pre-market",
  scan: "9:35 scan",
  signal: "Signal",
  risk: "Risk",
  proposal: "Proposal",
  approval: "Approval",
  order: "Order",
  fill: "Fill",
  exit: "Exit",
  overlay: "Overlay",
  kill_switch: "Kill switch",
  day: "Day",
};

const TONE: Partial<Record<DecisionOutcome, "ok" | "warn" | "bad" | "info" | "muted">> = {
  passed: "ok",
  approved: "ok",
  auto_approved: "ok",
  filled: "ok",
  rejected: "warn",
  declined: "warn",
  expired: "warn",
  cancelled: "warn",
  blocked: "bad",
  tripped: "bad",
  error: "bad",
};

/** One item per date, the first kept. The server already lists a date once, as the run a date-only request
 * serves (two live runs can record the same session), so picking the date opens that run; this guards the
 * picker (unique option values) whatever the list holds. */
export function uniqueDays<T extends { session_date: IsoDate }>(days: readonly T[]): T[] {
  const seen = new Set<IsoDate>();
  return days.filter((d) => {
    if (seen.has(d.session_date)) return false;
    seen.add(d.session_date);
    return true;
  });
}

/** The Day view's URL (`run` kept when given). */
export function dayHref(day: IsoDate | "", runId: number | null): string {
  return runId === null ? `/reports?day=${day}` : `/reports?day=${day}&run=${runId}`;
}

function checkResult(passed: boolean | null): string {
  if (passed === null) return "n/a";
  return passed ? "pass" : "fail";
}

function Checks({ checks }: { checks: CheckOut[] }) {
  if (checks.length === 0) return <p className="small muted">No filter checks for this decision.</p>;
  return (
    <Table aria-label="Checks">
      <thead>
        <tr>
          <th>Check</th>
          <th>Value</th>
          <th>Op</th>
          <th>Threshold</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>
        {checks.map((c, i) => (
          <tr key={`${c.name}-${i}`}>
            <td>{c.name}</td>
            <td>{c.value ?? "n/a"}</td>
            <td>{c.op}</td>
            <td>{c.threshold ?? "n/a"}</td>
            <td>
              <Badge tone={c.passed === null ? "muted" : c.passed ? "ok" : "bad"}>{checkResult(c.passed)}</Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function RowDetail({ row }: { row: DecisionRowOut }) {
  const hasData = Object.keys(row.data).length > 0;
  return (
    <div className="stack" role="region" aria-label={`Details of decision ${row.seq}`}>
      <Checks checks={row.checks} />
      {row.reason && <p className="small">{row.reason}</p>}
      {hasData && <pre className="small">{JSON.stringify(row.data, null, 2)}</pre>}
      <p className="small muted">
        {fmtDateTime(row.ts)}
        {row.strategy_key ? ` · ${row.strategy_key}` : ""}
        {Object.keys(row.ref).length > 0 ? ` · ${Object.entries(row.ref).map(([k, v]) => `${k} ${v}`).join(", ")}` : ""}
      </p>
    </div>
  );
}

function RuleCounts({ label, counts }: { label: string; counts: { rule: string; count: number }[] }) {
  if (counts.length === 0) return null;
  return (
    <p className="small">
      {label}: {counts.map((c) => `${c.rule} ${c.count}`).join(", ")}
    </p>
  );
}

function Summary({ day, summary }: { day: DecisionDayOut; summary: DecisionSummaryOut | null }) {
  if (!summary) {
    return <Empty>No summary for this day yet (it is written as the day is recorded).</Empty>;
  }
  const approvals = Object.entries(summary.approvals)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${k}`)
    .join(", ");
  return (
    <div className="stack">
      {summary.text && <p style={{ whiteSpace: "pre-line" }}>{summary.text}</p>}
      <div className="grid" aria-label="Day counts">
        <Stat label="Scanned" value={String(summary.scanned)} sub={summary.universe_size === null ? undefined : `of ${summary.universe_size}`} />
        <Stat label="Ranked" value={String(summary.ranked)} sub={`${summary.rvol_passed} above RVOL`} />
        <Stat label="Passed" value={String(summary.passed)} />
        <Stat label="Proposals" value={String(summary.proposals)} sub={approvals || undefined} />
        <Stat label="Fills" value={String(summary.fills)} sub={summary.avg_fill_diff_per_share === null ? undefined : `${fmtPrice(summary.avg_fill_diff_per_share)}/sh vs plan`} />
        <Stat label="Trades" value={String(summary.trades)} sub={`${summary.wins} won, ${summary.losses} lost`} />
        <Stat label="P&L" value={fmtMoney(summary.pnl)} sub={summary.pnl_r === null ? undefined : fmtR(summary.pnl_r)} />
      </div>
      <RuleCounts label="Top rejects" counts={summary.rejects_by_rule.slice(0, 5)} />
      <RuleCounts label="Risk rejections" counts={summary.risk_rejections} />
      <RuleCounts label="Exits" counts={summary.exits_by_reason} />
      {summary.notes.length > 0 && (
        <ul className="small">
          {summary.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      <p className="small muted">
        {day.final ? "Final" : "In progress"}
        {day.recorded_at ? ` · recorded ${fmtDateTime(day.recorded_at)}` : ""}
      </p>
    </div>
  );
}

function DecisionTable({ rows }: { rows: DecisionRowOut[] }) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const toggle = (seq: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  return (
    <Table aria-label="Decisions">
      <thead>
        <tr>
          <th>Time</th>
          <th>Stage</th>
          <th>Ticker</th>
          <th>Outcome</th>
          <th>Rule</th>
          <th>Reason</th>
          <th>Details</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const isOpen = open.has(row.seq);
          return (
            <Fragment key={row.seq}>
              <tr>
                <td>{fmtTime(row.ts)}</td>
                <td>{STAGE_LABEL[row.stage] ?? row.stage}</td>
                <td>{row.ticker ?? ""}</td>
                <td>
                  <Badge tone={TONE[row.outcome] ?? "muted"}>{row.outcome}</Badge>
                </td>
                <td>{row.rule ?? ""}</td>
                <td className="small">{row.reason ?? ""}</td>
                <td>
                  <Button aria-expanded={isOpen} aria-label={`${isOpen ? "Hide" : "Show"} details of decision ${row.seq}`} onClick={() => toggle(row.seq)}>
                    {isOpen ? "Hide" : "Details"}
                  </Button>
                </td>
              </tr>
              {isOpen && (
                <tr>
                  <td colSpan={7}>
                    <RowDetail row={row} />
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </Table>
  );
}

interface Filters {
  stage: DecisionStage | "";
  outcome: DecisionOutcome | "";
  ticker: string;
}

/** A ticker the URL may carry (`&ticker=`, the dashboard's rejection links, plan S8). */
const URL_TICKER = /^[A-Z.]{1,10}$/;

/**
 * The initial filters from the URL's `stage`, `outcome` and `ticker` (the dashboard's rejection links,
 * `/reports?day=D&stage=scan&outcome=rejected&ticker=T`); each invalid value is ignored (DB-T11).
 */
export function filtersFromParams(params: URLSearchParams): Filters {
  const stage = params.get("stage") ?? "";
  const outcome = params.get("outcome") ?? "";
  const ticker = params.get("ticker") ?? "";
  return {
    stage: (DECISION_STAGES as readonly string[]).includes(stage) ? (stage as DecisionStage) : "",
    outcome: (DECISION_OUTCOMES as readonly string[]).includes(outcome) ? (outcome as DecisionOutcome) : "",
    ticker: URL_TICKER.test(ticker) ? ticker : "",
  };
}

/** `params` with the filters written in (empty ones removed); every other parameter kept. */
function withFilters(params: URLSearchParams, f: Filters): URLSearchParams {
  const next = new URLSearchParams(params);
  const set = (key: string, value: string) => (value ? next.set(key, value) : next.delete(key));
  set("stage", f.stage);
  set("outcome", f.outcome);
  set("ticker", f.ticker.trim());
  return next;
}

/** The filter parameters of a URL as one comparable string (raw, before validation). */
function filterKey(params: URLSearchParams): string {
  return JSON.stringify([params.get("stage") ?? "", params.get("outcome") ?? "", params.get("ticker") ?? ""]);
}

function FilterBar({ filters, onChange }: { filters: Filters; onChange: (f: Filters) => void }) {
  return (
    <div className="row" style={{ gap: 12, flexWrap: "wrap" }} role="group" aria-label="Filters">
      <label>
        Stage
        <select aria-label="Stage" value={filters.stage} onChange={(e) => onChange({ ...filters, stage: e.target.value as DecisionStage | "" })}>
          <option value="">All stages</option>
          {DECISION_STAGES.map((s) => (
            <option key={s} value={s}>
              {STAGE_LABEL[s]}
            </option>
          ))}
        </select>
      </label>
      <label>
        Outcome
        <select aria-label="Outcome" value={filters.outcome} onChange={(e) => onChange({ ...filters, outcome: e.target.value as DecisionOutcome | "" })}>
          <option value="">All outcomes</option>
          {DECISION_OUTCOMES.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </label>
      <label>
        Ticker
        <input
          type="search"
          aria-label="Ticker"
          value={filters.ticker}
          maxLength={20}
          autoCapitalize="characters"
          onChange={(e) => onChange({ ...filters, ticker: e.target.value.toUpperCase() })}
        />
      </label>
    </div>
  );
}

function DayBody({ day, runId }: { day: IsoDate; runId: number | null }) {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const [filters, setFilters] = useState<Filters>(() => filtersFromParams(params));
  const [offset, setOffset] = useState(0);
  // The URL's filter parameters as this view last wrote or read them. When the URL changes them by itself
  // (back/forward, a link), the filters are read again; the view's own writes are not read back, so a ticker
  // being typed (not yet a valid URL ticker) is never reset (DB-T11 fix round 1).
  const urlFilters = filterKey(params);
  const seenFilters = useRef(urlFilters);
  useEffect(() => {
    if (urlFilters === seenFilters.current) return;
    seenFilters.current = urlFilters;
    setFilters(filtersFromParams(params));
    setOffset(0);
  }, [urlFilters, params]);
  const ticker = filters.ticker.trim();
  const q: DecisionDayQuery = {
    date: day,
    ...(runId === null ? {} : { run_id: runId }),
    ...(filters.stage ? { stage: filters.stage } : {}),
    ...(filters.outcome ? { outcome: filters.outcome } : {}),
    ...(ticker ? { ticker } : {}),
    limit: DAY_PAGE_SIZE,
    offset,
  };
  // The previous answer stays on screen while a filter or page change loads, so the filter bar (and the
  // ticker box being typed in) stays mounted.
  const dayQuery = useQuery({ queryKey: qk.decisionDay(q), queryFn: () => api.decisionDay(q), placeholderData: keepPreviousData });
  const csvUrl = api.decisionsCsvUrl(runId === null ? { date: day } : { date: day, run_id: runId });
  const changeFilters = (f: Filters) => {
    setFilters(f);
    setOffset(0);
    const next = withFilters(params, f);
    seenFilters.current = filterKey(next);
    setParams(next, { replace: true });
  };

  if (dayQuery.isPending) return <Loading />;
  if (dayQuery.isError) return <ErrorBox error={dayQuery.error} onRetry={() => void dayQuery.refetch()} />;
  const data = dayQuery.data;
  if (data === null) return <Empty>No decisions recorded for {day}.</Empty>;
  const first = data.total === 0 ? 0 : offset + 1;
  const last = offset + data.rows.length;
  return (
    <>
      <Card
        title={
          <>
            Decisions {data.session_date}
            {data.run_mode !== "live" && (
              <>
                {" "}
                <Badge tone="info">
                  {data.run_mode} run {data.run_id}
                </Badge>
              </>
            )}
          </>
        }
        actions={
          <a className="link-touch" href={csvUrl} download>
            Download CSV
          </a>
        }
      >
        <Summary day={data} summary={data.summary} />
      </Card>
      <Card title="Decisions">
        <div className="stack">
          <FilterBar filters={filters} onChange={changeFilters} />
          {data.rows.length === 0 ? (
            <Empty>No decisions match these filters.</Empty>
          ) : (
            <>
              <p className="small muted">
                Showing {first}–{last} of {data.total} · times in MT
              </p>
              <DecisionTable rows={data.rows} />
            </>
          )}
          {data.total > DAY_PAGE_SIZE && (
            <div className="row" style={{ gap: 12 }}>
              <Button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - DAY_PAGE_SIZE))}>
                Previous page
              </Button>
              <Button disabled={last >= data.total} onClick={() => setOffset(offset + DAY_PAGE_SIZE)}>
                Next page
              </Button>
            </div>
          )}
        </div>
      </Card>
    </>
  );
}

/** The Day view. `day` null: the latest day with rows. */
export function DayDecisions({ day, runId }: { day: IsoDate | null; runId: number | null }) {
  const api = useApi();
  const [params, setParams] = useSearchParams();
  const daysQ: DecisionDaysQuery = runId === null ? { limit: RECENT_DAYS } : { limit: RECENT_DAYS, run_id: runId };
  const days = useQuery({ queryKey: qk.decisionDays(daysQ), queryFn: () => api.decisionDays(daysQ) });
  const recent = uniqueDays(days.data?.days ?? []);
  const selected = day ?? recent[0]?.session_date ?? null;

  function go(value: string) {
    if (!isIsoDate(value)) return;
    const next = new URLSearchParams(params);
    next.set("day", value);
    next.delete("week");
    setParams(next);
  }

  const known = new Set(recent.map((d) => d.session_date));
  return (
    <>
      <h2>Decisions by day</h2>
      <nav className="row" style={{ gap: 12, flexWrap: "wrap" }} aria-label="Report views">
        <Link className="link-touch" to={selected ? `/reports?week=${selected}` : "/reports"}>
          Week view
        </Link>
      </nav>
      <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
        <label>
          Day
          <input type="date" aria-label="Day" value={selected ?? ""} onChange={(e) => go(e.target.value)} />
        </label>
        {recent.length > 0 && (
          <label>
            Recent days
            <select aria-label="Recent days" value={selected && known.has(selected) ? selected : ""} onChange={(e) => go(e.target.value)}>
              {!(selected && known.has(selected)) && <option value="">Choose a day</option>}
              {recent.map((d) => (
                <option key={d.session_date} value={d.session_date}>
                  {d.session_date}
                  {d.final ? "" : " (in progress)"}
                  {d.proposals > 0 ? ` · ${d.proposals} proposal${d.proposals === 1 ? "" : "s"}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {selected !== null ? (
        <DayBody key={`${selected}-${runId ?? "live"}`} day={selected} runId={runId} />
      ) : days.isPending ? (
        <Loading />
      ) : days.isError ? (
        <ErrorBox error={days.error} onRetry={() => void days.refetch()} />
      ) : (
        <Empty>No decisions recorded yet. The decision log fills in as the app runs each session.</Empty>
      )}
    </>
  );
}
