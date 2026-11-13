/**
 * Site switches. Everything a maintainer may need to flip before or after launch lives here.
 * Read by astro.config.ts (build-time link handling) and by the pages.
 */
export const siteConfig = {
  /** Canonical origin. Used for canonical URLs, Open Graph and the sitemap. */
  url: 'https://softwarden.dev',

  /** Product version shown in the footer and on the changelog page. */
  version: '1.0.0',

  /** The repository. Its address appears in the install snippet whatever `githubPublic` says. */
  repo: 'https://github.com/Softwarden/softwarden',
  /** Branch that repository links point at when `githubPublic` is on. */
  repoBranch: 'main',

  /**
   * The repository is not public yet. While false: no GitHub links or buttons in the nav, footer or calls to action,
   * and links in the docs to repository files that are not on this site render as plain text.
   */
  githubPublic: false,

  signup: {
    /**
     * Buttondown username for release emails. Empty: the signup section is left out of production builds
     * (it still renders in `npm run dev`, with a "not configured" note).
     */
    buttondownUser: '',
  },

  /** Shown in the footer only when set, for example 'hello@softwarden.dev'. */
  contactEmail: 'hello@softwarden.dev',

  /**
   * When true, a docs link that can't be resolved fails the build. When false, the build prints the list and continues.
   */
  failOnUnresolvedDocLinks: true,
} as const;

export const githubUrl = (repoPath: string, kind: 'blob' | 'tree' = 'blob') =>
  `${siteConfig.repo}/${kind}/${siteConfig.repoBranch}/${repoPath}`;
