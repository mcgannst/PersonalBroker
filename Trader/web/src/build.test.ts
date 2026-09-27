// @vitest-environment node
// Acceptance test 7: the production build (the `vite build` half of `npm run build`; `tsc -b` runs in
// `npm run check`) writes index.html and content-hashed assets. Built into a temporary directory so the
// test never touches web/dist.
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "vite";
import { afterAll, describe, expect, it } from "vitest";

const webDir = fileURLToPath(new URL("..", import.meta.url));
let outDir = "";

afterAll(async () => {
  if (outDir) await rm(outDir, { recursive: true, force: true });
});

describe("production build (acceptance test 7)", () => {
  it("produces index.html and hashed assets", async () => {
    outDir = await mkdtemp(join(tmpdir(), "trader-web-build-"));
    await build({
      root: webDir,
      configFile: join(webDir, "vite.config.ts"),
      logLevel: "silent",
      build: { outDir, emptyOutDir: true },
    });
    const html = await readFile(join(outDir, "index.html"), "utf8");
    expect(html).toContain('<div id="root"></div>');
    expect(html).toContain('name="viewport" content="width=device-width, initial-scale=1"');
    expect(html).toContain("<title>Trader</title>");
    const assets = await readdir(join(outDir, "assets"));
    const js = assets.filter((f) => /^index-[\w-]{8,}\.js$/.test(f));
    const css = assets.filter((f) => /^index-[\w-]{8,}\.css$/.test(f));
    expect(js).toHaveLength(1);
    expect(css).toHaveLength(1);
    expect(html).toContain(`/assets/${js[0]}`);
    expect(html).toContain(`/assets/${css[0]}`);
  }, 60_000);
});
