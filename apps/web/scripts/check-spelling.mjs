// Fails when user-visible text in src/ uses a British spelling (the project uses American
// spelling everywhere, matching the API's field names). Run by `npm run lint`; its cases are in
// check-spelling.test.mjs. It reads the TypeScript syntax tree and checks only text a person can see: string literals,
// template literal text and JSX text. Comments are not checked, and a string that looks like an identifier (lower case
// with no spaces: an API field or enum value such as "licence_waste" or "cancelled", a path, a test ID) is skipped, so
// API names keep their spelling. JSX text is always read, whatever its shape. A deliberate exception goes in ALLOW,
// with the reason.
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(root, "src");
// Types written by `npm run api:types` from the API schema: names and descriptions come from the API.
const GENERATED = "generated/";

/** Exact strings allowed despite a British form, each with why. */
export const ALLOW = new Set([]);

// -ise / -isation roots whose American form is -ize / -ization.
const ISE = "organi|recogni|summari|prioriti|authori|customi|minimi|maximi|optimi|utili|annuali|normali|synchroni|categori|finali|initiali|seriali|standardi|visuali|speciali|emphasi|apologi|critici|memori|centrali|personali|locali|anonymi|saniti|randomi|characteri|generali|reali|formali|amorti|capitali|itemi|materiali|moneti|parametri|tokeni|containeri|stabili|neutrali|penali|moderni|harmoni|legitimi|digiti|scrutini|sympathi|familiari|rationali|triviali";

/** Replace `from` inside the matched word with `to`, upper case when the replaced letters were. */
const swap = (from, to) => (m) => m.replace(from, (x) => (x === x.toUpperCase() ? to.toUpperCase() : to));

/** A British form (a regex over one word) and how to spell it the American way. */
export const WORDS = [
  [/\blicence(s|d)?\b/gi, swap(/c(?=e[sd]?$)/i, "s")],
  [new RegExp(`\\b((?:re|de|un|non|over|under|pre)?(?:${ISE}))s(e|ed|es|ing|ation|ations|ational|er|ers)\\b`, "gi"), swap(/s(?=(e|ed|es|ing|ation|ations|ational|er|ers)$)/i, "z")],
  // -yse verbs ("analyses" is also the plural of analysis, so -es isn't flagged).
  [/\b(analy|paraly|cataly|dialy|electroly|hydroly)s(e|ed|ing)\b/gi, swap(/s(?=(e|ed|ing)$)/i, "z")],
  // The verb "practise"; American English uses "practice" for both.
  [/\bpractis(e|ed|es|ing)\b/gi, swap(/s(?=(e|ed|es|ing)$)/i, "c")],
  [/\bcolour(s|ed|ful|ing)?\b/gi, swap(/our/i, "or")],
  [/\bcatalogue(s)?\b/gi, swap(/ue(?=s?$)/i, "")],
  [/\b(behaviour|favour|honour|labour|neighbour|rumour|armour|endeavour|harbour|flavour|vapour|rigour|odour)(s|al|ed|ing|able|ite|ites)?\b/gi, swap(/our/i, "or")],
  [/\bcentre(s)?\b/gi, swap(/re(?=s?$)/i, "er")],
  [/\b(cancel|label|model|travel|signal|channel|level|total|fuel|counsel|tunnel|marshal|funnel)l(ed|ing|er|ers)\b/gi, swap(/ll(?=(ed|ing|er|ers)$)/i, "l")],
  [/\bcancellable\b/gi, swap(/ll/i, "l")],
  [/\b(judge|acknowledge)ment(s)?\b/gi, swap(/e(?=ments?$)/i, "")],
  [/\bartefact(s)?\b/gi, swap(/e(?=facts?$)/i, "i")],
  [/\bdefence\b/gi, swap(/c(?=e$)/i, "s")],
  [/\benrolment(s)?\b/gi, swap(/l(?=ments?$)/i, "ll")],
  [/\bfulfil(ment|s)?\b/gi, swap(/il(?=(ment|s)?$)/i, "ill")],
  [/\bgrey(s|ed|ish)?\b/gi, swap(/e(?=y)/i, "a")],
  [/\bprogramme(s)?\b/gi, swap(/mme(?=s?$)/i, "m")],
];

/** A string that names something rather than saying something: lower case, no spaces (an API field or value, a path,
 *  a test ID, a query string). */
const IDENTIFIER = /^[a-z0-9_.\-/:?=&#%{}$[\]*@+,|]*$/;

/** The British forms in one piece of visible text. JSX text (`jsx`) is always read: it is never an identifier, so a
 *  lone lower-case word there ("licences") counts. */
export function britishIn(text, jsx = false) {
  if (ALLOW.has(text) || (!jsx && IDENTIFIER.test(text.trim()))) return [];
  const found = [];
  for (const [re] of WORDS) for (const m of text.matchAll(re)) found.push(m[0]);
  return found;
}

/** `text` with every British form spelled the American way (`jsx` as for britishIn). */
export function americanize(text, jsx = false) {
  if (ALLOW.has(text) || (!jsx && IDENTIFIER.test(text.trim()))) return text;
  let out = text;
  for (const [re, fix] of WORDS) out = out.replace(re, fix);
  return out;
}

/** The text a person can see in a source file: string literals, template literal text and JSX text, as
 *  { node, text, sf }. Import and export specifiers are skipped. */
export function textNodes(source, fileName = "x.tsx") {
  const kind = fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, kind);
  const out = [];
  const visit = (node) => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) return;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node) || ts.isJsxText(node)) {
      out.push({ node, text: node.text, sf });
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return out;
}

export function findBritish(source, fileName = "x.tsx") {
  const found = [];
  for (const { node, text, sf } of textNodes(source, fileName)) {
    const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    for (const word of britishIn(text, ts.isJsxText(node))) found.push({ line, word, text: text.trim().slice(0, 80) });
  }
  return found;
}

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (/\.tsx?$/.test(entry.name)) yield full;
  }
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  let problems = 0;
  for (const file of walk(SRC)) {
    const rel = path.relative(SRC, file).split(path.sep).join("/");
    if (rel.startsWith(GENERATED)) continue;
    for (const f of findBritish(readFileSync(file, "utf8"), file)) {
      problems += 1;
      console.error(`src/${rel}:${f.line}: British spelling "${f.word}" in "${f.text}"`);
    }
  }
  if (problems) {
    console.error(`spelling: ${problems} British ${problems === 1 ? "form" : "forms"} in user-visible text. Use American spelling; an API name keeps the API's spelling.`);
    process.exit(1);
  }
  console.log("spelling: user-visible text in src/ uses American spelling.");
}
