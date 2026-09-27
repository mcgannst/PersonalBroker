// One light per kill switch: green off, red tripped, amber for a manual pause, with what clears it; a
// tripped automatic switch links to the reset panel in Settings (P4-T13).
import { Link } from "react-router-dom";

import type { KillSwitchOut } from "../../api/types";
import { Light, type Tone } from "../../components/ui";

function tone(k: KillSwitchOut): Tone {
  if (!k.tripped) return "ok";
  return k.switch === "manual_pause" ? "warn" : "bad";
}

export default function KillSwitchLights({ switches }: { switches: KillSwitchOut[] }) {
  return (
    <section className="card" aria-label="Kill switches">
      <h2 className="card-title">Kill switches</h2>
      <ul className="plain-list">
        {switches.map((k) => (
          <li key={k.switch} className="stack killswitch">
            <Light tone={tone(k)} label={`${k.label}: ${k.tripped ? "tripped" : "off"}`} />
            {k.tripped && k.clears && <p className="small muted">{k.clears}</p>}
            {k.tripped && k.automatic && (
              <Link className="link-touch" to="/settings#killswitches">
                Reset in Settings
              </Link>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
