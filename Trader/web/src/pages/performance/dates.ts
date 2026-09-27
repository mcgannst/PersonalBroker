// Calendar-date helpers for the Journal and Reports pages. Session dates are ET calendar dates (`YYYY-MM-DD`);
// the arithmetic here is on plain dates (UTC midnight), so no time-zone data is involved.
import type { IsoDate } from "../../api/types";

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY_MS = 86_400_000;

function toMs(date: IsoDate): number | null {
  const m = ISO_DATE.exec(date);
  if (!m) return null;
  const y = Number(m[1]);
  const mo = Number(m[2]);
  const d = Number(m[3]);
  const ms = Date.UTC(y, mo - 1, d);
  const back = new Date(ms);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d) return null;
  return ms;
}

function fromMs(ms: number): IsoDate {
  return new Date(ms).toISOString().slice(0, 10);
}

/** True for a real calendar date written `YYYY-MM-DD`. */
export function isIsoDate(value: string | null | undefined): value is IsoDate {
  return typeof value === "string" && toMs(value) !== null;
}

/** `date` plus `days` (may be negative). `date` must be valid. */
export function addDays(date: IsoDate, days: number): IsoDate {
  const ms = toMs(date);
  if (ms === null) throw new RangeError(`not a date: ${date}`);
  return fromMs(ms + days * DAY_MS);
}

export interface TradingWeek {
  monday: IsoDate;
  friday: IsoDate;
  /** Monday to Friday, in order. */
  days: IsoDate[];
}

/** The Monday-to-Friday week containing `date` (a Saturday or Sunday belongs to the week just ended). */
export function tradingWeek(date: IsoDate): TradingWeek {
  const ms = toMs(date);
  if (ms === null) throw new RangeError(`not a date: ${date}`);
  const dow = new Date(ms).getUTCDay(); // 0 Sunday .. 6 Saturday
  const back = (dow + 6) % 7; // days since Monday
  const monday = fromMs(ms - back * DAY_MS);
  const days = [0, 1, 2, 3, 4].map((i) => addDays(monday, i));
  return { monday, friday: days[4] as IsoDate, days };
}

/** Today's date in New York (the session calendar), falling back to the UTC date without zone data. */
export function todayEt(now: Date = new Date()): IsoDate {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
    if (isIsoDate(parts)) return parts;
  } catch {
    // no zone data: fall through
  }
  return now.toISOString().slice(0, 10);
}
