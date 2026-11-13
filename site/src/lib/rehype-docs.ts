/**
 * Rehype plugin for the repository's markdown (docs and changelog):
 * - drops the first H1 (the page layout shows it as the title);
 * - rewrites relative links: published docs to their /docs/ URL, the changelog to /changelog/, other repository files
 *   to GitHub (marked data-repo-link, so a page can turn them into plain text while the repository is private);
 *   links that resolve to nothing become plain text (the build-time check lists them);
 * - wraps tables in a labelled region that scrolls on its own;
 * - turns placeholder "tags" such as <your tenant> or <name>, which markdown passes through as raw HTML, back into
 *   visible text;
 * - tidies wording meant for maintainers, without editing the files: a link whose text is a file name shows the
 *   page's name, "Status: written ..." paragraphs are left out, and a doc can name table columns and rows to leave
 *   out (docs-manifest dropColumns and dropRows).
 */
import { repoRelative, resolveLink } from './doc-links';
import { DOCS, docUrl } from './docs-manifest';

type Node = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: Node[];
};

const KNOWN_TAGS = new Set(
  (
    'a abbr address article aside audio b bdi bdo blockquote br button caption cite code col colgroup data dd del ' +
    'details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img input ins kbd label li main ' +
    'mark nav ol p picture pre q s samp section small source span strong sub summary sup table tbody td tfoot th ' +
    'thead time tr u ul var video wbr svg path g circle rect line polyline polygon'
  ).split(' '),
);

const textOf = (n: Node): string =>
  n.type === 'text' ? (n.value ?? '') : (n.children ?? []).map(textOf).join('');

function placeholderText(n: Node): string {
  const attrs = Object.keys(n.properties ?? {})
    .map((k) => k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`))
    .join(' ');
  return `<${n.tagName}${attrs ? ` ${attrs}` : ''}>`;
}

const isEl = (n: Node) => n.type === 'element';

const FILE_NAME_TEXT = /^(?:docs\/)?[\w./-]+\.md$/;

function rows(n: Node, out: { row: Node; parent: Node }[] = []) {
  for (const c of n.children ?? []) {
    if (c.tagName === 'tr') out.push({ row: c, parent: n });
    else rows(c, out);
  }
  return out;
}

function dropColumns(table: Node, names: string[], rowPrefixes: string[] = []) {
  const all = rows(table);
  if (!all.length) return;
  const header = (all[0].row.children ?? []).filter(isEl);
  const drop = new Set(header.map((c, i) => (names.includes(textOf(c).trim()) ? i : -1)).filter((i) => i >= 0));
  if (!drop.size) return;
  all.forEach(({ row, parent }, r) => {
    const cells = (row.children ?? []).filter(isEl);
    row.children = cells.filter((_, i) => !drop.has(i));
    const first = textOf(row.children[0] ?? { type: 'text', value: '' }).trim();
    if (r > 0 && (row.children.slice(1).every((c) => !textOf(c).trim()) || rowPrefixes.some((p) => first.startsWith(p)))) {
      parent.children = (parent.children ?? []).filter((x) => x !== row);
    }
  });
}

export default function rehypeDocs() {
  return (tree: Node, file: { path?: string; history?: string[] }) => {
    const abs = file.path ?? file.history?.[0];
    const from = abs ? repoRelative(abs) : null;
    const entry = DOCS.find((d) => `docs/${d.file}` === from);
    const navByHref = new Map(DOCS.map((d) => [docUrl(d), d.nav]));
    let droppedH1 = false;
    let lastHeading = '';
    let tableCount = 0;

    const walk = (parent: Node) => {
      const kids = parent.children;
      if (!kids) return;
      for (let i = 0; i < kids.length; i++) {
        const n = kids[i];

        if (n.type === 'raw' && typeof n.value === 'string') {
          const m = n.value.trim().match(/^<\/?([a-zA-Z][\w-]*)([^>]*)>$/);
          if (m && !KNOWN_TAGS.has(m[1].toLowerCase())) {
            kids[i] = { type: 'text', value: n.value };
          }
          continue;
        }
        if (n.type !== 'element' || !n.tagName) continue;
        if (n.tagName === 'pre' || n.tagName === 'code') continue;
        if (n.tagName === 'p' && /^Status: written/.test(textOf(n).trim())) {
          kids.splice(i, 1);
          i--;
          continue;
        }

        if (!KNOWN_TAGS.has(n.tagName)) {
          kids.splice(i, 1, { type: 'text', value: placeholderText(n) }, ...(n.children ?? []));
          i--;
          continue;
        }

        if (n.tagName === 'h1' && !droppedH1) {
          droppedH1 = true;
          kids.splice(i, 1);
          i--;
          continue;
        }
        if (/^h[2-4]$/.test(n.tagName)) lastHeading = textOf(n).trim();

        if (n.tagName === 'a' && from) {
          const href = String(n.properties?.href ?? '');
          if (href && !href.startsWith('#')) {
            const r = resolveLink(href, from);
            if (r.kind === 'site') {
              n.properties!.href = r.href;
              const nav = navByHref.get(r.href.split('#')[0]);
              if (nav && FILE_NAME_TEXT.test(textOf(n).trim())) n.children = [{ type: 'text', value: nav }];
            }
            else if (r.kind === 'repo') {
              n.properties!.href = r.href;
              n.properties!.dataRepoLink = r.repoPath;
            } else if (r.kind === 'unresolved') {
              kids[i] = { type: 'element', tagName: 'span', properties: { className: ['unresolved-link'] }, children: n.children ?? [] };
            }
          }
        }

        if (n.tagName === 'table') {
          if (entry?.dropColumns) dropColumns(n, entry.dropColumns, entry.dropRows);
          walk(n);
          tableCount++;
          kids[i] = {
            type: 'element',
            tagName: 'div',
            properties: {
              className: ['table-scroll'],
              role: 'region',
              tabIndex: 0,
              ariaLabel: lastHeading ? `Table: ${lastHeading}` : `Table ${tableCount}`,
            },
            children: [n],
          };
          continue;
        }
        walk(n);
      }
    };
    walk(tree);
  };
}
