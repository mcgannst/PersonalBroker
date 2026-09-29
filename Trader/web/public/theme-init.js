// Pre-paint theme (DB-T11 fix round 1): runs from <head> before the first paint, so a browser that remembers
// Light (or Auto) never flashes the dark default while the app loads. The same key and values as
// src/theme/tokens.ts (THEME_STORAGE_KEY) and layout/Layout.tsx (readThemeChoice); main.tsx applies the
// theme again before React renders. A file, not an inline script: the server's CSP is `script-src 'self'`.
(function () {
  var theme = "dark";
  try {
    var stored = window.localStorage.getItem("trader.theme");
    if (stored === "auto" || stored === "dark" || stored === "light") theme = stored;
  } catch (e) {
    // storage blocked: the dark default
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
