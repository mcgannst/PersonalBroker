// The Options page (OPTSIM-T15): the options simulation's account, positions, manual trading desk, orders,
// strategy plug-ins, activity and settings, one tab each, with the pending prompts above every tab.
// Deep links: `/options?tab=<tab>` opens a tab, `/options?prompt=<id>` (the Telegram link for a written answer)
// marks that prompt. Data refreshes by polling (5 s for money and orders, 30 s for the rest; paused while
// the page is hidden); there is no live-update topic for options.
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Button } from "../components/ui";
import { parseId } from "../lib/params";
import { AccountTab } from "./options/AccountTab";
import { ActivityTab } from "./options/ActivityTab";
import { OrdersTab } from "./options/OrdersTab";
import { PositionsTab } from "./options/PositionsTab";
import { PromptsBanner } from "./options/PromptsBanner";
import { SettingsTab } from "./options/SettingsTab";
import { EMPTY_TICKET, type TicketDraft } from "./options/shared";
import { StrategiesTab } from "./options/StrategiesTab";
import { TradeTab } from "./options/TradeTab";
import "./options/options.css";

export const OPTION_TABS = [
  { id: "account", label: "Account" },
  { id: "positions", label: "Positions" },
  { id: "trade", label: "Trade" },
  { id: "orders", label: "Orders" },
  { id: "strategies", label: "Strategies" },
  { id: "activity", label: "Activity" },
  { id: "settings", label: "Settings" },
] as const;

export type OptionTab = (typeof OPTION_TABS)[number]["id"];

/** The tab a `?tab=` value names; anything else is the Account tab. */
export function tabFrom(raw: string | null): OptionTab {
  return OPTION_TABS.find((t) => t.id === raw)?.id ?? "account";
}

export default function OptionsPage() {
  const [params, setParams] = useSearchParams();
  const tab = tabFrom(params.get("tab"));
  const promptId = parseId(params.get("prompt"));
  // The ticket lives here so that it survives a change of tab, and so that Close on a position can fill it.
  const [ticket, setTicket] = useState<TicketDraft>(EMPTY_TICKET);

  const go = (next: OptionTab) => {
    const p = new URLSearchParams(params);
    if (next === "account") p.delete("tab");
    else p.set("tab", next);
    setParams(p);
  };

  return (
    <main className="page options-page">
      <h1>Options</h1>
      <PromptsBanner focusId={promptId} />
      <div className="opt-tabs" role="tablist" aria-label="Options sections">
        {OPTION_TABS.map((t) => (
          <Button
            key={t.id}
            role="tab"
            id={`opt-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls="opt-tabpanel"
            className={tab === t.id ? "opt-tab is-active" : "opt-tab"}
            onClick={() => go(t.id)}
          >
            {t.label}
          </Button>
        ))}
      </div>
      <div id="opt-tabpanel" role="tabpanel" aria-labelledby={`opt-tab-${tab}`}>
        {tab === "account" && <AccountTab />}
        {tab === "positions" && (
          <PositionsTab
            onClose={(draft) => {
              setTicket(draft);
              go("trade");
            }}
          />
        )}
        {tab === "trade" && <TradeTab draft={ticket} onDraft={setTicket} />}
        {tab === "orders" && <OrdersTab />}
        {tab === "strategies" && <StrategiesTab />}
        {tab === "activity" && <ActivityTab />}
        {tab === "settings" && <SettingsTab />}
      </div>
    </main>
  );
}
