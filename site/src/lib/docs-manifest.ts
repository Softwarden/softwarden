/**
 * Which repository docs the site publishes, under which URL and sidebar group. The markdown files are read in place
 * from ../docs (and ../CHANGELOG.md); nothing under docs/ is copied or edited.
 *
 * Left out on purpose: CLOUD.md and 04-development/platform-admin-runbook.md (hosted mode is not supported for
 * production in 1.0, and the site does not describe a cloud edition), DEVELOPMENT.md (for contributors, read in the
 * repository) and TOUR.md (a walk through the demo data in the app).
 */
export type DocGroup = 'Get started' | 'Reference' | 'Operations';

export interface DocEntry {
  /** Path relative to the repository's docs/ folder. */
  file: string;
  /** URL segment under /docs/. */
  slug: string;
  group: DocGroup;
  /** Short label for the sidebar; the page title is the file's first H1. */
  nav: string;
  /** Meta description and docs index summary. */
  description: string;
  /** Page title to show in place of the file's first H1, when that H1 is worded for maintainers. */
  title?: string;
  /** Table columns (by header text) the site leaves out; rows left with no value are dropped too. */
  dropColumns?: string[];
  /** Table rows the site leaves out, by the start of their first cell (hidden preview features). */
  dropRows?: string[];
}

export const DOC_GROUPS: DocGroup[] = ['Get started', 'Reference', 'Operations'];

export const DOCS: DocEntry[] = [
  {
    file: 'INSTALL.md',
    slug: 'install',
    group: 'Get started',
    nav: 'Install',
    description:
      'Take a fresh Ubuntu Server 24.04 VM to a signed-in Admin with backups running, using Docker Compose.',
  },
  {
    file: 'FIRST_RUN.md',
    slug: 'first-run',
    group: 'Get started',
    nav: 'First run: Entra discovery',
    description:
      'Create the Entra app registration and the connection, then run the first discovery and fill the Discovery queue.',
  },
  {
    file: 'CAPABILITIES.md',
    dropColumns: ['Cloud', 'Gate in the API today'],
    // Preview-gated sources, and the rows that exist only in the hosted (cloud) edition, whose column is dropped.
    dropRows: ['discovery.okta', 'discovery.google', 'preview', 'billing', 'org.self_delete', 'org.tenant_backups'],
    slug: 'capabilities',
    group: 'Get started',
    nav: 'Editions and capabilities',
    description: 'Which features each edition includes, how the API gates them and how an operator sets the edition.',
  },
  {
    file: 'ENV.md',
    slug: 'env',
    group: 'Reference',
    nav: 'Environment settings',
    description: 'Every environment variable Softwarden reads, its default, which service reads it and whether it is a secret.',
  },
  {
    file: 'PERMISSIONS.md',
    slug: 'permissions',
    group: 'Reference',
    nav: 'Microsoft permissions',
    description:
      'Every Microsoft Graph, Azure, Dataverse and Exchange permission Softwarden requests, and which feature needs it.',
  },
  {
    file: 'API.md',
    slug: 'api',
    group: 'Reference',
    nav: 'REST API',
    description:
      'What every endpoint under /api/v1/ shares: authentication, tokens, pagination, errors, ETags, rate limits and webhooks.',
  },
  {
    file: 'CATALOG_AS_CODE.md',
    slug: 'catalog-as-code',
    group: 'Reference',
    nav: 'Catalog as code',
    description: 'Export the catalog as YAML files for a git repository, then plan and apply changes from pull requests.',
  },
  {
    file: 'MCP.md',
    slug: 'mcp',
    group: 'Reference',
    nav: 'MCP server',
    description: 'Let an AI assistant read the catalog through the REST API with your token, with opt-in guarded writes.',
  },
  {
    file: '04-development/backup-runbook.md',
    slug: 'backup',
    group: 'Operations',
    nav: 'Back up and restore',
    description: 'Nightly encrypted backups from the worker, the backup key, restores and the restore rehearsal.',
  },
  {
    file: '04-development/upgrade-runbook.md',
    title: 'Upgrade Softwarden',
    slug: 'upgrade',
    group: 'Operations',
    nav: 'Upgrade',
    description: 'Upgrade an installation: a fresh backup, a pinned image, the pre-check, forward-only migrations and checks.',
  },
  {
    file: '04-development/operations.md',
    slug: 'operations',
    group: 'Operations',
    nav: 'Logs, health and traces',
    description: 'JSON logs with request ids, /readyz for monitors, /healthz/ops for operators and optional traces.',
  },
  {
    file: '04-development/sso-recovery-runbook.md',
    slug: 'sso-recovery',
    group: 'Operations',
    nav: 'Single sign-on recovery',
    description: 'Recover from a broken single sign-on configuration from the host. Password sign-in is never removed.',
  },
];

export const docBySlug = (slug: string) => DOCS.find((d) => d.slug === slug);
export const docByFile = (file: string) => DOCS.find((d) => d.file === file);
export const docUrl = (d: DocEntry) => `/docs/${d.slug}/`;
