// Theme token names and helpers (live dashboard plan S11, DB-T1; final). The values live in tokens.css.
// Charts read the CSS variables through `readToken` so both themes work.

/** Short name -> CSS custom property, for every token tokens.css defines in each theme. */
export const TOKENS = {
  bg: "--bg",
  surface: "--surface",
  surface2: "--surface-2",
  text: "--text",
  textMuted: "--text-muted",
  border: "--border",
  accent: "--accent",
  accentText: "--accent-text",
  moneyUp: "--money-up",
  moneyDown: "--money-down",
  moneyFlat: "--money-flat",
  statusOk: "--status-ok",
  statusWarn: "--status-warn",
  statusBad: "--status-bad",
  statusMuted: "--status-muted",
  chartGrid: "--chart-grid",
  chartLine: "--chart-line",
  chartEntry: "--chart-entry",
  chartStop: "--chart-stop",
  chartTarget: "--chart-target",
  chartFill: "--chart-fill",
  fontNum: "--font-num",
  panelRadius: "--panel-radius",
  panelBorder: "--panel-border",
  touch: "--touch",
  // the names the existing pages use (defined in both themes too)
  ok: "--ok",
  okBg: "--ok-bg",
  warn: "--warn",
  warnBg: "--warn-bg",
  bad: "--bad",
  badBg: "--bad-bg",
  info: "--info",
  infoBg: "--info-bg",
  muted: "--muted",
  mutedBg: "--muted-bg",
  chartRange: "--chart-range",
  chartStopFill: "--chart-stop-fill",
  chartExit: "--chart-exit",
  chartBar: "--chart-bar",
  space1: "--space-1",
  space2: "--space-2",
  space3: "--space-3",
  space4: "--space-4",
  space5: "--space-5",
  space6: "--space-6",
  radius: "--radius",
  font: "--font",
  mono: "--mono",
} as const;

export type TokenName = keyof typeof TOKENS;

export type MoneyTone = "up" | "down" | "flat";

/** The sign of a money value (a decimal string or a number): null, unparsable and zero are `flat`. Use it as
 * the class next to `money` (`className={\`money ${moneyTone(v)}\`}`). */
export function moneyTone(value: string | number | null | undefined): MoneyTone {
  if (value === null || value === undefined || value === "") return "flat";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n) || n === 0) return "flat";
  return n > 0 ? "up" : "down";
}

/** The computed value of a token (a short name from `TOKENS` or a `--custom-property`) on `<html>`, trimmed;
 * "" where there is no stylesheet (tests) or no document. */
export function readToken(name: string): string {
  if (typeof document === "undefined" || typeof getComputedStyle !== "function") return "";
  const property = name.startsWith("--") ? name : (TOKENS as Record<string, string>)[name];
  if (!property) return "";
  return getComputedStyle(document.documentElement).getPropertyValue(property).trim();
}

export type ThemeChoice = "auto" | "dark" | "light";
export const THEME_STORAGE_KEY = "trader.theme";
