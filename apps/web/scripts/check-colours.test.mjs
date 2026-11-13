// Cases for check-colours.mjs. Run by `npm run lint` (node --test).
import assert from "node:assert/strict";
import { test } from "node:test";
import { findRawColours } from "./check-colours.mjs";

const flagged = (src) => findRawColours(src).length > 0;

test("raw colours are flagged", () => {
  for (const src of [
    'const x = "#fff";',
    "border: 1px solid #2457C5;",
    "color: rgb(0 0 0);",
    "background: color-mix(in oklab, var(--a) 50%, transparent);",
    '<p style={{color:"red"}}>',
    '<path style={{ stroke: "tomato" }} />',
    "<path fill={\"red\"} />",
    '<path fill="white" />',
    "<rect stroke={'navy'} />",
    ".x{color:red;background:white}",
    ".y { border-color: rebeccapurple; }",
    '<div className="bg-[red] p-2">',
    '<div className="text-[navy]">',
    '<div className="border-l-red-500">',
    '<div className="ring-offset-white">',
    '<span className="text-white">',
    '<div className="bg-black/50">',
    'cy.style({"line-color":"gray"})',
    "cy.style().selector('edge').style({ 'target-arrow-color': 'black' })",
    "{ backgroundColor: 'whitesmoke' }",
  ]) assert.ok(flagged(src), `not flagged: ${src}`);
});

test("tokens and non-colour values pass", () => {
  for (const src of [
    '<a href="#main">Skip</a>',
    '<path fill="none" stroke="currentColor" />',
    '<div className="bg-card text-link border-accent-line stroke-primary-foreground ring-offset-background">',
    "color: var(--link);",
    "border: 1px solid CanvasText; background: Canvas; color: CanvasText;",
    '{ fill: "full" }',
    "background: transparent;",
    'const tone = { tone: "text-criticality-high", fill: "half" };',
    "<Diamond fill={c.fill} />",
    '<Field id="red-flag" label="Red flag" />',
    "box-shadow: inset 3px 0 0 var(--sidebar-ring);",
  ]) assert.deepEqual(findRawColours(src), [], `flagged: ${src}`);
});
