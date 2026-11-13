import { siteConfig } from '../site.config';

/**
 * Applies the githubPublic switch to rendered markdown. Links to repository files that are not on the site were
 * written as GitHub links marked data-repo-link; while the repository is private they become plain text.
 * Done at page render, not in the markdown pipeline, so flipping the switch never depends on Astro's content cache.
 */
export function applyRepoLinks(html: string): string {
  if (siteConfig.githubPublic) return html.replace(/\sdata-repo-link="[^"]*"/g, '');
  return html.replace(/<a\b[^>]*\bdata-repo-link="[^"]*"[^>]*>([\s\S]*?)<\/a>/g, '<span class="repo-ref">$1</span>');
}
