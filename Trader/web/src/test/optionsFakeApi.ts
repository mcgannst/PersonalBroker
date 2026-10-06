// A fake `OptionsApiClient` for web tests (OPTSIM-T15): returns the option fixtures (deep copies), records
// every call, and can be told to fail or to answer differently per method. No network. Same shape as
// `FakeApiClient`.
import { ApiError } from "../api/client";
import { OPTIONS_API_METHODS, type OptionsApiClient, type OptionsApiMethod } from "../api/optionsClient";
import type { OptOrderIn, OptPanelActionIn, OptPromptAnswerIn, OptRepriceIn, SettingOut, StrategyIn } from "../api/types";
import * as ofx from "./optionsFixtures";

type ArgsOf<M extends OptionsApiMethod> = Parameters<OptionsApiClient[M]>;
type ResultOf<M extends OptionsApiMethod> = Awaited<ReturnType<OptionsApiClient[M]>>;
export type FakeOptionsResponses = { [M in OptionsApiMethod]: ResultOf<M> };
type Responder<M extends OptionsApiMethod> = (...args: ArgsOf<M>) => ResultOf<M> | Promise<ResultOf<M>>;

// `OPTIONS_API_METHODS` lists every method of the interface (a missing name fails the type check here).
type Missing = Exclude<keyof OptionsApiClient, OptionsApiMethod>;
const ALL_LISTED: Missing extends never ? true : never = true;
void ALL_LISTED;
void OPTIONS_API_METHODS;

export type RecordedOptionsCall = [OptionsApiMethod, unknown[]];

function clone<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

/** The server's answer when there is no active options run. */
export function noOptionsRun(): ApiError {
  return new ApiError(409, "no_options_run", "There is no active options run.");
}

/** The default answer of every method (the fixtures). */
export function defaultOptionsResponses(): FakeOptionsResponses {
  return {
    optAccount: ofx.optAccount,
    optPositions: ofx.optStructures,
    optChain: ofx.optChain,
    optChainQuotes: ofx.optChainQuotes,
    optPreview: ofx.optPreview,
    optSubmit: ofx.workingManualOrder,
    optOrders: ofx.optWorkingOrders,
    optCancel: { ...ofx.workingManualOrder, status: "cancelled", closed_at: ofx.OPT_NOW },
    optReprice: ofx.workingManualOrder,
    optActivity: ofx.optActivity,
    optPrompts: ofx.optPrompts,
    optAnswerPrompt: { ...ofx.yesNoPrompt, status: "answered", answer: "y", answered_at: ofx.OPT_NOW, answered_via: "web" },
    optStrategies: ofx.optStrategies,
    optPutStrategy: ofx.toyStrategy,
    optPanel: ofx.toyPanel,
    optPanelAction: ofx.panelActionOk,
    optSettings: ofx.optSettings,
    optPutSetting: ofx.optSettingsItems[0] as SettingOut,
  };
}

export class FakeOptionsApiClient implements OptionsApiClient {
  /** Every call in order, as `[method, args]`. */
  readonly calls: RecordedOptionsCall[] = [];
  private readonly responses: FakeOptionsResponses;
  private readonly responders = new Map<OptionsApiMethod, (...args: never[]) => unknown>();
  private readonly failures = new Map<OptionsApiMethod, unknown>();

  constructor(overrides: Partial<FakeOptionsResponses> = {}) {
    this.responses = { ...defaultOptionsResponses(), ...overrides };
    // Lookups by a key or a filter answer from the fixtures unless a test overrides them.
    if (!("optOrders" in overrides)) {
      this.respond("optOrders", (q) => (q.status === "history" ? ofx.optHistoryOrders : ofx.optWorkingOrders));
    }
    if (!("optPositions" in overrides)) {
      this.respond("optPositions", (q) => (q.state === "closed" ? { items: [ofx.closedStructure] } : ofx.optStructures));
    }
    if (!("optPanel" in overrides)) {
      this.respond("optPanel", (key) => (key === ofx.richPanel.strategy_key ? ofx.richPanel : { ...ofx.toyPanel, strategy_key: key }));
    }
  }

  /** Sets the value `method` resolves with from now on. */
  set<M extends OptionsApiMethod>(method: M, value: ResultOf<M>): this {
    this.responders.delete(method);
    (this.responses as Record<OptionsApiMethod, unknown>)[method] = value;
    return this;
  }

  /** Answers `method` with a function of its arguments (it may throw, or return a promise). */
  respond<M extends OptionsApiMethod>(method: M, fn: Responder<M>): this {
    this.responders.set(method, fn as unknown as (...args: never[]) => unknown);
    return this;
  }

  /** Makes every later call of `method` reject with `error` (until `succeed(method)`). */
  fail(method: OptionsApiMethod, error: unknown): this {
    this.failures.set(method, error);
    return this;
  }

  succeed(method: OptionsApiMethod): this {
    this.failures.delete(method);
    return this;
  }

  /** The argument lists of every call to `method`, in order. */
  callsTo(method: OptionsApiMethod): unknown[][] {
    return this.calls.filter(([m]) => m === method).map(([, args]) => args);
  }

  private async call<M extends OptionsApiMethod>(method: M, args: ArgsOf<M>): Promise<ResultOf<M>> {
    this.calls.push([method, clone(args)]);
    if (this.failures.has(method)) throw this.failures.get(method);
    const responder = this.responders.get(method) as Responder<M> | undefined;
    const value = responder ? await responder(...args) : this.responses[method];
    return clone(value) as ResultOf<M>;
  }

  optAccount() {
    return this.call("optAccount", []);
  }
  optPositions(q: Parameters<OptionsApiClient["optPositions"]>[0]) {
    return this.call("optPositions", [q]);
  }
  optChain(q: Parameters<OptionsApiClient["optChain"]>[0]) {
    return this.call("optChain", [q]);
  }
  optChainQuotes(q: Parameters<OptionsApiClient["optChainQuotes"]>[0]) {
    return this.call("optChainQuotes", [q]);
  }
  optPreview(body: OptOrderIn) {
    return this.call("optPreview", [body]);
  }
  optSubmit(body: OptOrderIn) {
    return this.call("optSubmit", [body]);
  }
  optOrders(q: Parameters<OptionsApiClient["optOrders"]>[0]) {
    return this.call("optOrders", [q]);
  }
  optCancel(id: number) {
    return this.call("optCancel", [id]);
  }
  optReprice(id: number, body: OptRepriceIn) {
    return this.call("optReprice", [id, body]);
  }
  optActivity(q: Parameters<OptionsApiClient["optActivity"]>[0]) {
    return this.call("optActivity", [q]);
  }
  optPrompts(q: Parameters<OptionsApiClient["optPrompts"]>[0]) {
    return this.call("optPrompts", [q]);
  }
  optAnswerPrompt(id: number, body: OptPromptAnswerIn) {
    return this.call("optAnswerPrompt", [id, body]);
  }
  optStrategies() {
    return this.call("optStrategies", []);
  }
  optPutStrategy(key: string, body: StrategyIn) {
    return this.call("optPutStrategy", [key, body]);
  }
  optPanel(key: string) {
    return this.call("optPanel", [key]);
  }
  optPanelAction(key: string, body: OptPanelActionIn) {
    return this.call("optPanelAction", [key, body]);
  }
  optSettings() {
    return this.call("optSettings", []);
  }
  optPutSetting(key: string, value: unknown) {
    return this.call("optPutSetting", [key, value]);
  }
}
