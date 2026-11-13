// Fails when a colour is written anywhere in src/ except the token source (one token source; every colour
// is a token). Run by `npm run lint`; its cases are in check-colours.test.mjs. Catches hex literals, CSS colour functions,
// CSS named colours in colour properties (CSS, inline style objects, Cytoscape styles) and in attributes, Tailwind
// palette classes (text-white, border-l-red-500, ring-offset-white…) and arbitrary values (bg-[red]). Use a semantic
// token instead: bg-card, text-link, stroke-accent-line, var(--chart-1)…
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = path.join(root, "src");
// The token source and its generated CSS are the only places a colour value may appear.
const ALLOWED = new Set(["design/tokens.json", "design/tokens.css"]);
// Types written by `npm run api:types` from the API schema (no styles; a tag colour enum names "gray").
const GENERATED = "generated/";

/** CSS named colours (CSS Color 4), lower case. Not transparent, currentColor or the system colours (Canvas…). */
export const NAMED = new Set(`aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown
burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray
darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue
darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory
khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen
lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime
limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive
olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue
slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
yellowgreen`.split(/\s+/));
const NAMES = [...NAMED].sort((a, b) => b.length - a.length).join("|");

const PALETTE = "white|black|slate|gray|grey|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";
const PREFIX = "bg|text|border|border-[xytrblse]|fill|stroke|ring|ring-offset|inset-ring|outline|from|via|to|shadow|inset-shadow|drop-shadow|text-shadow|decoration|accent|caret|divide|placeholder";

// A property that takes a colour, in CSS (`color: red`), a style object (`{ stroke: "tomato" }`, `backgroundColor`) or a
// Cytoscape style (`"line-color": "gray"`), and the value up to the end of the declaration.
const COLOUR_PROP = /(["']?)((?:[\w-]*(?:color|colour|fill|stroke|background|border|outline|shadow|caret|accent|decoration)[\w-]*))\1\s*:\s*(["']?)([^;}\n"'`]*)/gi;

export const RULES = [
  { name: "hex colour", re: /(?<![\w&/])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b/g },
  { name: "colour function", re: /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\(/g },
  { name: "Tailwind palette colour", re: new RegExp(`(?<![\\w-])(?:${PREFIX})-(?:${PALETTE})(?:-\\d{2,3})?(?![\\w-])`, "g") },
  { name: "Tailwind arbitrary named colour", re: new RegExp(`-\\[(?:${NAMES})\\]`, "gi") },
  { name: "named colour in an attribute", re: new RegExp(`\\b[\\w-]+=\\{?\\s*["'](?:${NAMES})["']\\s*\\}?`, "gi") },
];

function namedColourValues(line) {
  const found = [];
  for (const m of line.matchAll(COLOUR_PROP)) {
    // Words in the value, ignoring var(--name) references and system colours (Canvas, CanvasText…).
    const value = m[4].replace(/var\([^)]*\)/g, "");
    for (const word of value.match(/(?<![\w-])[a-z]+(?![\w-])/gi) ?? []) {
      if (NAMED.has(word.toLowerCase())) found.push(`${m[2]}: ${word}`);
    }
  }
  return found;
}

export function findRawColours(text) {
  const found = [];
  text.split(/\r?\n/).forEach((line, i) => {
    for (const rule of RULES) {
      for (const m of line.matchAll(rule.re)) found.push({ line: i + 1, rule: rule.name, match: m[0] });
    }
    for (const match of namedColourValues(line)) found.push({ line: i + 1, rule: "named colour in a colour property", match });
  });
  return found;
}

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else if (/\.(tsx?|css|html)$/.test(entry.name)) yield full;
  }
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const problems = [];
  for (const file of walk(SRC)) {
    const rel = path.relative(SRC, file).split(path.sep).join("/");
    if (ALLOWED.has(rel) || rel.startsWith(GENERATED)) continue;
    for (const f of findRawColours(readFileSync(file, "utf8"))) problems.push(`src/${rel}:${f.line}  ${f.rule}: ${f.match}`);
  }
  if (problems.length) {
    console.error(`Raw colours outside src/design/tokens.json (use a token):\n  ${problems.join("\n  ")}`);
    process.exit(1);
  }
  console.log("colours: no raw colours in src/ outside the token source.");
}
