/**
 * Resolves links found in the repository's markdown to site URLs, GitHub URLs or nothing.
 * Shared by the rehype plugin (which rewrites the rendered HTML) and the build-time link check (which reads the
 * markdown directly, so it reports every link on every build, whatever the content cache holds).
 */
import fs from 'node:fs';
import path from 'node:path';
import GithubSlugger from 'github-slugger';
import { DOCS, docByFile, docUrl } from './docs-manifest';
import { githubUrl } from '../site.config';

/** The site is built from site/, so the repository is its parent. */
export const REPO_ROOT = path.resolve(process.cwd(), '..');

export type Resolved =
  | { kind: 'external' }
  | { kind: 'site'; href: string }
  | { kind: 'repo'; href: string; repoPath: string }
  | { kind: 'unresolved'; reason: string };

const toPosix = (p: string) => p.split(path.sep).join('/');

/** Repository-relative path of a file on disk, e.g. "docs/INSTALL.md". */
export const repoRelative = (absPath: string) => toPosix(path.relative(REPO_ROOT, absPath));

const headingCache = new Map<string, Set<string>>();

/** Heading ids of a markdown file, made the way Astro (and GitHub) make them. */
export function headingIds(repoPath: string): Set<string> {
  const cached = headingCache.get(repoPath);
  if (cached) return cached;
  const ids = new Set<string>();
  const slugger = new GithubSlugger();
  const text = fs.readFileSync(path.join(REPO_ROOT, repoPath), 'utf8');
  let fence: string | null = null;
  for (const line of text.split(/\r?\n/)) {
    const f = line.match(/^\s*(```+|~~~+)/);
    if (f) {
      if (!fence) fence = f[1][0];
      else if (f[1][0] === fence) fence = null;
      continue;
    }
    if (fence) continue;
    const h = line.match(/^#{1,6}\s+(.*?)\s*#*\s*$/);
    if (!h) continue;
    const plain = h[1]
      .replace(/`([^`]*)`/g, '$1')
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/(\*\*|__|\*|_)(.+?)\1/g, '$2')
      .replace(/<[^>]+>/g, '');
    ids.add(slugger.slug(plain));
  }
  headingCache.set(repoPath, ids);
  return ids;
}

const SITE_FILES: Record<string, string> = { 'CHANGELOG.md': '/changelog/' };

/**
 * @param href the link as written in the markdown
 * @param fromRepoPath the repository-relative path of the file the link is in
 */
export function resolveLink(href: string, fromRepoPath: string): Resolved {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) return { kind: 'external' };

  const hashAt = href.indexOf('#');
  const rawPath = hashAt === -1 ? href : href.slice(0, hashAt);
  const hash = hashAt === -1 ? '' : href.slice(hashAt + 1);

  let target = fromRepoPath;
  if (rawPath) {
    const decoded = decodeURIComponent(rawPath);
    const base = decoded.startsWith('/') ? '' : path.posix.dirname(fromRepoPath);
    target = path.posix.normalize(path.posix.join(base, decoded.replace(/^\//, '')));
  }
  if (target.startsWith('..')) return { kind: 'unresolved', reason: 'points outside the repository' };

  const abs = path.join(REPO_ROOT, target);
  const exists = fs.existsSync(abs);
  if (!exists) return { kind: 'unresolved', reason: `no file ${target}` };

  const isMarkdown = target.endsWith('.md');
  if (hash && isMarkdown && !headingIds(target).has(hash)) {
    return { kind: 'unresolved', reason: `no heading #${hash} in ${target}` };
  }

  const docsRel = target.startsWith('docs/') ? target.slice('docs/'.length) : null;
  const doc = docsRel ? docByFile(docsRel) : undefined;
  if (doc) return { kind: 'site', href: docUrl(doc) + (hash ? `#${hash}` : '') };
  if (SITE_FILES[target]) return { kind: 'site', href: SITE_FILES[target] + (hash ? `#${hash}` : '') };

  const isDir = fs.statSync(abs).isDirectory();
  return {
    kind: 'repo',
    repoPath: target,
    href: githubUrl(target, isDir ? 'tree' : 'blob') + (hash ? `#${hash}` : ''),
  };
}

export interface LinkReport {
  file: string;
  line: number;
  href: string;
  reason: string;
}

/** Every markdown link in the published files, checked. Code spans and fenced blocks are skipped. */
export function checkAllLinks(): { unresolved: LinkReport[]; toRepo: LinkReport[] } {
  const files = [...DOCS.map((d) => `docs/${d.file}`), 'CHANGELOG.md'];
  const unresolved: LinkReport[] = [];
  const toRepo: LinkReport[] = [];
  for (const file of files) {
    const lines = fs.readFileSync(path.join(REPO_ROOT, file), 'utf8').split(/\r?\n/);
    let fence: string | null = null;
    lines.forEach((line, i) => {
      const f = line.match(/^\s*(```+|~~~+)/);
      if (f) {
        if (!fence) fence = f[1][0];
        else if (f[1][0] === fence) fence = null;
        return;
      }
      if (fence) return;
      const noCode = line.replace(/`[^`]*`/g, '');
      for (const m of noCode.matchAll(/\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/g)) {
        const href = m[1];
        const r = resolveLink(href, file);
        if (r.kind === 'unresolved') unresolved.push({ file, line: i + 1, href, reason: r.reason });
        else if (r.kind === 'repo') toRepo.push({ file, line: i + 1, href, reason: r.repoPath });
      }
    });
  }
  return { unresolved, toRepo };
}
