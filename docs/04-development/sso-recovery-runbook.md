# Recover from a broken single sign-on configuration

Use this runbook when single sign-on stops working and people can't get in. Password sign-in is never removed, and
everything here works from the host, with no need for the web interface.

These steps have been tested against simulated identity providers, not yet against a real tenant.

An organization signs in with its own OpenID Connect settings (Administration › Sign-in, or
`/api/v1/sso/settings`). A draft can't be activated until a test sign-in of the Admin's own account against it has
passed, so a typo in a draft never locks anyone out. What can still go wrong: the provider changes underneath (the
client secret expires, the app is deleted, the tenant or domain moves), or the only Admin's password account was never
used.

## 1. See the state (1 min)

```bash
docker compose exec app python manage.py sso_recovery
```

For each organization it prints how members sign in with SSO now (`active` with its provider, `legacy` for the
installation's `SOFTWARDEN_OIDC_*`, `disabled`, `off`, or `force_local`), the stored status, the draft's revision and
whether it is tested, and how many Admins can sign in with a password. No secret is printed.

## 2. Get people in now: break-glass (2 min)

Set `SOFTWARDEN_FORCE_LOCAL_LOGIN=1` in `.env` and restart:

```bash
docker compose up -d app
```

Every SSO route is then hidden and refused for every organization: the sign-in page shows the password form only,
`/api/v1/auth/oidc/start` and the OIDC callback go back to the sign-in page, and testing or activating sign-in
settings is refused (409 `/problems/sso-local-only`). Sign-ins already in progress are refused at the callback. Nothing in the
database changes, so removing the setting and restarting brings SSO back as it was.

## 3. Turn SSO off for good, or hand it back

```bash
docker compose exec app python manage.py sso_recovery --org <slug> --disable   # one organization
docker compose exec app python manage.py sso_recovery --all --disable          # every organization
docker compose exec app python manage.py sso_recovery --org <slug> --use-env   # back to SOFTWARDEN_OIDC_* (legacy)
```

- `--disable` sets the organization's status to `disabled`: members sign in with their passwords, and the legacy
  `SOFTWARDEN_OIDC_*` isn't used for it either. An Admin turns SSO on again by activating a tested draft.
- `--use-env` sets the status to `legacy`: the installation's `SOFTWARDEN_OIDC_*` Entra settings apply again, but only
  while the installation has exactly one organization. The command warns when they are incomplete or can't apply.
- Self-hosted, a sign-in domain belongs to the first organization that claims it. In cloud mode
  (`SOFTWARDEN_SSO_DOMAIN_VERIFICATION`) a claim is pending until the organization's DNS TXT record verifies it, so a
  squatter can't hold a domain; a dispute between two organizations that both prove the domain is the operator's. To
  move or free one (also in the platform console, Sign-in domains: `platform/sign-in-domains/move|release`):
  `sso_recovery --move-domain <domain> --org <slug>` gives it to that organization, and
  `sso_recovery --release-domain <domain>` frees it. Each is audited in every organization whose domains change
  (`sso_settings.updated` with `sign_in_domains` before and after, `via: cli`).
- Each change is audited in the organization it affects (`sso_settings.disabled` or `.legacy_restored`, no actor,
  `via: cli`). Organization slugs: `manage.py set_edition --list`.

Then remove `SOFTWARDEN_FORCE_LOCAL_LOGIN` and restart, so the other organizations get their SSO back.

## 4. No Admin can sign in with a password

`sso_recovery` warns "Admins who can sign in with a password: 0". Give one a temporary password from the host:

```bash
docker compose exec app python manage.py reset_admin <email>
```

They sign in with it, must change it, and can then fix the draft in Sign-in settings: save it, test it with their own
account, activate it.

## 5. Fix and re-activate

1. Correct the draft (`PATCH /api/v1/sso/settings`); every saved change bumps its revision and invalidates its test.
2. Test it: `POST /api/v1/sso/settings/test` with the draft's revision, in the browser, signing in as yourself. The
   result (passed, or the failing check's code and Softwarden's sentence) is recorded on that revision.
3. Activate it: `POST /api/v1/sso/settings/activate` with the same revision. Sign-ins in progress must start again.
