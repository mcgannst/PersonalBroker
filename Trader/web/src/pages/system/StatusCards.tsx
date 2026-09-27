// The System page's status cards: Questrade token, worker, Telegram, version, time-zone check; plus the
// rate-limit and failed-send panels (P4 plan, task P4-T16).
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { NotificationOut, SystemOut, TokenOut, WorkerOut } from "../../api/types";
import { Card, Empty, Light, Table } from "../../components/ui";
import { displayZoneInfo, fmtDateTime, fmtDuration } from "../../lib/format";
import { TelegramTest } from "./TelegramTest";

export const WORKER_DOWN_TEXT = "worker not running: approvals and fills will not happen";
export const TELEGRAM_OFF_TEXT = "Telegram not configured: nothing is relayed to your phone; approve on the Dashboard";

/** `-360` → `UTC−6`, `330` → `UTC+5:30`, `0` → `UTC`. */
export function utcOffsetLabel(minutes: number): string {
  if (minutes === 0) return "UTC";
  const sign = minutes < 0 ? "−" : "+";
  const abs = Math.abs(minutes);
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `UTC${sign}${h}${m ? `:${String(m).padStart(2, "0")}` : ""}`;
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="facts small">
      {rows.map(([term, value]) => (
        <div className="row" key={term}>
          <dt className="muted">{term}</dt>
          <dd style={{ margin: 0 }}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function TokenCard({ token }: { token: TokenOut }) {
  const label = token.ok ? "Token OK" : token.seeded ? "Token error" : "Token not seeded";
  return (
    <Card title="Questrade token">
      <Light tone={token.ok ? "ok" : "bad"} label={label} />
      {!token.ok && token.error && <p className="small tone-bad">{token.error}</p>}
      <Facts
        rows={[
          ["Last refresh", fmtDateTime(token.last_refresh_at)],
          ["Age", token.age_hours === null ? "n/a" : fmtDuration(token.age_hours * 3600)],
          ["Access expires", fmtDateTime(token.expires_at)],
        ]}
      />
      {!token.ok && (
        <p className="small">
          <Link to="/settings#questrade">Paste a new token</Link>
        </p>
      )}
    </Card>
  );
}

function WorkerCard({ worker }: { worker: WorkerOut }) {
  return (
    <Card title="Worker">
      <Light tone={worker.ok ? "ok" : "bad"} label={worker.ok ? "Worker OK" : "Worker not running"} />
      {!worker.ok && <p className="small tone-bad">{WORKER_DOWN_TEXT}</p>}
      <Facts
        rows={[
          ["Phase", worker.phase ?? "n/a"],
          ["Heartbeat", worker.age_seconds === null ? "n/a" : `${fmtDuration(worker.age_seconds)} ago`],
          ["Last beat", fmtDateTime(worker.beat_at)],
          ["Session", worker.session_date ?? "n/a"],
          ["Process", worker.pid === null ? "n/a" : `${worker.pid} on ${worker.host ?? "unknown host"}`],
        ]}
      />
    </Card>
  );
}

function TelegramCard({ configured }: { configured: boolean }) {
  return (
    <Card title="Telegram">
      <Light tone={configured ? "ok" : "bad"} label={configured ? "Telegram configured" : "Telegram not configured"} />
      {!configured && <p className="small tone-bad">{TELEGRAM_OFF_TEXT}</p>}
      <TelegramTest />
    </Card>
  );
}

function TimeZoneCard({ serverOffsetMinutes }: { serverOffsetMinutes: number | null }) {
  const info = displayZoneInfo();
  const offset = info.serverOffsetMinutes ?? serverOffsetMinutes;
  const offsetText = offset === null ? "" : ` (${utcOffsetLabel(offset)})`;
  return (
    <Card title="Time zone">
      {info.mode === "zone" ? (
        <>
          <Light tone="ok" label="Time zone OK" />
          <p className="small">Your browser&apos;s time-zone data agrees with the server&apos;s{offsetText}.</p>
        </>
      ) : (
        <>
          <Light tone="warn" label="Time zone: server offset" />
          <p className="small tone-warn">
            {`Your browser's time-zone data differs from the server's; times use the server's offset${offsetText}`}
          </p>
        </>
      )}
      {info.browserOffsetMinutes !== null && info.serverOffsetMinutes !== null && (
        <Facts
          rows={[
            ["Browser", utcOffsetLabel(info.browserOffsetMinutes)],
            ["Server", utcOffsetLabel(info.serverOffsetMinutes)],
          ]}
        />
      )}
    </Card>
  );
}

/** The status cards. `serverOffsetMinutes` is `meta.tz_offset_minutes` (null while meta loads). */
export function StatusCards({ system, serverOffsetMinutes }: { system: SystemOut; serverOffsetMinutes: number | null }) {
  return (
    <div className="grid">
      <TokenCard token={system.token} />
      <WorkerCard worker={system.worker} />
      <TelegramCard configured={system.telegram_configured} />
      <Card title="Version">
        <Facts
          rows={[
            ["Version", system.version],
            ["Environment", system.app_env],
            ["Alembic revision", system.alembic_revision ?? "n/a"],
            ["tz database", system.tz_iana_version ?? "n/a"],
          ]}
        />
      </Card>
      <TimeZoneCard serverOffsetMinutes={serverOffsetMinutes} />
    </div>
  );
}

/** Questrade rate-limit numbers (remaining requests per category) from the worker's heartbeat. */
export function RateLimits({ rateLimit }: { rateLimit: Record<string, number> | null }) {
  const entries = rateLimit ? Object.entries(rateLimit) : [];
  return (
    <Card title="Rate limits">
      {entries.length === 0 ? (
        <Empty>Questrade rate limits: not reported yet.</Empty>
      ) : (
        <Table aria-label="Rate limits">
          <thead>
            <tr>
              <th>Category</th>
              <th className="num">Remaining</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([category, remaining]) => (
              <tr key={category}>
                <td>{category}</td>
                <td className="num">{remaining}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

/** Telegram messages that failed (or may not have been) delivered. Never shows message text. */
export function FailedSends({ items }: { items: NotificationOut[] }) {
  return (
    <Card title="Failed Telegram sends">
      {items.length === 0 ? (
        <Empty>No failed sends.</Empty>
      ) : (
        <Table aria-label="Failed Telegram sends">
          <thead>
            <tr>
              <th>Time</th>
              <th>Kind</th>
              <th>Status</th>
              <th className="num">Attempts</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {items.map((n) => (
              <tr key={n.id}>
                <td>{fmtDateTime(n.created_at)}</td>
                <td>{n.kind}</td>
                <td>{n.status}</td>
                <td className="num">{n.attempts}</td>
                <td>{n.error ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
