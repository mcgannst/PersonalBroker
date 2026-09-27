// The Settings page (P4-T15; SPEC §12 Settings): approval mode, kill switches, strategies, runtime settings,
// Questrade token, Telegram test and account security, each section with an anchor (`/settings#killswitches`
// is linked from the Dashboard's tripped kill-switch lights).
import { useEffect } from "react";
import { useLocation } from "react-router-dom";

import { Card } from "../components/ui";
import { ApprovalMode } from "./settings/ApprovalMode";
import { KillSwitchPanel } from "./settings/KillSwitchPanel";
import { QuestradeToken } from "./settings/QuestradeToken";
import { Security } from "./settings/Security";
import { SettingsGroups } from "./settings/SettingsGroups";
import { StrategyForms } from "./settings/StrategyForms";
import { TelegramTest } from "./settings/TelegramTest";

export default function SettingsPage() {
  const { hash } = useLocation();

  useEffect(() => {
    const id = decodeURIComponent(hash.replace(/^#/, ""));
    if (!id) return;
    document.getElementById(id)?.scrollIntoView?.({ block: "start" });
  }, [hash]);

  return (
    <main className="page stack">
      <h1>Settings</h1>
      <Card id="approval" title="Approval mode">
        <ApprovalMode />
      </Card>
      <Card id="killswitches" title="Kill switches">
        <KillSwitchPanel />
      </Card>
      <Card id="strategies" title="Strategies">
        <StrategyForms />
      </Card>
      <Card id="settings" title="Runtime settings">
        <SettingsGroups />
      </Card>
      <Card id="questrade" title="Questrade token">
        <QuestradeToken />
      </Card>
      <Card id="telegram" title="Telegram">
        <TelegramTest />
      </Card>
      <Card id="security" title="Security">
        <Security />
      </Card>
    </main>
  );
}
