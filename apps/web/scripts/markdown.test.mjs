// The permissions reference's Markdown reader (src/features/discovery/markdown.ts): links only for https:// and
// #anchors, table cells that keep a `|` inside backticks, and fenced code kept as text. Run by `npm run lint`
// (Node strips the TypeScript types; the module has no imports).
import assert from "node:assert/strict";
import test from "node:test";
import { cells, inline, parse } from "../src/features/discovery/markdown.ts";

test("only https and in-page links become links; other schemes are text", () => {
  assert.deepEqual(inline("[x](javascript:alert(1))"), [{ t: "text", text: "x" }, { t: "text", text: ")" }]);
  assert.deepEqual(inline("[x](data:text/html,hi)"), [{ t: "text", text: "x" }]);
  assert.deepEqual(inline("[x](JavaScript:void)"), [{ t: "text", text: "x" }]);
  assert.deepEqual(inline("[file](../core/graph.py)"), [{ t: "text", text: "file" }]);
  assert.deepEqual(inline("see [Learn](https://learn.microsoft.com/a)"), [
    { t: "text", text: "see " }, { t: "link", text: "Learn", href: "https://learn.microsoft.com/a", external: true },
  ]);
  assert.deepEqual(inline("[up](#discovery-source-entra)"), [{ t: "link", text: "up", href: "#discovery-source-entra", external: false }]);
  assert.deepEqual(inline("[bad](#a\"onclick)"), [{ t: "text", text: "bad" }]);
});

test("inline code and bold are tokens, never markup", () => {
  assert.deepEqual(inline("a `<b>` **c**"), [
    { t: "text", text: "a " }, { t: "code", text: "<b>" }, { t: "text", text: " " }, { t: "bold", text: "c" },
  ]);
});

test("tables: a pipe inside backticks or escaped stays in its cell", () => {
  assert.deepEqual(cells(String.raw`| a | ${"`x | y`"} | b \| c |`), ["a", "`x | y`", "b | c"]);
  const blocks = parse("## Grants\n\n| Name | Value |\n|---|---|\n| one | `a|b` |\n| two | 2 |\n");
  assert.deepEqual(blocks[1], { t: "table", head: ["Name", "Value"], rows: [["one", "`a|b`"], ["two", "2"]], caption: "Grants" });
});

test("code blocks keep their lines as text, headings inside them included", () => {
  const blocks = parse("Intro\n\n```bash\n# not a heading\n| not | a table |\n<script>x</script>\n```\nAfter");
  assert.deepEqual(blocks, [
    { t: "p", text: "Intro" },
    { t: "code", text: "# not a heading\n| not | a table |\n<script>x</script>" },
    { t: "p", text: "After" },
  ]);
});

test("headings, lists with continuation lines and rules", () => {
  assert.deepEqual(parse("# T\n- one\n  more\n- two\n\n1. a\n2. b\n\n---"), [
    { t: "h", level: 1, text: "T" }, { t: "ul", items: ["one more", "two"] }, { t: "ol", items: ["a", "b"] }, { t: "hr" },
  ]);
});
