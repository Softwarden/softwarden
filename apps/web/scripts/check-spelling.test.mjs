// Cases for check-spelling.mjs. Run by `npm run lint` (node --test).
import assert from "node:assert/strict";
import { test } from "node:test";
import { americanize, findBritish } from "./check-spelling.mjs";

const flagged = (src) => findBritish(src, "x.tsx").length > 0;

test("British forms in visible text are flagged", () => {
  for (const src of [
    'const t = "Licence pools";',
    "<h2>Licences</h2>",
    "const s = `${n} licences unused`;",
    'label: "Organisation settings"',
    "<p>Pick a colour for the tag.</p>",
    '<Field label="Cost centre" />',
    'const x = "Annualised spend";',
    "<span>Utilisation</span>",
    "<span>Cancelled</span>",
    'aria-label="Catalogue search"',
    "<p>Default behaviour</p>",
    "const m = `Seats ${a} of ${b}, licence ${c}`;",
    // JSX text is always read, even a lone lower-case word.
    "<span>licences</span>",
    "<p><b>{n}</b> organisation</p>",
    "<p>{count}licence</p>",
    // practise and the -yse verbs.
    "<p>Practise the restore</p>",
    'const t = "We analyse usage";',
    "<p>Sign-in paralysed by the outage</p>",
    'const c = "Catalysed by the change";',
  ]) assert.ok(flagged(src), `not flagged: ${src}`);
});

test("a lone lower-case string literal is an identifier, but JSX text never is", () => {
  assert.ok(!flagged('const v = "licences";'));
  assert.ok(flagged("<td>licences</td>"));
  assert.equal(americanize("licences", true), "licenses");
  assert.equal(americanize("practising", true), "practicing");
  assert.equal(americanize("analysed", true), "analyzed");
});

test("API names, paths, identifiers and comments pass", () => {
  for (const src of [
    'api("licence-waste")',
    'const k = "licence_waste";',
    'if (s === "cancelled") {}',
    "const p = `license-pools/${id}/licence-waste?${q}`;",
    "// Licence pools, organisation, colour: comments are not checked",
    "/* Annualised */ const x = 1;",
    'import { LicenceWaste } from "./licence-waste";',
    "type LicenceWasteRow = { licence: string };",
    'data-testid="licence-waste"',
    "<p>License pools, organization, color, canceled, cancellation</p>",
    "<p>Placed under emphasis; analyses show</p>",
  ]) assert.ok(!flagged(src), `flagged: ${src}`);
});

test("the fix keeps case and spells the American form", () => {
  const cases = {
    "Licence pools": "License pools", licences: "licenses", LICENCE: "LICENSE", Organisation: "Organization",
    "organisations": "organizations", "Colours": "Colors", catalogue: "catalog", Behaviour: "Behavior",
    "Cost centre": "Cost center", Utilisation: "Utilization", "Annualised spend": "Annualized spend",
    Cancelled: "Canceled", cancelling: "canceling", labelled: "labeled", acknowledgement: "acknowledgment",
    artefacts: "artifacts", Grey: "Gray", cancellable: "cancelable", programmes: "programs", Analysed: "Analyzed", reorganised: "reorganized",
    denormalised: "denormalized", Armour: "Armor", tunnelled: "tunneled",
  };
  // In a sentence: a lone lower-case word is an identifier and is left alone.
  for (const [from, to] of Object.entries(cases)) assert.equal(americanize(`The ${from}`), `The ${to}`);
  assert.equal(americanize("cancelled"), "cancelled");
});
