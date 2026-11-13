# softwarden.dev

The public website for Softwarden: the landing page, the documentation and the changelog. A static [Astro](https://astro.build) site with TypeScript and plain CSS. It lives in `site/` and touches nothing else in the repository.

- `/` landing page: hero with the Needs attention queue in HTML, the three questions, the feature list, the dependency map, install, editions, release emails.
- `/docs/` the repository's own docs, rendered from `../docs` at build time.
- `/changelog/` the repository's `CHANGELOG.md`.
- `/404.html` not found.

## Commands

Run from `site/`. Needs Node 22.12 or later and npm.

```bash
npm ci            # or npm install to change dependencies
npm run dev       # http://localhost:4321, reloads on changes (also to ../docs)
npm run build     # static output in dist/; also checks every link in the published docs
npm run preview   # serves dist/
npm run check     # astro check: TypeScript and .astro diagnostics
```

Astro is pinned to 7.3.8 because 7.4 requires Node 22.23.4 or later. On a newer Node, `npm install astro@latest` is the only change needed.

## Switches

All in `src/site.config.ts`:

| Switch | Default | Effect |
|---|---|---|
| `githubPublic` | `false` | While false: no GitHub links in the nav, footer or calls to action, no "Edit on GitHub" on docs pages, and doc links to repository files that are not on the site (for example `CLOUD.md`, `SECURITY.md`, the Helm chart) show as plain text. When true they link to `repo` on `repoBranch`. The install snippet shows the clone command either way. |
| `signup.buttondownUser` | `''` | The release-email form posts to Buttondown's embed endpoint for this username. Empty: the section is left out of production builds; `npm run dev` shows it disabled with a "Not configured" note. |
| `contactEmail` | `'hello@softwarden.dev'` | Shown in the footer only when set (empty hides it). |
| `failOnUnresolvedDocLinks` | `true` | A doc link that resolves to nothing fails the build. Set false to print the list and continue. |
| `url`, `version`, `repo`, `repoBranch` | | Canonical origin, the version in the footer, the repository address and branch. |

## How the docs are pulled in

`src/content.config.ts` defines two content collections with Astro's `glob()` loader: `docs` reads the files listed in `src/lib/docs-manifest.ts` from `../docs`, and `changelog` reads `../CHANGELOG.md`. Nothing is copied: edit the markdown in `docs/` and rebuild.

- **Which docs**: the manifest lists each published file with its URL, sidebar group (Get started, Reference, Operations), sidebar label and description. To publish another doc, add it there. The manifest's header comment says which docs are left out and why.
- **Titles**: the files have no frontmatter; the page title is the file's first `# ` heading, which is removed from the body.
- **Links** (`src/lib/rehype-docs.ts`, `src/lib/doc-links.ts`): relative links to published docs become `/docs/<slug>/` (anchors kept and checked against the target's headings), `CHANGELOG.md` becomes `/changelog/`, other repository files become GitHub links or plain text (`githubPublic`). Placeholders such as `<your tenant>` that markdown would swallow as HTML are shown as text. Tables are wrapped in a labelled region that scrolls on its own.
- **Link check**: every build reads the published markdown directly and resolves every link (`astro.config.ts`, `docLinkCheck`). It prints how many links point at repository files outside the site and fails on any it cannot resolve.

## The dependency map

`src/components/DepMap.astro` renders the Contoso dependency scenario (data in `src/data/graph.ts`, from the demo seed) as a static SVG, with the list of records and the "If X changes" panel as the complete text equivalent. `src/scripts/depmap.ts` handles selection. When the section nears the viewport it loads `src/scripts/depmap-three.ts` and three.js through a dynamic import, and only if the visitor has not asked for reduced motion, the visible map is at least 600 px wide and WebGL works. Otherwise, or if loading fails, the SVG stays. Colours come from the CSS tokens and are re-read on every theme change.

## Design tokens

`src/styles/tokens.css` holds the light and dark values taken from the app's `apps/web/src/design/tokens.json`, under the same names. The theme follows the system, and the header's System / Light / Dark control stores a choice in `localStorage` (`softwarden.theme`, the app's key); an inline script sets `data-theme` before first paint. Inter is self-hosted through `@fontsource-variable/inter`. The site makes no requests to other hosts, sets no cookies and has no analytics.

## Open Graph image

`public/og.png` is rendered from `og/og-image.html`. After editing it, run `node og/make-og.cjs` from `site/`; the script borrows Playwright from `../apps/web/node_modules` (run `npm install` in `apps/web` first).

## Checks

`.github/workflows/site.yml` runs `npm ci` and `npm run build` on every push and pull request that changes `site/`, `docs/` or `CHANGELOG.md`, so a doc link the site cannot resolve fails there. The app's CI (`ci.yml`) skips changes that touch only `site/`.

## Deploying

The site is plain static files in `dist/`, so any static host works. On Vercel:

| Setting | Value |
|---|---|
| Root directory | `site` |
| Framework preset | Astro |
| Build command | `npm run build` |
| Output directory | `dist` |
| Install command | `npm install` (default) |
| Node.js version | 22.x or later |

The build reads `../docs` and `../CHANGELOG.md`, so the whole repository must be checked out (Vercel does this by default; keep "Include files outside the root directory in the Build Step" on).
