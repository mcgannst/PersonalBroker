// Shared pieces of the Options page (OPTSIM-T15): how a net price is worded, the reject reasons in plain
// words, the ticket's draft shape and the page's error box.
import { useQueryClient } from "@tanstack/react-query";

import { isApiError } from "../../api/client";
import { oqk } from "../../api/optionsClient";
import type { Money, OptEffect, OptInstrument, OptOrderIntent, OptRejectReason, OptRight, OptStructureKind, OptStructureOut, Side } from "../../api/types";
import { Empty, ErrorBox } from "../../components/ui";
import { fmtPrice } from "../../lib/format";
import { isDecimalString } from "../settings/validate";

// ---------------------------------------------------------------- net prices

export type NetDirection = "credit" | "debit";

function isZero(text: string): boolean {
  return /^[0.]*$/.test(text);
}

/**
 * A net price in words. The API's net prices are per share with credit positive; they are never shown signed:
 * `"0.45"` is "credit 0.45", `"-1.20"` is "debit 1.20".
 */
export function netWords(net: Money | null | undefined): string {
  if (net === null || net === undefined || !isDecimalString(net)) return "n/a";
  const t = net.trim();
  const abs = t.replace(/^[+-]/, "");
  if (isZero(abs)) return "even";
  return `${t.startsWith("-") ? "debit" : "credit"} ${fmtPrice(abs)}`;
}

/** The side a net price is on (a zero or missing price counts as a credit). */
export function netDirection(net: Money | null | undefined): NetDirection {
  return typeof net === "string" && net.trim().startsWith("-") && !isZero(net.trim().slice(1)) ? "debit" : "credit";
}

/** A net price without its sign, for a text box (`"-0.3000"` → `"0.30"`). */
export function netAmount(net: Money | null | undefined): string {
  if (net === null || net === undefined || !isDecimalString(net)) return "";
  return fmtPrice(net.trim().replace(/^[+-]/, ""));
}

/** The signed net price for the API from what was typed (an unsigned amount) and the chosen side; null when invalid. */
export function signedNet(amount: string, direction: NetDirection): Money | null {
  const t = amount.trim();
  if (!isDecimalString(t) || t.startsWith("-") || t.startsWith("+")) return null;
  if (!/^\d+(\.\d{1,4})?$/.test(t)) return null;
  return direction === "debit" && !isZero(t) ? `-${t}` : t;
}

/** The amount box and the credit/debit choice of a limit price. */
export function NetInput({
  id,
  amount,
  direction,
  onAmount,
  onDirection,
  disabled = false,
}: {
  id: string;
  amount: string;
  direction: NetDirection;
  onAmount: (value: string) => void;
  onDirection: (value: NetDirection) => void;
  disabled?: boolean;
}) {
  return (
    <div className="row">
      <label htmlFor={`${id}-amount`}>
        Limit (per share)
        <input id={`${id}-amount`} className="opt-narrow" type="text" inputMode="decimal" autoComplete="off" value={amount} disabled={disabled} onChange={(e) => onAmount(e.target.value)} />
      </label>
      <label htmlFor={`${id}-side`}>
        Credit or debit
        <select id={`${id}-side`} value={direction} disabled={disabled} onChange={(e) => onDirection(e.target.value === "debit" ? "debit" : "credit")}>
          <option value="credit">Credit (you receive)</option>
          <option value="debit">Debit (you pay)</option>
        </select>
      </label>
    </div>
  );
}

// ---------------------------------------------------------------- words

/** Why the collateral engine refused an order, in plain words. */
export const REJECT_WORDS: Record<OptRejectReason, string> = {
  naked_short: "This would leave a short option with nothing covering it. Naked shorts are never allowed.",
  insufficient_cash: "There is not enough free cash for this order.",
  position_cap: "This would put more than the allowed share of the account into one underlying.",
  shares_committed: "The shares this needs already cover another position.",
  not_covered_after_close: "Closing this would leave another short option uncovered.",
  nothing_to_close: "There is no open position that this order would close.",
  unknown_contract: "One of the contracts is not known.",
  expired_contract: "One of the contracts has already expired.",
  invalid_order: "The order is not valid as entered.",
  no_quote: "There is no usable quote for one of the legs right now.",
  structure_frozen: "This position is frozen and cannot be traded.",
  strategies_paused: "Strategies are paused.",
};

export const KIND_WORDS: Record<OptStructureKind, string> = {
  long_call: "Long call",
  long_put: "Long put",
  csp: "Cash-secured put",
  covered_call: "Covered call",
  debit_spread: "Debit spread",
  credit_spread: "Credit spread",
  iron_condor: "Iron condor",
  calendar: "Calendar",
  diagonal: "Diagonal",
  shares: "Shares",
  custom: "Custom",
};

/** `F 2026-11-20 P 12.00`: the same shape as the server's contract label. */
export function contractLabel(underlying: string, expiry: string, right: OptRight, strike: Money): string {
  return `${underlying} ${expiry} ${right === "call" ? "C" : "P"} ${fmtPrice(strike)}`;
}

// ---------------------------------------------------------------- the ticket's draft

export interface TicketLeg {
  instrument: OptInstrument;
  /** Null for a shares leg. */
  contract_id: number | null;
  label: string;
  side: Side;
  effect: OptEffect;
  ratio: number;
}

/** What the ticket holds between tabs: one underlying, its legs and, when closing, the structure. */
export interface TicketDraft {
  underlying: string;
  structure_id: number | null;
  legs: TicketLeg[];
  qty: number;
}

export const EMPTY_TICKET: TicketDraft = { underlying: "", structure_id: null, legs: [], qty: 1 };

/** Open when every leg opens, close when every leg closes, otherwise a roll. */
export function ticketIntent(legs: readonly TicketLeg[]): OptOrderIntent {
  if (legs.every((l) => l.effect === "open")) return "open";
  if (legs.every((l) => l.effect === "close")) return "close";
  return "roll";
}

/** The ticket that closes a structure: every open leg reversed, with the structure's quantity. */
export function closingTicket(structure: OptStructureOut): TicketDraft {
  const qty = Math.max(1, structure.qty);
  const legs: TicketLeg[] = structure.positions
    .filter((p) => p.qty !== 0)
    .map((p) => ({
      instrument: p.instrument,
      contract_id: p.contract?.id ?? null,
      label: p.contract?.label ?? `${structure.underlying} shares`,
      side: p.qty > 0 ? "sell" : "buy",
      effect: "close",
      ratio: Math.max(1, Math.round(Math.abs(p.qty) / qty)),
    }));
  return { underlying: structure.underlying, structure_id: structure.id, legs, qty };
}

// ---------------------------------------------------------------- queries

/** Refreshes everything on the Options page (after an order, an answer or an action). */
export function useRefreshOptions(): () => void {
  const queryClient = useQueryClient();
  return () => void queryClient.invalidateQueries({ queryKey: oqk.all() });
}

export function isNoOptionsRun(error: unknown): boolean {
  return isApiError(error) && error.code === "no_options_run";
}

/** The page's error box: "no options run yet" is a state to explain, not a failure to retry. */
export function OptErrorBox({ error, onRetry, noRun = false }: { error: unknown; onRetry?: () => void; noRun?: boolean }) {
  if (noRun || isNoOptionsRun(error)) {
    return (
      <Empty>
        No options run yet. Start one on the server with <code>trader options-run new --cash 5000 --confirm</code>
      </Empty>
    );
  }
  return <ErrorBox error={error} onRetry={onRetry} />;
}
