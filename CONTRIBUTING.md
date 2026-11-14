# Contributing to Softwarden

Thank you for helping. This page says how to propose a change, the rules every change follows, and how to sign off
your commits. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **A bug:** open an issue with the bug report template: the version (`/healthz/ops` reports it, or the image tag you run), what
  you did, what you expected and what happened. Leave out secrets, tenant ids and personal data.
- **A security problem:** don't open an issue. Follow [SECURITY.md](SECURITY.md).
- **A feature or a larger change:** open an issue first and describe the problem it solves, so we can agree on the
  approach before you write code. Small fixes (a typo, a broken link, a clear bug with a test) can go straight to a
  pull request.

## Set up

[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) explains how to run the API, the worker and the SPA from source with a
development PostgreSQL, load the demo data, and run the tests. You need Docker, [uv](https://docs.astral.sh/uv/)
(Python 3.13) and Node 22.

## Rules every change follows

These keep a multi-organization catalog safe. Review checks each one, and most have a test that fails without it.

- **Tenant isolation.** Every tenant table has a UUID id, `organization_id`, `UNIQUE (organization_id, id)` and
  composite foreign keys. A new model extends the cross-tenant tests.
- **Audited writes.** Every write goes through a service function that validates, scopes to the organization and
  writes an `AuditEvent` in the same transaction. Views never save domain models.
- **Roles.** Every endpoint declares its minimum role (Admin, Editor or Viewer); the URL coverage test fails otherwise.
- **Capabilities, not editions.** A feature is gated by a capability name from `capabilities(org)`, never by comparing
  an edition ([docs/CAPABILITIES.md](docs/CAPABILITIES.md)).
- **Secrets.** A secret entered in the UI is encrypted, write-only, and never appears in audit events, exports, logs,
  API responses or job arguments.
- **Discovery suggests.** Discovery never overwrites curated data; it queues observations for a person to apply.
- **Migrations.** Every model change ships with a migration. Migrations are forward-only, and an applied one is never
  edited.
- **Microsoft permissions.** A new Microsoft Graph or Azure call is checked against Microsoft Learn and recorded in
  [docs/PERMISSIONS.md](docs/PERMISSIONS.md).
- **Screens.** Follow the [UI rules](docs/DEVELOPMENT.md#ui-rules): design tokens only, status as a glyph plus text,
  loading, empty, error and read-only states on every screen, filters in the URL, WCAG 2.2 AA.
- **Spelling.** American English in the UI, API messages and docs; `npm run lint` checks it.
- **Dependencies.** A new dependency needs a reason in the pull request.
- **Docs.** Update the README, [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), [docs/API.md](docs/API.md),
  [docs/ENV.md](docs/ENV.md) and the runbooks in the same pull request as the change they describe.

A comment says the reason for a rule in words; it doesn't cite tracker or decision ids.

## Tests and the gate

A change comes with tests: models, services, permissions, endpoints, jobs and notifications each have theirs. Before
you open a pull request, run the gate for what you changed:

```bash
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy --no-incremental . \
  && SOFTWARDEN_DEBUG=1 uv run python manage.py makemigrations --check --dry-run && uv run pytest -q
cd apps/web && npm run lint && npm run build && npm run size && npm run e2e
python3 ops/ci/check-doc-links.py
```

The full pytest run takes a while; while you work, run the tests for the area you touched. CI runs everything on
every pull request.

## Sign your commits

Softwarden uses the [Developer Certificate of Origin](https://developercertificate.org/) (DCO) instead of a
contributor license agreement. By signing off a commit you certify that you wrote it, or otherwise have the right to
submit it, under the project's license:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Sign off with `git commit -s`, which adds a line with your name and email:

```text
Signed-off-by: Ada Example <ada@example.com>
```

The email must be the commit's author email. The `DCO sign-off` check in CI fails a pull request with a commit that
isn't signed off by its author; `bash ops/ci/check-dco.sh origin/main` runs the same check locally, and
`git rebase --signoff origin/main` adds the missing lines. Dependabot's version updates are the one exception: a bot
can't certify the DCO, so the check skips its commits.

## Pull requests

- One logical change per pull request, with a title that says what it does.
- Fill in the template: what changed and why, how you tested it, and what a reviewer should look at.
- Keep commits readable; we may squash them when merging.
- Screens: include a screenshot in light and dark, and say what you checked with a keyboard and a screen reader.

## The name

"Softwarden" is the name of this project. You may use it to refer to the project, to say that your software works
with Softwarden, and for unmodified builds of it. A fork, a modified build or a hosted service should use its own name,
and must not use "Softwarden", or a name or logo confusingly similar to it, in a way that suggests it is the project
itself or is endorsed by it. The Apache License 2.0 doesn't grant rights to the name (section 6). If you are unsure,
open an issue and ask.
