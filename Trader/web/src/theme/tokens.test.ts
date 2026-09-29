// @vitest-environment node
// DB-T1 acceptance test 8: tokens.css defines every TOKENS name in every theme block, green and red are
// money-only hues, and moneyTone behaves (readToken: tokens.dom.test.ts). Node environment, so the file is
// read from disk as it is (like build.test.ts).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { moneyTone, readToken, THEME_STORAGE_KEY, TOKENS } from "./tokens";

const CSS = readFileSync(fileURLToPath(new URL("./tokens.css", import.meta.url)), "utf8");

/** `name -> value` of the custom properties declared directly in a rule body. */
function declarations(body: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out.set(m[1]!, m[2]!.trim());
  return out;
}

/** The theme blocks: the two top-level ones by selector and the two prefers-color-scheme ones. */
function blocks(): Record<"dark" | "light" | "autoDark" | "autoLight", Map<string, string>> {
  const top = new Map<string, string>();
  for (const m of CSS.matchAll(/^(:root[^{]*)\{([^}]*)\}/gm)) top.set(m[1]!.trim(), m[2]!);
  const media = new Map<string, string>();
  for (const m of CSS.matchAll(/^@media \(prefers-color-scheme: (dark|light)\) \{\n\s*:root\[data-theme="auto"\] \{([^}]*)\}/gm)) {
    media.set(m[1]!, m[2]!);
  }
  const dark = [...top].find(([sel]) => sel.includes('[data-theme="dark"]'));
  const light = [...top].find(([sel]) => sel.includes('[data-theme="light"]'));
  expect(dark && light && media.get("dark") && media.get("light")).toBeTruthy();
  return {
    dark: declarations(dark![1]),
    light: declarations(light![1]),
    autoDark: declarations(media.get("dark")!),
    autoLight: declarations(media.get("light")!),
  };
}

/** Hue in degrees (0-360) of a #rrggbb colour. */
function hue(hex: string): number {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  if (!m) throw new Error(`not a #rrggbb colour: ${hex}`);
  const [r, g, b] = [m[1]!, m[2]!, m[3]!].map((x) => parseInt(x, 16) / 255) as [number, number, number];
  const max = Math.max(r, g, b);
  const d = max - Math.min(r, g, b);
  if (d === 0) return 0;
  let h: number;
  if (max === r) h = ((g - b) / d) % 6;
  else if (max === g) h = (b - r) / d + 2;
  else h = (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

const isGreen = (h: number) => h >= 90 && h <= 160;
const isRed = (h: number) => h >= 340 || h <= 20;

describe("theme tokens (acceptance test 8)", () => {
  it("every TOKENS name is defined in the dark, light and both prefers-color-scheme blocks", () => {
    const all = blocks();
    for (const [name, decl] of Object.entries(all)) {
      const missing = Object.values(TOKENS).filter((property) => !decl.has(property));
      expect(missing, name).toEqual([]);
    }
    // auto follows the matching palette exactly
    expect([...all.autoDark]).toEqual([...all.dark]);
    expect([...all.autoLight]).toEqual([...all.light]);
  });

  it("the dark theme is the default (also without a data-theme attribute)", () => {
    expect(CSS).toMatch(/^:root,\n:root\[data-theme="dark"\] \{/m);
  });

  it("money is green/red and no status colour is", () => {
    for (const [name, decl] of Object.entries(blocks())) {
      expect(isGreen(hue(decl.get("--money-up")!)), `${name} money-up`).toBe(true);
      expect(isRed(hue(decl.get("--money-down")!)), `${name} money-down`).toBe(true);
      for (const status of ["--status-ok", "--status-warn", "--status-bad", "--status-muted"]) {
        const h = hue(decl.get(status)!);
        expect(isGreen(h) || isRed(h), `${name} ${status} hue ${h.toFixed(1)}`).toBe(false);
      }
    }
  });

  it("panels are flat with 1 px borders and 44 px touch targets", () => {
    const dark = blocks().dark;
    expect(dark.get("--panel-border")).toBe("1px solid var(--border)");
    expect(dark.get("--panel-radius")).toBe("6px");
    expect(dark.get("--touch")).toBe("44px");
    expect(CSS.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(/box-shadow|gradient|text-shadow|glow/);
    expect(CSS).toMatch(/font-variant-numeric:\s*tabular-nums/);
  });

  it("moneyTone gives the sign of a decimal string or number", () => {
    expect(moneyTone("-0.01")).toBe("down");
    expect(moneyTone("0")).toBe("flat");
    expect(moneyTone("0.0000")).toBe("flat");
    expect(moneyTone("-0.0000")).toBe("flat");
    expect(moneyTone(null)).toBe("flat");
    expect(moneyTone(undefined)).toBe("flat");
    expect(moneyTone("")).toBe("flat");
    expect(moneyTone("abc")).toBe("flat");
    expect(moneyTone("12.5000")).toBe("up");
    expect(moneyTone(-3)).toBe("down");
  });

  it("readToken is empty without a document", () => {
    expect(readToken("moneyUp")).toBe("");
    expect(THEME_STORAGE_KEY).toBe("trader.theme");
  });
});
