// The Settings page (P4-T15; SPEC §12 Settings; live dashboard plan S12, DB-T11): strategies, runtime settings,
// Questrade token, Telegram test and account security, each section with an anchor. The engine controls
// (approval mode and kill switches) moved to the Control page (open question 6); a one-line card links there.
import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";

import { Card } from "../components/ui";
import { QuestradeToken } from "./settings/QuestradeToken";
import { Security } from "./settings/Security";
import { SettingsGroups } from "./settings/SettingsGroups";
import { StrategyForms } from "./settings/StrategyForms";
import { TelegramTest } from "./settings/TelegramTest";

export const ENGINE_MOVED_TITLE = "Engine controls moved";

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
      <Card id="engine" title={ENGINE_MOVED_TITLE}>
        <p className="small">
          Approval mode, pause and resume, and the kill switches are on the{" "}
          <Link className="link-touch" to="/control">
            Control page
          </Link>
        </p>
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
