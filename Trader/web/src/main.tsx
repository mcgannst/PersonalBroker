// Entry point (P4-T12): renders the app (providers, router, auth, live updates) into #root. The theme tokens
// load before the app's styles (they define every variable styles.css uses, plan S11), and the stored theme
// choice is applied to <html> before the first paint (dark by default, open question 1).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { applyTheme, readThemeChoice } from "./layout/Layout";
import "./theme/tokens.css";
import "./styles.css";

applyTheme(readThemeChoice());

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
