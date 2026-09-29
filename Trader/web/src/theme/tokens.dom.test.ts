// DB-T1 acceptance test 8 (DOM half): readToken reads the computed custom property on <html>.
import { afterEach, describe, expect, it } from "vitest";

import { readToken } from "./tokens";

afterEach(() => {
  document.documentElement.style.removeProperty("--money-up");
});

describe("readToken", () => {
  it("is empty without the stylesheet and for unknown names", () => {
    expect(readToken("moneyUp")).toBe("");
    expect(readToken("--nope")).toBe("");
    expect(readToken("notAToken")).toBe("");
  });

  it("reads a short name or a custom property, trimmed", () => {
    document.documentElement.style.setProperty("--money-up", " #4ade80 ");
    expect(readToken("moneyUp")).toBe("#4ade80");
    expect(readToken("--money-up")).toBe("#4ade80");
  });
});
