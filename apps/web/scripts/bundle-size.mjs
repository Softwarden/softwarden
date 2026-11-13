// The initial JS bundle check (the performance target was initial JS under 300 KB gzipped; the limit is now
// tighter, below). `npm run size` after
// `npm run build`; CI runs it too. "Initial" is what dist/index.html loads before any lazy route: its scripts (the
// module entry and the classic theme script) and its modulepreloaded chunks, each gzipped at level 9 and summed. Lazy
// chunks (the dashboard's charts, the command palette...) are listed for information and don't count. No dependencies.
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

// 230 KB: 230,000 bytes, as CLAUDE.md and CI name it. It was 307,200 (300 KiB)
// at first and 300,000 (the original target) until initial JS dropped to about 134 KB; the lower limit keeps that headroom
// from being spent unnoticed.
export const LIMIT = 230_000;

/** The attributes of one start tag's attribute text, names lower-cased; a bare attribute is "". */
function attributes(text) {
  const found = {};
  for (const m of text.matchAll(/([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
    found[m[1].toLowerCase()] = m[2] ?? m[3] ?? m[4] ?? "";
  }
  return found;
}

const REMOTE = /^([a-z][a-z0-9+.-]*:)?\/\//i;

/** The JS files an index.html loads up front, in document order and once each: `<script src>` (module or classic)
 * and `<link rel="modulepreload" href>`. Remote URLs, inline scripts, data blocks and comments are left out. */
export function initialScripts(html) {
  const source = html.replace(/<!--[\s\S]*?-->/g, "");
  const found = [];
  for (const m of source.matchAll(/<(script|link)\b([^>]*)>/gi)) {
    const attrs = attributes(m[2]);
    let url;
    if (m[1].toLowerCase() === "script") {
      const type = (attrs.type ?? "").toLowerCase();
      if (attrs.src && ["", "module", "text/javascript"].includes(type)) url = attrs.src;
    } else if ((attrs.rel ?? "").toLowerCase().split(/\s+/).includes("modulepreload")) {
      url = attrs.href;
    }
    if (url && !REMOTE.test(url) && !found.includes(url)) found.push(url);
  }
  return found;
}

const gz = (file) => gzipSync(readFileSync(file), { level: 9 }).length;
const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
const bytesText = (bytes) => bytes.toLocaleString("en-GB");
const row = (name, bytes) => `  ${name.padEnd(44)} ${bytesText(bytes).padStart(9)} B ${kb(bytes).padStart(9)}`;

/** {initial: [[path, gzipped bytes]], lazy: [[path, gzipped bytes]], total} for a built dist directory. */
export function measure(dist) {
  const urls = initialScripts(readFileSync(path.join(dist, "index.html"), "utf8"));
  // Vite's base is "/": every URL is a path inside dist.
  const initial = urls.map((url) => {
    const rel = url.replace(/^\.?\//, "").split(/[?#]/)[0];
    return [rel, gz(path.join(dist, rel))];
  });
  const counted = new Set(initial.map(([rel]) => rel));
  const lazy = readdirSync(path.join(dist, "assets"))
    .filter((name) => name.endsWith(".js") && !counted.has(`assets/${name}`))
    .map((name) => [`assets/${name}`, gz(path.join(dist, "assets", name))])
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return { initial, lazy, total: initial.reduce((sum, [, bytes]) => sum + bytes, 0) };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const { initial, lazy, total } = measure(path.resolve(process.argv[2] ?? "dist"));
  console.log("Initial JS (what dist/index.html loads), gzip -9:");
  for (const [name, bytes] of initial) console.log(row(name, bytes));
  console.log(row("Total", total));
  console.log(row("Limit", LIMIT));
  if (lazy.length) {
    console.log("Lazy chunks (information only, not counted):");
    for (const [name, bytes] of lazy) console.log(row(name, bytes));
  }
  if (!initial.some(([name]) => name.startsWith("assets/"))) {
    console.error("size: no entry chunk found in dist/index.html, so nothing was measured.");
    process.exit(1);
  }
  if (total > LIMIT) {
    console.error(`size: initial JS is ${bytesText(total)} B gzipped, over the ${bytesText(LIMIT)} B limit.`);
    process.exit(1);
  }
  console.log(`size: initial JS ${bytesText(total)} B gzipped, within the ${bytesText(LIMIT)} B limit.`);
}
