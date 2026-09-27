// Display formatting. Every time, money amount, price, percentage, R multiple and duration on screen goes
// through this module (P4 Global Constraints: web times in MT through format.ts only).
//
// Times arrive as UTC ISO strings and are shown in the display zone (America/Edmonton, labelled MT).
// A browser's time-zone data may predate tzdata 2026c (Alberta on UTC-6 all year), so the shell (T12)
// compares the browser's offset for "now" with the server's (`/api/meta`) and, when they differ, switches
// this module to a fixed offset (`setDisplayZone({ fixedOffsetMinutes, label })`). Fixed mode uses plain
// arithmetic and needs no zone data at all.
//
// Money values are decimal strings; they are rounded here with integer (BigInt) arithmetic, never floats.

export type DisplayZoneMode = { zone: string; label?: string } | { fixedOffsetMinutes: number; label: string };

export interface DisplayZoneCheck {
  browserOffsetMinutes: number;
  serverOffsetMinutes: number;
}

export interface DisplayZoneInfo {
  mode: "zone" | "fixed";
  browserOffsetMinutes: number | null;
  serverOffsetMinutes: number | null;
}

export const DEFAULT_DISPLAY_ZONE = "America/Edmonton";
export const DEFAULT_ZONE_LABEL = "MT";
const NA = "n/a";

interface ZoneState {
  mode: DisplayZoneMode;
  check: DisplayZoneCheck | null;
}

let state: ZoneState = { mode: { zone: DEFAULT_DISPLAY_ZONE, label: DEFAULT_ZONE_LABEL }, check: null };
const formatters = new Map<string, Intl.DateTimeFormat>();

/** Sets how times are shown. `check` carries the two offsets the shell compared (shown on the System page). */
export function setDisplayZone(mode: DisplayZoneMode, check?: DisplayZoneCheck): void {
  state = { mode: { ...mode }, check: check ? { ...check } : null };
}

/** What the System page shows about the time-zone check. */
export function displayZoneInfo(): DisplayZoneInfo {
  return {
    mode: "zone" in state.mode ? "zone" : "fixed",
    browserOffsetMinutes: state.check?.browserOffsetMinutes ?? null,
    serverOffsetMinutes: state.check?.serverOffsetMinutes ?? null,
  };
}

function zoneFormatter(zone: string): Intl.DateTimeFormat {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
    formatters.set(zone, f);
  }
  return f;
}

interface WallClock {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

function wallClockInZone(ms: number, zone: string): WallClock {
  const parts: Record<string, number> = {};
  for (const p of zoneFormatter(zone).formatToParts(new Date(ms))) {
    if (p.type !== "literal") parts[p.type] = Number(p.value);
  }
  return {
    year: parts.year ?? 0,
    month: parts.month ?? 0,
    day: parts.day ?? 0,
    // Some engines print midnight as 24 even with h23.
    hour: (parts.hour ?? 0) % 24,
    minute: parts.minute ?? 0,
    second: parts.second ?? 0,
  };
}

function wallClockAtOffset(ms: number, offsetMinutes: number): WallClock {
  const d = new Date(ms + offsetMinutes * 60_000);
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth() + 1,
    day: d.getUTCDate(),
    hour: d.getUTCHours(),
    minute: d.getUTCMinutes(),
    second: d.getUTCSeconds(),
  };
}

/** The UTC offset in minutes (east positive, so Edmonton is -360) of `zone` at `iso`, from `Intl`. */
export function zoneOffsetMinutes(zone: string, iso: string): number {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) throw new RangeError(`not a time: ${iso}`);
  const w = wallClockInZone(ms, zone);
  const asUtc = Date.UTC(w.year, w.month - 1, w.day, w.hour, w.minute, w.second);
  const whole = Math.floor(ms / 1000) * 1000;
  return Math.round((asUtc - whole) / 60_000);
}

function wallClock(iso: string | null | undefined): { w: WallClock; label: string } | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const mode = state.mode;
  if ("zone" in mode) return { w: wallClockInZone(ms, mode.zone), label: mode.label ?? DEFAULT_ZONE_LABEL };
  return { w: wallClockAtOffset(ms, mode.fixedOffsetMinutes), label: mode.label };
}

const pad2 = (n: number): string => String(n).padStart(2, "0");
const ymd = (w: WallClock): string => `${String(w.year).padStart(4, "0")}-${pad2(w.month)}-${pad2(w.day)}`;

/** `07:35 MT`. */
export function fmtTime(iso: string | null | undefined): string {
  const r = wallClock(iso);
  return r ? `${pad2(r.w.hour)}:${pad2(r.w.minute)} ${r.label}` : NA;
}

/** `2026-10-06 07:35 MT`. */
export function fmtDateTime(iso: string | null | undefined): string {
  const r = wallClock(iso);
  return r ? `${ymd(r.w)} ${pad2(r.w.hour)}:${pad2(r.w.minute)} ${r.label}` : NA;
}

/** A session date (`YYYY-MM-DD`, already an ET calendar date: no zone conversion). */
export function fmtDate(isoDate: string | null | undefined): string {
  if (!isoDate || !/^\d{4}-\d{2}-\d{2}$/.test(isoDate)) return NA;
  return isoDate;
}

// ---------------------------------------------------------------- decimals

interface Dec {
  neg: boolean;
  digits: bigint; // magnitude, as an integer
  scale: number; // value = digits / 10^scale (scale may be negative)
}

const DEC_RE = /^\s*([+-])?(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?\s*$/;

function parseDec(value: string | number | null | undefined): Dec | null {
  if (value === null || value === undefined) return null;
  const s = typeof value === "number" ? (Number.isFinite(value) ? String(value) : "") : value;
  const m = DEC_RE.exec(s);
  if (!m) return null;
  const intPart = m[2] ?? "";
  const frac = m[3] ?? "";
  if (intPart === "" && frac === "") return null;
  const exp = m[4] ? Number(m[4]) : 0;
  return { neg: m[1] === "-", digits: BigInt((intPart + frac).replace(/^0+(?=\d)/, "") || "0"), scale: frac.length - exp };
}

/** Rounds half away from zero to `dp` places; returns the magnitude as an integer scaled by 10^dp. */
function roundTo(d: Dec, dp: number): bigint {
  if (d.scale <= dp) return d.digits * 10n ** BigInt(dp - d.scale);
  const div = 10n ** BigInt(d.scale - dp);
  const q = d.digits / div;
  return (d.digits % div) * 2n >= div ? q + 1n : q;
}

function shift(d: Dec, places: number): Dec {
  return { ...d, scale: d.scale - places };
}

/** Splits a scaled integer into "int" and "frac" strings. */
function split(scaled: bigint, dp: number): { int: string; frac: string } {
  const s = scaled.toString().padStart(dp + 1, "0");
  return { int: s.slice(0, s.length - dp), frac: s.slice(s.length - dp) };
}

function group(int: string): string {
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function signed(d: Dec, dp: number, plus: boolean): { sign: string; int: string; frac: string } {
  const scaled = roundTo(d, dp);
  const parts = split(scaled, dp);
  const sign = scaled === 0n ? "" : d.neg ? "-" : plus ? "+" : "";
  return { sign, ...parts };
}

/** `$1,234.56`, `-$12.30`, `n/a` for null. */
export function fmtMoney(s: string | number | null | undefined): string {
  const d = parseDec(s);
  if (!d) return NA;
  const r = signed(d, 2, false);
  return `${r.sign}$${group(r.int)}.${r.frac}`;
}

/** A price: 4 decimal places, trailing zeros trimmed down to at least 2 (`21.56`, `21.5608`). */
export function fmtPrice(s: string | number | null | undefined): string {
  const d = parseDec(s);
  if (!d) return NA;
  const r = signed(d, 4, false);
  const frac = r.frac.replace(/0{1,2}$/, "");
  return `${r.sign}${r.int}.${frac}`;
}

/** A fraction as a signed percentage: `0.0123` → `+1.23%`. */
export function fmtPct(fraction: string | number | null | undefined): string {
  const d = parseDec(fraction);
  if (!d) return NA;
  const r = signed(shift(d, 2), 2, true);
  return `${r.sign}${r.int}.${r.frac}%`;
}

/**
 * A fraction as a percentage without the plus sign, for rates and drawdowns (`0.5` → `50.00%`,
 * `0.0138` → `1.38%`); a negative value keeps its minus.
 */
export function fmtRate(fraction: string | number | null | undefined): string {
  const d = parseDec(fraction);
  if (!d) return NA;
  const r = signed(shift(d, 2), 2, false);
  return `${r.sign}${r.int}.${r.frac}%`;
}

/** An R multiple: `2.1700` → `+2.17R`. */
export function fmtR(s: string | number | null | undefined): string {
  const d = parseDec(s);
  if (!d) return NA;
  const r = signed(d, 2, true);
  return `${r.sign}${r.int}.${r.frac}R`;
}

/** `3m 20s`, `45s`, `1h 5m`, `3d 2h`; negative counts as 0. */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return NA;
  const total = Math.max(0, Math.floor(seconds));
  const d = Math.floor(total / 86_400);
  const h = Math.floor((total % 86_400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/**
 * Whole seconds from the server's "now" until `iso` (never below 0). `serverSkewMs` is
 * `Date.parse(server_time) - Date.now()` measured when the server's time was read.
 */
export function secondsUntil(iso: string, serverSkewMs: number): number {
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return 0;
  const serverNow = Date.now() + serverSkewMs;
  return Math.max(0, Math.floor((target - serverNow) / 1000));
}
