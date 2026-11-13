// Cases for bundle-size.mjs. Run by `npm run lint` (node --test).
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { initialScripts, measure } from "./bundle-size.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = readFileSync(path.join(here, "fixtures", "bundle-index.html"), "utf8");
const INITIAL = ["theme-init.js", "assets/index-AbC123.js", "assets/vendor-react-XyZ789.js", "assets/vendor-query-Q1.js"];

test("initial scripts are the classic and module scripts and the modulepreloads, in document order, once each", () => {
  assert.deepEqual(
    initialScripts(fixture),
    INITIAL.map((name) => `/${name}`),
  );
});

test("comments, stylesheets, fonts, data blocks, inline and remote scripts are not counted", () => {
  const found = initialScripts(fixture).join(" ");
  for (const skipped of ["commented-out", ".css", ".woff2", "remote.js", "ld+json"]) assert.ok(!found.includes(skipped), skipped);
});

test("measure sums the initial files gzipped and lists every other chunk as lazy", () => {
  const dist = mkdtempSync(path.join(tmpdir(), "bundle-size-"));
  try {
    mkdirSync(path.join(dist, "assets"));
    const files = {
      "theme-init.js": "document.documentElement.dataset.theme = 'light';",
      "assets/index-AbC123.js": "export const a = 1;\n".repeat(200),
      "assets/vendor-react-XyZ789.js": "export const r = 2;\n".repeat(100),
      "assets/vendor-query-Q1.js": "export const q = 3;\n",
      "assets/dashboard-Lazy1.js": "export const chart = 4;\n".repeat(500),
    };
    for (const [name, text] of Object.entries(files)) writeFileSync(path.join(dist, name), text);
    writeFileSync(path.join(dist, "index.html"), fixture);
    const size = (name) => gzipSync(Buffer.from(files[name]), { level: 9 }).length;
    const { initial, lazy, total } = measure(dist);
    assert.deepEqual(initial, INITIAL.map((name) => [name, size(name)]));
    assert.equal(total, INITIAL.reduce((sum, name) => sum + size(name), 0));
    assert.deepEqual(lazy, [["assets/dashboard-Lazy1.js", size("assets/dashboard-Lazy1.js")]]);
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
});
