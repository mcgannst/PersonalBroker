// The server's clock and display zone (P4-T12). After login the shell reads `/api/meta` and:
//   - compares the browser's offset for the display zone at the server's "now" with the server's offset;
//     equal -> format with the named zone, different (browser zone data older than tzdata 2026c, or the zone
//     unknown to the browser) -> format with the server's fixed offset (`zoneCheck`, a pure function of the
//     meta answer, so the System page derives its card from the same `meta` query instead of a global);
//   - keeps the server clock skew (`server_time - Date.now()`) for countdowns (`secondsUntil`). Every skew is
//     computed by `skewFrom`, whichever answer carried the server's time (`/api/meta`, `/api/dashboard`).
import { DEFAULT_ZONE_LABEL, setDisplayZone, zoneOffsetMinutes } from "../lib/format";
import type { MetaOut } from "../api/types";

let skewMs = 0;

/**
 * `Date.parse(serverTime) - receivedAt`: how far the server's clock is ahead of the browser's, from an answer
 * received at `receivedAt` (ms since the epoch). 0 when the time is missing or unreadable.
 */
export function skewFrom(serverTime: string | null | undefined, receivedAt: number): number {
  const serverMs = serverTime ? Date.parse(serverTime) : Number.NaN;
  return Number.isNaN(serverMs) ? 0 : serverMs - receivedAt;
}

/** The skew from the last `/api/meta` read (0 before the first). */
export function serverSkewMs(): number {
  return skewMs;
}

export interface ZoneCheck {
  /** `zone`: the browser's zone data agrees with the server; `fixed`: use the server's offset. */
  mode: "zone" | "fixed";
  /** The browser's offset for the display zone at the server's "now"; null when it does not know the zone. */
  browserOffsetMinutes: number | null;
  serverOffsetMinutes: number;
}

/** Compares the browser's zone data with the server's, from a `/api/meta` answer. */
export function zoneCheck(meta: MetaOut): ZoneCheck {
  let browserOffset: number | null;
  try {
    browserOffset = zoneOffsetMinutes(meta.tz_display, meta.server_time);
  } catch {
    browserOffset = null;
  }
  const serverOffset = meta.tz_offset_minutes;
  return {
    mode: browserOffset === serverOffset ? "zone" : "fixed",
    browserOffsetMinutes: browserOffset,
    serverOffsetMinutes: serverOffset,
  };
}

/** Applies the time-zone check and records the clock skew from a `/api/meta` answer. */
export function applyServerMeta(meta: MetaOut, now: number = Date.now()): void {
  // An unreadable time keeps the last good skew.
  if (!Number.isNaN(Date.parse(meta.server_time))) skewMs = skewFrom(meta.server_time, now);

  const check = zoneCheck(meta);
  if (check.browserOffsetMinutes === null) {
    // The browser does not know the zone at all: plain arithmetic with the server's offset.
    setDisplayZone({ fixedOffsetMinutes: check.serverOffsetMinutes, label: DEFAULT_ZONE_LABEL });
    return;
  }
  const offsets = { browserOffsetMinutes: check.browserOffsetMinutes, serverOffsetMinutes: check.serverOffsetMinutes };
  if (check.mode === "zone") {
    setDisplayZone({ zone: meta.tz_display, label: DEFAULT_ZONE_LABEL }, offsets);
  } else {
    setDisplayZone({ fixedOffsetMinutes: check.serverOffsetMinutes, label: DEFAULT_ZONE_LABEL }, offsets);
  }
}
