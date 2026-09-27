// Stub (P4-T2): T12 replaces it with the real fetch-based client (cookies, CSRF, errors).
import type { ApiClient } from "./client";

export interface HttpClientOptions {
  /** Prefix for every request; "" (same origin) by default. */
  baseUrl?: string;
  /** Called on any 401 (the shell clears auth and goes to /login?next=...). */
  onUnauthorized?: () => void;
}

export function createHttpClient(_opts: HttpClientOptions = {}): ApiClient {
  throw new Error("createHttpClient is not implemented yet (P4-T12)");
}
