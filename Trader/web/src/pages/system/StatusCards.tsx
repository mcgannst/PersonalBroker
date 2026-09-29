// What the Control page's Health card reuses from the old System page (P4-T16): the worker-down and Telegram-off
// texts, the UTC offset label and the failed-send panel. The System page's own status cards and rate-limit panel
// went with that page (DB-T11 fix round 1: Control's Health card draws its own).
import type { NotificationOut } from "../../api/types";
import { Card, Empty, Table } from "../../components/ui";
import { fmtDateTime } from "../../lib/format";

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
