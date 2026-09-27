// Sizing and small helpers shared by the Recharts charts of the Trades and Performance pages.
import { cloneElement, type ReactElement } from "react";
import { ResponsiveContainer } from "recharts";

import { fmtDateTime, fmtTime } from "../../lib/format";

/** The width used when the browser cannot measure (no `ResizeObserver`, as in jsdom). Fits a 390 px phone. */
export const FALLBACK_CHART_WIDTH = 340;

/**
 * Renders a Recharts chart at the container's width (`ResponsiveContainer`), or at a fixed `width` when one is
 * given or the browser has no `ResizeObserver`.
 */
export function ChartFrame({ height, width, children }: { height: number; width?: number; children: ReactElement }) {
  const fixed = width ?? (typeof ResizeObserver === "undefined" ? FALLBACK_CHART_WIDTH : undefined);
  if (fixed !== undefined) return cloneElement(children, { width: fixed, height });
  return (
    <ResponsiveContainer width="100%" height={height}>
      {children}
    </ResponsiveContainer>
  );
}

/**
 * A money or price string as a number, for plotting only (P4 Global Constraints: the web parses money for charts,
 * never for arithmetic). Returns null for null, empty or non-finite values.
 */
export function plotNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Epoch milliseconds of an ISO time, or null. */
export function plotTime(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms;
}

/** An axis tick for an epoch-ms value: the display-zone clock time without its label (`07:35`). */
export function tickClock(ms: number): string {
  return fmtTime(new Date(ms).toISOString()).replace(/ \S+$/, "");
}

/** An axis tick for an epoch-ms value: the display-zone month and day (`10-05`). */
export function tickDay(ms: number): string {
  // fmtDateTime is "YYYY-MM-DD HH:MM MT", so the day follows the display zone.
  return fmtDateTime(new Date(ms).toISOString()).slice(5, 10);
}
