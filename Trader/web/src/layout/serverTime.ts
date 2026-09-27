// The server's clock and display zone (P4-T12). After login the shell reads `/api/meta` and:
//   - compares the browser's offset for the display zone at the server's "now" with the server's offset;
//     equal -> format with the named zone, different (browser zone data older than tzdata 2026c, or the zone
//     unknown to the browser) -> format with the server's fixed offset. Both offsets are recorded, so the
//     System page can show a warning from `displayZoneInfo()`;
//   - keeps the server clock skew (`server_time - Date.now()`) for countdowns (`secondsUntil`).
import { DEFAULT_ZONE_LABEL, setDisplayZone, zoneOffsetMinutes } from "../lib/format";
import type { MetaOut } from "../api/types";

let skewMs = 0;

/** `Date.parse(server_time) - Date.now()` from the last `/api/meta` read (0 before the first). */
export function serverSkewMs(): number {
  return skewMs;
}

/** Applies the time-zone check and records the clock skew from a `/api/meta` answer. */
export function applyServerMeta(meta: MetaOut, now: number = Date.now()): void {
  const serverMs = Date.parse(meta.server_time);
  if (!Number.isNaN(serverMs)) skewMs = serverMs - now;

  const serverOffset = meta.tz_offset_minutes;
  let browserOffset: number | null;
  try {
    browserOffset = zoneOffsetMinutes(meta.tz_display, meta.server_time);
  } catch {
    browserOffset = null;
  }
  if (browserOffset === null) {
    // The browser does not know the zone at all: plain arithmetic with the server's offset.
    setDisplayZone({ fixedOffsetMinutes: serverOffset, label: DEFAULT_ZONE_LABEL });
    return;
  }
  const check = { browserOffsetMinutes: browserOffset, serverOffsetMinutes: serverOffset };
  if (browserOffset === serverOffset) {
    setDisplayZone({ zone: meta.tz_display, label: DEFAULT_ZONE_LABEL }, check);
  } else {
    setDisplayZone({ fixedOffsetMinutes: serverOffset, label: DEFAULT_ZONE_LABEL }, check);
  }
}
