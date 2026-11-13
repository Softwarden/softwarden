import { defineConfig } from 'astro/config';
import type { AstroIntegration } from 'astro';
import sitemap from '@astrojs/sitemap';
import { unified } from '@astrojs/markdown-remark';
import rehypeDocs from './src/lib/rehype-docs';
import { checkAllLinks } from './src/lib/doc-links';
import { siteConfig } from './src/site.config';

/** Checks every link in the published markdown on each build and prints what it could not resolve. */
function docLinkCheck(): AstroIntegration {
  return {
    name: 'softwarden-doc-link-check',
    hooks: {
      'astro:config:done': ({ logger }) => {
        const { unresolved, toRepo } = checkAllLinks();
        const where = siteConfig.githubPublic ? 'linked to GitHub' : 'shown as plain text (githubPublic is off)';
        if (toRepo.length) {
          logger.info(`${toRepo.length} doc links point at repository files that are not on the site; ${where}.`);
        }
        if (unresolved.length) {
          const list = unresolved.map((u) => `  ${u.file}:${u.line}  ${u.href}  (${u.reason})`).join('\n');
          const msg = `${unresolved.length} unresolved doc links:\n${list}`;
          if (siteConfig.failOnUnresolvedDocLinks) throw new Error(msg);
          logger.warn(msg);
        } else {
          logger.info('Doc links: all resolved.');
        }
      },
    },
  };
}

export default defineConfig({
  site: siteConfig.url,
  output: 'static',
  trailingSlash: 'always',
  build: { format: 'directory' },
  integrations: [docLinkCheck(), sitemap({ filter: (page) => !page.includes('/404') })],
  markdown: {
    syntaxHighlight: 'shiki',
    shikiConfig: {
      themes: { light: 'github-light-high-contrast', dark: 'github-dark-high-contrast' }, // the default GitHub themes miss 4.5:1 for comments and some tokens
      defaultColor: false,
      wrap: false,
    },
    // The remark/rehype pipeline, for the rehype plugin that rewrites links and wraps tables.
    processor: unified({ rehypePlugins: [rehypeDocs] }),
  },
  vite: {
    server: { fs: { allow: ['..'] } },
    // three.js (about 530 KB minified, 135 KB gzipped) is one chunk, loaded only by the dependency map when it
    // nears the viewport. Nothing else comes close, so the warning only ever names that chunk.
    build: { chunkSizeWarningLimit: 600 },
  },
});
