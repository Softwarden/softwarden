// Fails when the repository's documents use a British spelling (the project uses American spelling
// everywhere). The words and their American forms are check-spelling.mjs's WORDS, so the SPA and the docs follow one
// list. Run by `npm run lint`; its cases are in check-docs-spelling.test.mjs. `--fix` rewrites the files.
//
// What is read: README.md, CHANGELOG.md, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, CLAUDE.md and AGENTS.md at
// the root, every *.md under docs/ (docs/DEVELOPMENT.md, the contributor guide, and the runbooks among them), and the Helm chart's README and
// NOTES.txt under deploy/helm/. Files that are mostly keys, values and flags (the chart's values and templates, the
// ops/ scripts and units) are not read: there a word such as `status: cancelled`, `colour: red` or
// `--organisation x` is data, and --fix must never rewrite it. HTML files are not read.
//
// What is skipped inside a file: fenced code blocks, inline code spans, link targets, URLs, HTML tags, and any word
// that names something rather than saying something: one containing _ / \ = @ { } < > $ | ~ or a dot or colon between
// letters (a field, path, file, env var or capability), and the hyphenated identifiers in IDENTIFIERS (URL paths and
// export names that keep their original spelling for compatibility, docs/API.md).
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WORDS } from "./check-spelling.mjs";

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/** Hyphenated names that keep a British form on purpose: API paths, export file names and an HTML attribute. Each
 *  matches the whole word or a path segment, in lower case, so prose such as "Licence-waste report" is still read. */
export const IDENTIFIERS = [/(^|\/)licence-(waste|pools)(\/|$|\.|\?)/, /^aria-labelledby(=|$)/];

const ROOT_FILES = ["README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "CLAUDE.md", "AGENTS.md"];
/** [folder, files to read in it, sub-folders to leave out]. */
const TREES = [
  ["docs", /\.md$/, []],
  ["deploy/helm", /^(README|NOTES\.txt$)/, []],
];

/** Whether a file (path relative to the repository) is read: Markdown, or a Helm README or NOTES.txt. */
export function isDocument(rel) {
  const name = rel.split("/").pop();
  if (!rel.includes("/")) return ROOT_FILES.includes(rel);
  if (rel.startsWith("docs/")) return name.endsWith(".md");
  if (rel.startsWith("deploy/helm/")) return /^(README|NOTES\.txt$)/.test(name);
  return false;
}

/** The British forms in one file (path relative to the repository); a file that isn't a document has none. */
export function findBritishInFile(rel, text) {
  return isDocument(rel) ? findBritishInDoc(text, rel.endsWith(".md")) : [];
}

/** A word that is a name: a field, path, file, env var, capability, address or template. */
const NAMED = /[_/\\=@{}<>$|~]|\w[.:]\w/;
/** Spans read as code or address, never as prose. */
const PROTECTED = /(`+)[\s\S]*?\1|\]\([^)]*\)|<[^>\s][^>]*>|https?:\/\/\S+/g;

const isName = (word) => NAMED.test(word) || IDENTIFIERS.some((re) => re.test(word));

/** One word spelled the American way (a name is returned as it is). */
export function fixWord(word) {
  if (isName(word)) return word;
  let out = word;
  for (const [re, fix] of WORDS) out = out.replace(re, fix);
  return out;
}

/** A run of prose (no code spans) with each word fixed; the whitespace is kept. */
const fixProse = (text) => text.split(/(\s+)/).map((part, i) => (i % 2 ? part : fixWord(part))).join("");

/** One line with its prose fixed and its code spans, link targets, URLs and tags kept. */
export function fixLine(line) {
  let out = "";
  let at = 0;
  for (const m of line.matchAll(PROTECTED)) {
    out += fixProse(line.slice(at, m.index)) + m[0];
    at = m.index + m[0].length;
  }
  return out + fixProse(line.slice(at));
}

/** A whole document fixed. Markdown fenced code blocks are kept as they are. */
export function fixDocument(text, markdown = true) {
  let fence = null;
  return text
    .split("\n")
    .map((line) => {
      const open = markdown && /^\s*(```+|~~~+)/.exec(line);
      if (fence) {
        if (open && open[1][0] === fence[0] && open[1].length >= fence.length) fence = null;
        return line;
      }
      if (open) {
        fence = open[1];
        return line;
      }
      return fixLine(line);
    })
    .join("\n");
}

/** The British forms in a document as { line, word, fixed }. */
export function findBritishInDoc(text, markdown = true) {
  const fixed = fixDocument(text, markdown).split("\n");
  const found = [];
  text.split("\n").forEach((line, i) => {
    if (line === fixed[i]) return;
    const before = line.split(/(\s+)/);
    const after = fixed[i].split(/(\s+)/);
    before.forEach((word, j) => {
      if (word !== after[j]) found.push({ line: i + 1, word, fixed: after[j] });
    });
  });
  return found;
}

function* walk(dir, match, skip) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    const rel = path.relative(ROOT, full).split(path.sep).join("/");
    if (entry.isDirectory()) {
      if (!skip.includes(rel)) yield* walk(full, match, skip);
    } else if (match.test(entry.name)) yield full;
  }
}

export function documents() {
  const files = [];
  for (const name of ROOT_FILES) {
    const full = path.join(ROOT, name);
    try {
      if (statSync(full).isFile()) files.push(full);
    } catch {
      // A root file that doesn't exist is skipped.
    }
  }
  for (const [dir, match, skip] of TREES) files.push(...walk(path.join(ROOT, dir), match, skip));
  return files;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const fix = process.argv.includes("--fix");
  let problems = 0;
  for (const file of documents()) {
    const rel = path.relative(ROOT, file).split(path.sep).join("/");
    if (!isDocument(rel)) continue;
    const markdown = rel.endsWith(".md");
    const text = readFileSync(file, "utf8");
    if (fix) {
      const fixed = fixDocument(text, markdown);
      if (fixed !== text) writeFileSync(file, fixed);
      continue;
    }
    for (const f of findBritishInFile(rel, text)) {
      problems += 1;
      console.error(`${rel}:${f.line}: British spelling "${f.word}" (American: "${f.fixed}")`);
    }
  }
  if (problems) {
    console.error(`docs spelling: ${problems} British ${problems === 1 ? "form" : "forms"}. Use American spelling; an identifier keeps its spelling in a code span.`);
    process.exit(1);
  }
  if (!fix) console.log("docs spelling: the documents use American spelling.");
}
