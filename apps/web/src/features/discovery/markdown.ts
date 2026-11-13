/** The small Markdown reader behind the permissions reference (permissions-doc.tsx): blocks (headings, paragraphs,
 *  lists, tables, fenced code, rules) and inline tokens (code, bold, links). Pure functions with no imports, so
 *  scripts/markdown.test.mjs runs them under `node --test`. Nothing here produces HTML: the page renders the tokens as
 *  React elements. A link becomes a link only for https:// addresses and in-page #anchors; any other scheme
 *  (javascript:, data:, a relative repository path) is shown as its text. */

export type Block =
  | { t: "h"; level: number; text: string }
  | { t: "p"; text: string }
  | { t: "ul" | "ol"; items: string[] }
  | { t: "code"; text: string }
  | { t: "table"; head: string[]; rows: string[][]; caption: string }
  | { t: "hr" };

export type Token =
  | { t: "text"; text: string }
  | { t: "code"; text: string }
  | { t: "bold"; text: string }
  | { t: "link"; text: string; href: string; external: boolean };

/** A table row's cells: split on `|`, except inside backticks or when escaped as `\|`. */
export function cells(line: string): string[] {
  const row = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const out: string[] = [];
  let cur = "";
  let code = false;
  for (let i = 0; i < row.length; i++) {
    const ch = row[i];
    if (ch === "\\" && row[i + 1] === "|") { cur += "|"; i++; continue; }
    if (ch === "`") code = !code;
    if (ch === "|" && !code) { out.push(cur.trim()); cur = ""; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}

const ITEM = /^(\s*)([-*]|\d+\.)\s+(.*)$/;

export function parse(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let lastHeading = "";
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith("```")) {
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) body.push(lines[i]);
      blocks.push({ t: "code", text: body.join("\n") });
      i++;
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) { blocks.push({ t: "h", level: h[1].length, text: h[2].trim() }); lastHeading = h[2].trim(); i++; continue; }
    if (/^-{3,}\s*$/.test(line)) { blocks.push({ t: "hr" }); i++; continue; }
    if (line.trimStart().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] ?? "")) {
      const head = cells(line);
      const rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].trimStart().startsWith("|"); i++) rows.push(cells(lines[i]));
      blocks.push({ t: "table", head, rows, caption: lastHeading.replace(/`/g, "") });
      continue;
    }
    const bullet = ITEM.exec(line);
    if (bullet) {
      const ordered = /\d/.test(bullet[2]);
      const items: string[] = [];
      while (i < lines.length) {
        const m = ITEM.exec(lines[i]);
        if (m && m[1].length < 2) { items.push(m[3]); i++; continue; }
        // A continuation line (indented, or a nested item) joins the item above.
        if (lines[i].trim() && /^\s+/.test(lines[i]) && items.length) { items[items.length - 1] += ` ${lines[i].trim().replace(/^[-*]\s+/, "· ")}`; i++; continue; }
        break;
      }
      blocks.push({ t: ordered ? "ol" : "ul", items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*\||\s*[-*]\s|\d+\.\s)/.test(lines[i])) para.push(lines[i++].trim());
    if (para.length) blocks.push({ t: "p", text: para.join(" ") });
    else i++;
  }
  return blocks;
}

/** Inline Markdown as tokens: `code`, **bold** (its text is read again by the caller), [text](href). */
export function inline(text: string): Token[] {
  const out: Token[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\([^)]+\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: "text", text: text.slice(last, at) });
    const tok = m[0];
    if (tok.startsWith("`")) out.push({ t: "code", text: tok.slice(1, -1) });
    else if (tok.startsWith("**")) out.push({ t: "bold", text: tok.slice(2, -2) });
    else {
      const [, label, rawHref] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok) ?? ["", tok, ""];
      const href = rawHref.trim();
      if (/^https:\/\//i.test(href)) out.push({ t: "link", text: label, href, external: true });
      else if (/^#[A-Za-z0-9_-]+$/.test(href)) out.push({ t: "link", text: label, href, external: false });
      else out.push({ t: "text", text: label });
    }
    last = at + tok.length;
  }
  if (last < text.length) out.push({ t: "text", text: text.slice(last) });
  return out;
}
