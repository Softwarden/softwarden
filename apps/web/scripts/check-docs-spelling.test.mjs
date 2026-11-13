// Cases for check-docs-spelling.mjs. Run by `npm run lint` (node --test).
import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";
import { ROOT, documents, findBritishInDoc, findBritishInFile, fixDocument, fixLine, isDocument } from "./check-docs-spelling.mjs";

const flagged = (text, markdown = true) => findBritishInDoc(text, markdown).map((f) => f.word);

test("British prose is flagged and fixed, keeping case and punctuation", () => {
  assert.deepEqual(flagged("Each organisation's licences, colour and behaviour."), ["organisation's", "licences,", "colour", "behaviour."]);
  assert.equal(
    fixLine("**Licence pools** per-organisation: cancelled, labelled, annualised (centre)."),
    "**License pools** per-organization: canceled, labeled, annualized (center).",
  );
  assert.equal(fixLine("Reorganised and denormalised, a tunnelled defence"), "Reorganized and denormalized, a tunneled defense");
});

test("code, links, URLs and identifiers keep their spelling", () => {
  for (const line of [
    "Run `entra_licence_usage` against `/reports/licence-waste`.",
    "The status is ``cancelled`` and the source is entra_licence_usage.",
    "See [the report](https://example.com/licence-waste) and https://example.com/organisation.",
    "GET /api/v1/reports/licence-waste/export.csv",
    "The capability discovery.licences and SOFTWARDEN_LICENCE_KEY stay, as does licence-waste.",
    "<span aria-labelledby=\"licence\">",
    "Placeholders such as {organisation} and $LICENCE stay.",
  ]) assert.deepEqual(flagged(line), [], line);
});

test("fenced code blocks are skipped in Markdown but read elsewhere", () => {
  const md = "Text\n```bash\n# every organisation\n```\nThe organisation\n";
  assert.equal(fixDocument(md), "Text\n```bash\n# every organisation\n```\nThe organization\n");
  assert.equal(fixDocument("# the organisation\n", false), "# the organization\n");
});

test("line endings are kept", () => {
  assert.equal(fixDocument("The licence\r\nThe colour\r\n"), "The license\r\nThe color\r\n");
});

test("the contributor guide, the runbooks, the agents' guide and the docs are read; HTML is not", () => {
  const read = documents().map((f) => path.relative(ROOT, f).split(path.sep).join("/"));
  assert.ok(read.includes("docs/API.md") && read.includes("docs/PERMISSIONS.md") && read.includes("README.md"));
  assert.ok(read.includes("docs/DEVELOPMENT.md") && isDocument("docs/DEVELOPMENT.md"));
  assert.ok(read.includes("docs/04-development/backup-runbook.md"));
  for (const name of ["CLAUDE.md", "AGENTS.md"]) {
    assert.ok(read.includes(name) && isDocument(name), name);
  }
  assert.ok(!read.some((f) => f.endsWith(".html")));
  assert.ok(!isDocument("docs/04-development/page.html"));
});

test("YAML, templates and scripts are data, not prose: never flagged, never rewritten", () => {
  const data = "status: cancelled\ncolour: red\nsoftwarden sync --organisation x\n";
  for (const rel of ["deploy/helm/softwarden/values.yaml", "deploy/helm/softwarden/templates/app.yaml", "ops/backup.sh", "ops/softwarden-backup.service"]) {
    assert.ok(!isDocument(rel), rel);
    assert.deepEqual(findBritishInFile(rel, data), [], rel);
  }
  const read = documents().map((f) => path.relative(ROOT, f).split(path.sep).join("/")).filter(isDocument);
  assert.ok(!read.some((f) => f.startsWith("ops/") || /\.(ya?ml|tpl|sh)$/.test(f)));
  assert.ok(isDocument("deploy/helm/softwarden/README.md") && isDocument("deploy/helm/softwarden/templates/NOTES.txt"));
  assert.equal(findBritishInFile("deploy/helm/softwarden/templates/NOTES.txt", "The organisation {{ .Values.x }}\n").length, 1);
});

test("the kept identifiers match a whole word or path segment only", () => {
  for (const line of ["GET /reports/licence-waste", "the licence-waste export", "licence-waste.csv", "/license-pools/{id}/licence-waste?days=90"]) {
    assert.deepEqual(flagged(line), [], line);
  }
  assert.deepEqual(flagged("The Licence-waste report"), ["Licence-waste"]);
  assert.deepEqual(flagged("A licence-wastes table"), ["licence-wastes"]);
});
