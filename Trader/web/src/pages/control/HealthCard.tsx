// The Control page's Health card (live dashboard design §4.5, plan DB-T9): worker heartbeat age with the
// stale badge, Questrade token, database, Telegram with the reused TelegramTest, Questrade counts today and
// the rate limits, the 9:35 opening-bar fetch, the mark publisher, the time-zone check and failed Telegram
// sends (carried over from the System page). Everything but the time-zone check comes from `/api/control`;
// the check reads `/api/meta` as the System page did. No Questrade call.
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useApi } from "../../api/client";
import { qk } from "../../api/queryKeys";
import type { HealthPanelOut, OpeningBarsOut, QuestradeCountsOut } from "../../api/types";
import { Table } from "../../components/ui";
import { zoneCheck } from "../../layout/serverTime";
import { fmtDateTime, fmtDuration, fmtTime } from "../../lib/format";
import { Panel } from "../live/Panel";
import { TelegramTest } from "../settings/TelegramTest";
import { FailedSends, TELEGRAM_OFF_TEXT, WORKER_DOWN_TEXT, utcOffsetLabel } from "../system/StatusCards";
import { Facts, StatusChip, cardError } from "./parts";

/** The heartbeat badge shows past this age (plan S16), independent of the worker's own stale threshold. */
export const HEARTBEAT_BADGE_SECONDS = 60;
export const NOT_FETCHED_TEXT = "not fetched by the worker today";

/** `31.2`, `45`: seconds with at most one decimal. */
function secs(n: number): string {
  const r = Math.round(n * 10) / 10;
  return Number.isInteger(r) ? String(r) : r.toFixed(1);
}

/** "543 of 543 bars in 31.2 s". */
export function openingBarsText(ob: OpeningBarsOut): string {
  return `${ob.completed} of ${ob.symbols} bars in ${secs(ob.elapsed_s)} s`;
}

/** What went wrong in an incomplete fetch: "2 errors, 11 outstanding, 3 x 429 (4.5 s paused), stopped by X". */
function openingBarsProblems(ob: OpeningBarsOut): string {
  const parts: string[] = [];
  if (ob.errors > 0) parts.push(`${ob.errors} error${ob.errors === 1 ? "" : "s"}`);
  if (ob.outstanding > 0) parts.push(`${ob.outstanding} outstanding`);
  if (ob.http_429 > 0) parts.push(`${ob.http_429} x 429 (${secs(ob.pause_s)} s paused)`);
  if (ob.raised) parts.push(`stopped by ${ob.raised}`);
  return parts.join(", ");
}

function Sub({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="ctl-health-part">
      <h3 className="ctl-sub">{title}</h3>
      {children}
    </div>
  );
}

function QuestradeRow({ name, c }: { name: string; c: QuestradeCountsOut }) {
  return (
    <tr>
      <td>{name}</td>
      <td className="num">{c.requests}</td>
      <td className="num">{c.http_429}</td>
      <td className="num">{`${secs(c.pause_s)} s`}</td>
      <td className="num">{c.http_5xx}</td>
      <td className="num">{c.transport_errors}</td>
    </tr>
  );
}

function TimeZone({ tzVersion }: { tzVersion: string | null }) {
  const api = useApi();
  const meta = useQuery({ queryKey: qk.meta(), queryFn: () => api.meta() });
  let chip: ReactNode;
  let text: string;
  if (!meta.data) {
    chip = <StatusChip tone="muted">Time zone: checking</StatusChip>;
    text = meta.isError ? "The server's time zone could not be read." : "Waiting for the server's time zone.";
  } else {
    const check = zoneCheck(meta.data);
    const server = utcOffsetLabel(check.serverOffsetMinutes);
    if (check.mode === "zone") {
      chip = <StatusChip tone="ok">Time zone OK</StatusChip>;
      text = `Browser and server agree (${server}).`;
    } else {
      chip = <StatusChip tone="warn">Time zone: server offset</StatusChip>;
      text = `The browser's time-zone data differs from the server's; times use the server's offset (${server}).`;
    }
  }
  return (
    <div className="ctl-stack">
      <div className="ctl-row">{chip}</div>
      <p className="ctl-small ctl-muted">{text}</p>
      <p className="ctl-small ctl-muted">{`tz database ${tzVersion ?? "n/a"}`}</p>
    </div>
  );
}

function HealthBody({ health }: { health: HealthPanelOut }) {
  const { worker, token, questrade, opening_bars: ob, marks } = health;
  const stale = health.worker_stale || (worker.age_seconds !== null && worker.age_seconds > HEARTBEAT_BADGE_SECONDS);
  const tokenWord = token.ok ? "Token OK" : token.seeded ? "Token error" : "Token not seeded";
  const limits = questrade?.rate_limit ? Object.entries(questrade.rate_limit) : [];

  return (
    <div className="ctl-health">
      <Sub title="Worker">
        <div className="ctl-row">
          <StatusChip tone={worker.ok ? "ok" : "bad"}>{worker.ok ? "Worker OK" : "Worker not running"}</StatusChip>
          {stale && <StatusChip tone="warn">heartbeat stale</StatusChip>}
        </div>
        {!worker.ok && <p className="ctl-small ctl-bad">{WORKER_DOWN_TEXT}</p>}
        <Facts
          rows={[
            ["Heartbeat", worker.age_seconds === null ? "none yet" : `${fmtDuration(worker.age_seconds)} ago`],
            ["Phase", worker.phase ?? "n/a"],
            ["Process", worker.pid === null ? "n/a" : `${worker.pid} on ${worker.host ?? "unknown host"}`],
          ]}
        />
      </Sub>

      <Sub title="Questrade token">
        <div className="ctl-row">
          <StatusChip tone={token.ok ? "ok" : "bad"}>{tokenWord}</StatusChip>
        </div>
        {!token.ok && token.error && <p className="ctl-small ctl-bad ctl-text">{token.error}</p>}
        <Facts
          rows={[
            ["Last refresh", fmtDateTime(token.last_refresh_at)],
            ["Access expires", fmtDateTime(token.expires_at)],
          ]}
        />
        {!token.ok && (
          <p className="ctl-small">
            <Link className="link-touch" to="/settings#questrade">
              Paste a new token
            </Link>
          </p>
        )}
      </Sub>

      <Sub title="Database">
        <p className={`ctl-small ${health.db_ok ? "" : "ctl-bad"}`}>
          {health.db_ok ? `OK${health.db_latency_ms !== null ? `, ${secs(health.db_latency_ms)} ms` : ""}` : "not reachable"}
        </p>
      </Sub>

      <Sub title="Telegram">
        <div className="ctl-row">
          <StatusChip tone={health.telegram_configured ? "ok" : "bad"}>
            {health.telegram_configured ? "Telegram configured" : "Telegram not configured"}
          </StatusChip>
        </div>
        {!health.telegram_configured && <p className="ctl-small ctl-bad">{TELEGRAM_OFF_TEXT}</p>}
        <TelegramTest />
      </Sub>

      <Sub title="Questrade today">
        {questrade === null ? (
          <p className="ctl-small ctl-muted">Not reported by the worker today.</p>
        ) : (
          <>
            <Table aria-label="Questrade today">
              <thead>
                <tr>
                  <th>Category</th>
                  <th className="num">Requests</th>
                  <th className="num">429s</th>
                  <th className="num">Paused</th>
                  <th className="num">5xx</th>
                  <th className="num">Transport</th>
                </tr>
              </thead>
              <tbody>
                <QuestradeRow name="market" c={questrade.market} />
                <QuestradeRow name="account" c={questrade.account} />
              </tbody>
            </Table>
            <p className="ctl-small ctl-muted">{`since ${fmtDateTime(questrade.since)}`}</p>
            <p className="ctl-small ctl-muted">
              {limits.length === 0 ? "Rate limits: not reported yet." : limits.map(([k, v]) => `${k} ${v}`).join(" · ")}
            </p>
          </>
        )}
      </Sub>

      <Sub title="Opening-bar fetch">
        {ob === null ? (
          <p className="ctl-small ctl-muted">{NOT_FETCHED_TEXT}</p>
        ) : (
          <>
            <div className="ctl-row">
              <span className="ctl-small num">{openingBarsText(ob)}</span>
              <StatusChip tone={ob.complete ? "ok" : "bad"}>{ob.complete ? "complete" : "incomplete"}</StatusChip>
            </div>
            {!ob.complete && <p className="ctl-small ctl-bad ctl-text">{openingBarsProblems(ob)}</p>}
            <p className="ctl-small ctl-muted">{`started ${fmtTime(ob.started_at)}${ob.deadline_s !== null ? `, deadline ${secs(ob.deadline_s)} s` : ""}`}</p>
          </>
        )}
      </Sub>

      <Sub title="Marks">
        {marks === null ? (
          <p className="ctl-small ctl-muted">Marks: not reported by the worker.</p>
        ) : (
          <div className="ctl-row">
            <span className="ctl-small">
              {`${marks.written_at ? `last write ${fmtTime(marks.written_at)}` : "no write yet"}, ${marks.symbols} symbol${marks.symbols === 1 ? "" : "s"}`}
            </span>
            <StatusChip tone={marks.failing ? "bad" : "ok"}>{marks.failing ? "marks failing" : "marks OK"}</StatusChip>
          </div>
        )}
      </Sub>

      <Sub title="Time zone">
        <TimeZone tzVersion={health.tz_iana_version} />
      </Sub>

      <div className="ctl-health-part ctl-health-wide">
        <FailedSends items={health.notifications_failed} />
      </div>
    </div>
  );
}

export function HealthCard({ health, error, onRetry }: { health: HealthPanelOut | null; error?: string | null; onRetry?: () => void }) {
  return (
    <Panel title="Health" error={cardError(health, error)} onRetry={onRetry}>
      {health && <HealthBody health={health} />}
    </Panel>
  );
}
