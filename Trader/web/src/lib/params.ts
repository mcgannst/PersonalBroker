// Query-string values from deep links (Telegram links: /dashboard?proposal=12, /trades?position=3). A value
// that fails these checks never reaches the API.

/** A positive whole-number id (1 to 15 digits, no sign, exponent or decimal point), else null. */
export function parseId(value: string | null | undefined): number | null {
  if (typeof value !== "string" || !/^\d{1,15}$/.test(value)) return null;
  const n = Number(value);
  return n > 0 ? n : null;
}
