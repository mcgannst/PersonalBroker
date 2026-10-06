// Fixtures for the Options page tests (OPTSIM-T15): one answer per /api/options route, shaped as the server
// sends them (money and ratios as decimal strings, net prices per share with credit positive).
import type {
  FieldOut,
  Items,
  OptAccountOut,
  OptActivityOut,
  OptChainOut,
  OptChainQuotesOut,
  OptContractOut,
  OptOrderOut,
  OptPanelActionResultOut,
  OptPanelOut,
  OptPreviewOut,
  OptPromptOut,
  OptQuoteOut,
  OptRejectReason,
  OptStrategyOut,
  OptStructureOut,
  SettingOut,
  SettingsOut,
} from "../api/types";

/** The "now" of the fixtures (07:58 MT on the fixtures' session day). */
export const OPT_NOW = "2026-10-06T13:58:00Z";
export const OPT_EXPIRY = "2026-11-20";

function field(partial: Partial<FieldOut> & Pick<FieldOut, "name" | "kind" | "title">): FieldOut {
  return {
    description: null,
    default: null,
    minimum: null,
    maximum: null,
    exclusive_minimum: false,
    exclusive_maximum: false,
    enum: null,
    item_enum: null,
    pattern: null,
    nullable: false,
    ...partial,
  };
}

// ---------------------------------------------------------------- account

export const optAccount: OptAccountOut = {
  run_id: 7,
  started_at: "2026-10-05T13:30:00Z",
  starting_cash: "5000.0000",
  cash: "5123.4500",
  reserved: "1200.0000",
  free_cash: "3923.4500",
  positions_value: "-41.0000",
  account_value: "5082.4500",
  premium_collected: "164.0000",
  realized_pnl: "41.2500",
  unrealized_pnl: "41.2000",
  fees_total: "3.9600",
  max_position_pct: "0.50",
  marks_as_of: "2026-10-06T13:57:30Z",
  marks_complete: true,
  worker_beat_at: "2026-10-06T13:57:50Z",
  by_source: [
    { source: "manual", open_structures: 2, reserved: "0.0000", realized_pnl: "12.0000", unrealized_pnl: "9.0000", premium_collected: "0.0000" },
    { source: "toy_call", open_structures: 1, reserved: "1200.0000", realized_pnl: "29.2500", unrealized_pnl: "32.2000", premium_collected: "164.0000" },
  ],
  benchmark: { ticker: "SOFI", since: "2026-10-05", benchmark_return: "0.0123", account_return: "0.0165" },
};

// ---------------------------------------------------------------- contracts, positions

export function optContract(id: number, underlying: string, right: "call" | "put", strike: string, extra: Partial<OptContractOut> = {}): OptContractOut {
  return {
    id,
    underlying,
    expiry: OPT_EXPIRY,
    strike,
    right,
    multiplier: 100,
    is_monthly: true,
    dte: 45,
    label: `${underlying} ${OPT_EXPIRY} ${right === "call" ? "C" : "P"} ${Number(strike).toFixed(2)}`,
    ...extra,
  };
}

export const putF12 = optContract(501, "F", "put", "12.0000");
export const callF13 = optContract(502, "F", "call", "13.0000");
export const callF14 = optContract(503, "F", "call", "14.0000");

/** A cash-secured put a strategy sold (one short leg). */
export const cspStructure: OptStructureOut = {
  id: 21,
  source: "toy_call",
  kind: "csp",
  underlying: "F",
  state: "open",
  close_reason: null,
  frozen: false,
  qty: 1,
  entry_net: "0.4500",
  reserved_cash: "1200.0000",
  take_profit_net: "-0.2250",
  realized_pnl: "0.0000",
  unrealized_pnl: "12.0000",
  fees_total: "0.9900",
  opened_at: "2026-10-05T14:05:00Z",
  closed_at: null,
  dte: 45,
  positions: [{ id: 301, instrument: "option", contract: putF12, qty: -1, avg_price: "0.4500", mark: "0.3300", unrealized_pnl: "12.0000", delta: "0.2100" }],
};

/** A two-contract call debit spread bought by hand (a long and a short leg). */
export const spreadStructure: OptStructureOut = {
  id: 22,
  source: "manual",
  kind: "debit_spread",
  underlying: "F",
  state: "open",
  close_reason: null,
  frozen: false,
  qty: 2,
  entry_net: "-0.3000",
  reserved_cash: "0.0000",
  take_profit_net: null,
  realized_pnl: "0.0000",
  unrealized_pnl: "-8.0000",
  fees_total: "3.9600",
  opened_at: "2026-10-06T13:40:00Z",
  closed_at: null,
  dte: 45,
  positions: [
    { id: 302, instrument: "option", contract: callF13, qty: 2, avg_price: "0.5500", mark: "0.5000", unrealized_pnl: "-10.0000", delta: "0.4200" },
    { id: 303, instrument: "option", contract: callF14, qty: -2, avg_price: "0.2500", mark: "0.2400", unrealized_pnl: "2.0000", delta: "-0.2300" },
  ],
};

/** 100 shares held by hand (no contract, no expiry). */
export const sharesStructure: OptStructureOut = {
  id: 23,
  source: "manual",
  kind: "shares",
  underlying: "SOFI",
  state: "open",
  close_reason: null,
  frozen: false,
  qty: 1,
  entry_net: "-9.5000",
  reserved_cash: "0.0000",
  take_profit_net: null,
  realized_pnl: "0.0000",
  unrealized_pnl: "17.0000",
  fees_total: "0.0000",
  opened_at: "2026-10-05T15:00:00Z",
  closed_at: null,
  dte: null,
  positions: [{ id: 304, instrument: "shares", contract: null, qty: 100, avg_price: "9.5000", mark: "9.6700", unrealized_pnl: "17.0000", delta: "1.0000" }],
};

export const optStructures: Items<OptStructureOut> = { items: [cspStructure, spreadStructure, sharesStructure] };

export const closedStructure: OptStructureOut = {
  ...cspStructure,
  id: 19,
  state: "closed",
  close_reason: "expired",
  reserved_cash: "0.0000",
  realized_pnl: "44.0100",
  unrealized_pnl: null,
  closed_at: "2026-10-02T20:00:00Z",
  dte: null,
  positions: [{ ...cspStructure.positions[0]!, id: 290, qty: 0, mark: null, unrealized_pnl: null, delta: null }],
};

// ---------------------------------------------------------------- chain

export const optChain: OptChainOut = {
  underlying: "F",
  underlying_price: "12.8400",
  price_time: "2026-10-06T13:57:55Z",
  market_open: true,
  expiries: [
    { expiry: "2026-10-30", dte: 24, is_monthly: false, strikes: 12 },
    { expiry: OPT_EXPIRY, dte: 45, is_monthly: true, strikes: 14 },
  ],
};

export function optQuote(contractId: number, bid: string | null, ask: string | null, extra: Partial<OptQuoteOut> = {}): OptQuoteOut {
  return {
    contract_id: contractId,
    bid,
    ask,
    last: bid,
    bid_size: 10,
    ask_size: 12,
    volume: 150,
    open_interest: 2400,
    iv: "0.3400",
    delta: "0.2100",
    gamma: "0.0900",
    theta: "-0.0100",
    vega: "0.0200",
    fetched_at: "2026-10-06T13:57:58Z",
    stale: false,
    ...extra,
  };
}

/** Three strikes: 12 (both sides fresh), 13 (the call is stale), 14 (no put listed). */
export const optChainQuotes: OptChainQuotesOut = {
  underlying: "F",
  expiry: OPT_EXPIRY,
  underlying_price: "12.8400",
  fetched_at: "2026-10-06T13:57:58Z",
  market_open: true,
  rows: [
    { strike: "12.0000", call_contract_id: 511, put_contract_id: 501, call: optQuote(511, "1.0500", "1.1000", { delta: "0.7200" }), put: optQuote(501, "0.4500", "0.4800", { delta: "-0.2100" }) },
    {
      strike: "13.0000",
      call_contract_id: 502,
      put_contract_id: 512,
      call: optQuote(502, "0.5200", "0.5500", { delta: "0.4200", stale: true, fetched_at: "2026-10-06T13:50:00Z" }),
      put: optQuote(512, "0.8800", "0.9300", { delta: "-0.5600" }),
    },
    { strike: "14.0000", call_contract_id: 503, put_contract_id: null, call: optQuote(503, "0.2400", "0.2600", { delta: "0.2300" }), put: null },
  ],
};

// ---------------------------------------------------------------- preview and orders

export const optPreview: OptPreviewOut = {
  accepted: true,
  reject_reason: null,
  detail: "Cash-secured put: 1,200.00 held against the strike.",
  kind: "csp",
  net_at_market: "0.4500",
  max_loss: "1155.9900",
  max_profit: "44.0100",
  breakevens: ["11.5500"],
  fees: "0.9900",
  reserve_cash: "1200.0000",
  cash_after: "5167.4600",
  free_cash_after: "2767.4600",
  exposure_after: "2400.0000",
  cap_limit: "2541.2300",
};

export function optPreviewRejected(reason: OptRejectReason, detail = "The engine's own explanation."): OptPreviewOut {
  return { ...optPreview, accepted: false, reject_reason: reason, detail, max_loss: null, max_profit: null, breakevens: [] };
}

/** A working limit order entered by hand: it can be cancelled and repriced. */
export const workingManualOrder: OptOrderOut = {
  id: 31,
  source: "manual",
  intent: "open",
  structure_id: null,
  underlying: "F",
  legs: [{ leg_no: 1, instrument: "option", contract: putF12, side: "sell", effect: "open", ratio: 1, fill_price: null, fill_quote: null }],
  qty: 1,
  order_type: "limit",
  net_limit: "0.4700",
  tif: "day",
  status: "working",
  walk: false,
  reject_reason: null,
  reject_detail: null,
  reason: "manual order",
  reserved_cash: "1200.0000",
  submitted_at: "2026-10-06T13:45:00Z",
  closed_at: null,
  fill_net: null,
  fees: null,
};

/** A working order a strategy placed: read-only on the web. */
export const workingStrategyOrder: OptOrderOut = {
  ...workingManualOrder,
  id: 32,
  source: "toy_call",
  intent: "close",
  structure_id: 21,
  legs: [{ leg_no: 1, instrument: "option", contract: putF12, side: "buy", effect: "close", ratio: 1, fill_price: null, fill_quote: null }],
  net_limit: "-0.2250",
  tif: "gtc",
  walk: true,
  reason: "take_profit",
  reserved_cash: "0.0000",
};

export const filledOrder: OptOrderOut = {
  id: 28,
  source: "manual",
  intent: "open",
  structure_id: 22,
  underlying: "F",
  legs: [
    { leg_no: 1, instrument: "option", contract: callF13, side: "buy", effect: "open", ratio: 1, fill_price: "0.5500", fill_quote: { bid: "0.5200", ask: "0.5500", last: "0.5300", fetched_at: "2026-10-06T13:40:01Z" } },
    { leg_no: 2, instrument: "option", contract: callF14, side: "sell", effect: "open", ratio: 1, fill_price: "0.2500", fill_quote: { bid: "0.2500", ask: "0.2700", last: "0.2600", fetched_at: "2026-10-06T13:40:01Z" } },
  ],
  qty: 2,
  order_type: "limit",
  net_limit: "-0.3000",
  tif: "day",
  status: "filled",
  walk: false,
  reject_reason: null,
  reject_detail: null,
  reason: "manual order",
  reserved_cash: "0.0000",
  submitted_at: "2026-10-06T13:39:30Z",
  closed_at: "2026-10-06T13:40:01Z",
  fill_net: "-0.3000",
  fees: "3.9600",
};

export const rejectedOrder: OptOrderOut = {
  ...workingManualOrder,
  id: 27,
  status: "rejected",
  reject_reason: "insufficient_cash",
  reject_detail: "Needs 1,200.00 free cash; 800.00 is free.",
  reserved_cash: "0.0000",
  closed_at: "2026-10-06T13:30:00Z",
};

export const optWorkingOrders: Items<OptOrderOut> = { items: [workingManualOrder, workingStrategyOrder] };
export const optHistoryOrders: Items<OptOrderOut> = { items: [filledOrder, rejectedOrder] };

// ---------------------------------------------------------------- activity

export const optActivity: Items<OptActivityOut> = {
  items: [
    { id: "fill:91", ts: "2026-10-06T13:40:01Z", kind: "fill", source: "manual", underlying: "F", title: "Bought 2 F call spreads", detail: "debit 0.30", level: "info", structure_id: 22 },
    { id: "prompt:12", ts: "2026-10-06T13:35:00Z", kind: "prompt", source: "toy_call", underlying: "F", title: "Roll or take assignment?", detail: "Asked on Telegram", level: "info", structure_id: 21 },
    { id: "event:880", ts: "2026-10-06T13:31:00Z", kind: "alert", source: "toy_call", underlying: "F", title: "Strike touched", detail: "F traded at 12.00", level: "warning", structure_id: 21 },
    { id: "life:14", ts: "2026-10-02T20:00:00Z", kind: "lifecycle", source: "toy_call", underlying: "F", title: "Put expired worthless", detail: "kept 44.01", level: "info", structure_id: 19 },
    { id: "event:860", ts: "2026-10-02T14:35:00Z", kind: "decision", source: "toy_call", underlying: null, title: "No new position", detail: "strategies paused", level: "info", structure_id: null },
  ],
};

// ---------------------------------------------------------------- prompts

/** A plain yes/no prompt. */
export const yesNoPrompt: OptPromptOut = {
  id: 12,
  source: "toy_call",
  kind: "fresh_cash",
  scope_key: "F",
  title: "Would you buy F today with fresh cash?",
  body: "100 shares of F were assigned at 12.00. Net cost 11.55.",
  choices: [
    { code: "y", label: "Yes" },
    { code: "n", label: "No" },
  ],
  needs_text: false,
  status: "pending",
  asked_at: "2026-10-06T13:35:00Z",
  answered_at: null,
  answer: null,
  answer_text: null,
  answered_via: null,
  data: {},
};

/** A prompt whose approval needs a written reason. */
export const textPrompt: OptPromptOut = {
  ...yesNoPrompt,
  id: 13,
  kind: "ownership",
  scope_key: "SOFI",
  title: "Approve SOFI?",
  body: "Why would you own it?",
  choices: [
    { code: "a", label: "Approve" },
    { code: "r", label: "Reject" },
  ],
  needs_text: true,
};

export const optPrompts: Items<OptPromptOut> = { items: [yesNoPrompt, textPrompt] };

// ---------------------------------------------------------------- strategies and panels

export const toyStrategy: OptStrategyOut = {
  key: "toy_call",
  version: "0.1.0",
  enabled: true,
  revision: 2,
  params: { min_dte: 30, max_cost: "150", fee_guess: "0.99" },
  schema: { title: "ToyParams", type: "object", properties: {} },
  fields: [
    field({ name: "min_dte", kind: "integer", title: "Min DTE", default: 30, minimum: "1", maximum: "365" }),
    field({ name: "max_cost", kind: "decimal", title: "Max Cost", default: "150", minimum: "0", exclusive_minimum: true }),
    field({ name: "fee_guess", kind: "decimal", title: "Fee Guess", description: "ASSUMPTION: 0.99 per contract until the broker's schedule is confirmed.", default: "0.99", minimum: "0" }),
  ],
  updated_at: "2026-10-05T13:30:00Z",
  updated_by: "web:stephen",
  open_structures: 1,
  manual_events: ["toy_buy"],
};

export const secondStrategy: OptStrategyOut = {
  key: "second_plugin",
  version: "1.0.0",
  enabled: false,
  revision: 1,
  params: { ticker: "SOFI" },
  schema: { title: "SecondParams", type: "object", properties: {} },
  fields: [field({ name: "ticker", kind: "string", title: "Ticker", default: "SOFI", pattern: "^[A-Z][A-Z0-9.\\-]{0,9}$" })],
  updated_at: "2026-10-05T13:30:00Z",
  updated_by: null,
  open_structures: 0,
  manual_events: [],
};

export const optStrategies: Items<OptStrategyOut> = { items: [toyStrategy, secondStrategy] };

/** The toy plug-in's panel: one table and one text action offered on its row. */
export const toyPanel: OptPanelOut = {
  strategy_key: "toy_call",
  summary: [{ label: "Open calls", value: "1", tone: null }],
  tables: [
    {
      key: "calls",
      title: "Calls bought",
      columns: [
        { key: "contract", label: "Contract", kind: "text" },
        { key: "qty", label: "Quantity", kind: "number" },
        { key: "cost", label: "Cost", kind: "money" },
      ],
      rows: [{ id: "41", cells: { contract: "F 2026-11-20 C 13.00", qty: 1, cost: "55.0000" }, actions: ["note"], detail: [] }],
      empty_text: "No calls yet",
    },
  ],
  actions: [{ key: "note", label: "Leave a note", kind: "text", confirm: false, choices: [] }],
};

/** A richer panel: toned summary, two tables, every column kind, row details, and every action kind. */
export const richPanel: OptPanelOut = {
  strategy_key: "second_plugin",
  summary: [
    { label: "Positions", value: "2", tone: "ok" },
    { label: "Paused", value: "yes", tone: "warn" },
  ],
  tables: [
    {
      key: "tickers",
      title: "Tickers",
      columns: [
        { key: "ticker", label: "Ticker", kind: "text" },
        { key: "status", label: "Status", kind: "badge" },
        { key: "flagged", label: "Flagged", kind: "bool" },
        { key: "next_date", label: "Next date", kind: "date" },
        { key: "net_cost", label: "Net cost", kind: "money" },
        { key: "rolls", label: "Rolls", kind: "number" },
      ],
      rows: [
        {
          id: "SOFI",
          cells: { ticker: "SOFI", status: "approved", flagged: false, next_date: "2026-11-03", net_cost: "9.1200", rolls: 1 },
          actions: ["flagged", "kind", "drop"],
          detail: [
            { label: "Test 1", value: "pass", tone: "ok" },
            { label: "Test 2", value: "fail: earnings inside the window", tone: "bad" },
          ],
        },
        { id: "F", cells: { ticker: "F", status: "candidate", flagged: true, next_date: null, net_cost: null, rolls: 0 }, actions: ["flagged"], detail: [] },
      ],
      empty_text: "No tickers",
    },
    { key: "candidates", title: "Candidates", columns: [{ key: "ticker", label: "Ticker", kind: "text" }], rows: [], empty_text: "Nothing passed the screen" },
  ],
  actions: [
    { key: "add", label: "Add a ticker", kind: "text", confirm: false, choices: [] },
    { key: "rescreen", label: "Screen again", kind: "button", confirm: false, choices: [] },
    { key: "flagged", label: "Flagged", kind: "toggle", confirm: false, choices: [] },
    { key: "kind", label: "Security type", kind: "choice", confirm: false, choices: ["stock", "etf"] },
    { key: "drop", label: "Drop", kind: "button", confirm: true, choices: [] },
  ],
};

export const panelActionOk: OptPanelActionResultOut = { ok: true, message: "Saved" };

// ---------------------------------------------------------------- settings

function setting(key: string, value: unknown, fieldPart: Partial<FieldOut> & Pick<FieldOut, "kind" | "title">, extra: Partial<SettingOut> = {}): SettingOut {
  const f = field({ name: key, ...fieldPart });
  return { key, value, default: f.default, is_default: JSON.stringify(value) === JSON.stringify(f.default), group: "Options", field: f, updated_at: null, updated_by: null, ...extra };
}

export const optSettingsItems: SettingOut[] = [
  setting("options.max_position_pct", "0.50", { kind: "decimal", title: "Max Position Pct", description: "Cap per underlying, of account value", default: "0.50", minimum: "0", exclusive_minimum: true, maximum: "1" }),
  setting("options.assignment_fee", "0", { kind: "decimal", title: "Assignment Fee", description: "ASSUMPTION: no fee per assignment; verify with the broker.", default: "0", minimum: "0", maximum: "100" }),
  setting("options.allow_market_orders", true, { kind: "boolean", title: "Allow Market Orders", default: true }),
  setting("options.watchlist", ["F"], { kind: "string_list", title: "Watchlist", default: [] }, { updated_at: "2026-10-05T13:30:00Z", updated_by: "web:stephen" }),
];

export const optSettings: SettingsOut = { items: optSettingsItems };
