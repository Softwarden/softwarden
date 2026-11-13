// third-party-notices.mjs: the npm half of the image's THIRD_PARTY_NOTICES. Run by `npm run lint`.
import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { declaredLicense, notices, productionPackages } from "./third-party-notices.mjs";

const APP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("the license field is read in its SPDX, object and array forms", () => {
  assert.equal(declaredLicense({ license: "MIT" }), "MIT");
  assert.equal(declaredLicense({ license: { type: "BSD-3-Clause" } }), "BSD-3-Clause");
  assert.equal(declaredLicense({ licenses: [{ type: "MIT" }, { type: "Apache-2.0" }] }), "MIT OR Apache-2.0");
  assert.equal(declaredLicense({}), "UNKNOWN");
});

test("only production packages are listed: dev, devOptional and links are left out", () => {
  const lock = {
    packages: {
      "": { name: "app" },
      "node_modules/react": { version: "18.3.1" },
      "node_modules/eslint": { version: "9.0.0", dev: true },
      "node_modules/fsevents": { version: "2.3.3", devOptional: true },
      "node_modules/local": { link: true },
    },
  };
  assert.deepEqual(
    productionPackages(lock).map(([key]) => key),
    ["node_modules/react"],
  );
});

test("the real lock file lists the SPA's runtime packages with their license texts, and no build tools", () => {
  const { sections, unknown } = notices(APP);
  const names = new Set(sections.map((s) => s.name));
  for (const name of ["react", "react-dom", "recharts", "cytoscape", "cmdk", "@fontsource-variable/inter", "@scalar/api-reference-react"]) {
    assert.ok(names.has(name), name);
  }
  for (const name of ["eslint", "typescript", "@playwright/test", "@tailwindcss/vite"]) assert.ok(!names.has(name), name);
  const react = sections.find((s) => s.name === "react");
  assert.match(react.text, /^react 18\.3\.1\nLicense: MIT/);
  assert.match(react.text, /--- LICENSE ---\nMIT License\n\nCopyright \(c\) Facebook, Inc\./);
  assert.deepEqual(unknown, []);
});
