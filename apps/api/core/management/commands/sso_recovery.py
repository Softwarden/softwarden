"""Recover from a broken single sign-on configuration from the host, without the web UI (per
organization).

    manage.py sso_recovery                        # show how every organization signs in
    manage.py sso_recovery --org <slug> --disable # turn SSO off for one organization (password sign-in stays)
    manage.py sso_recovery --all --disable        # ... for every organization
    manage.py sso_recovery --org <slug> --use-env # hand it back to SOFTWARDEN_OIDC_* (legacy, single organization)
    manage.py sso_recovery --move-domain <domain> --org <slug>  # give a sign-in domain to this organization
    manage.py sso_recovery --release-domain <domain>            # free a sign-in domain

Every change is audited in the organization it affects (via: cli). For an immediate override that ignores every
organization's settings, set SOFTWARDEN_FORCE_LOCAL_LOGIN=1 and restart; to recover an Admin's password use
`manage.py reset_admin <email>`. No secret is ever printed."""

from django.core.management.base import BaseCommand, CommandError

from core import sso
from core.models import Organization


class Command(BaseCommand):
    help = "Show or turn off single sign-on per organization (password sign-in is never affected)."

    def add_arguments(self, parser):
        which = parser.add_mutually_exclusive_group()
        which.add_argument("--org", help="The organization's slug (see `set_edition --list`).")
        which.add_argument("--all", action="store_true", help="Every organization.")
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--disable", action="store_true", help="Turn SSO off.")
        action.add_argument("--use-env", action="store_true", help="Hand sign-in back to SOFTWARDEN_OIDC_*.")
        action.add_argument("--move-domain", metavar="DOMAIN", help="Give a sign-in domain to --org.")
        action.add_argument("--release-domain", metavar="DOMAIN", help="Free a sign-in domain.")

    def handle(self, *args, org=None, all=False, disable=False, use_env=False, **options):
        move, release = options.get("move_domain"), options.get("release_domain")
        if move or release:
            return self._domain(org, all, move, release)
        if (disable or use_env) and not (org or all):
            raise CommandError("Name the organization with --org <slug>, or use --all.")
        if (org or all) and not (disable or use_env):
            raise CommandError("Add --disable or --use-env, or run without arguments to see the state.")
        if use_env and all:
            raise CommandError("--use-env applies to one organization: the legacy settings serve a single one.")
        if org:
            orgs = list(Organization.objects.filter(slug=org))
            if not orgs:
                raise CommandError(f"No organization with the slug {org!r}. List them with `set_edition --list`.")
        else:
            orgs = list(Organization.objects.order_by("name"))
        if disable:
            for target in orgs:
                _, changed = sso.disable(target, None, via="cli")
                verb = "turned off" if changed else "already off"
                self.stdout.write(self.style.SUCCESS(f"{target.slug}: single sign-on {verb}."))
        elif use_env:
            target = orgs[0]
            _, changed = sso.use_env(target, None, via="cli")
            verb = "handed back to SOFTWARDEN_OIDC_*" if changed else "already uses SOFTWARDEN_OIDC_*"
            self.stdout.write(self.style.SUCCESS(f"{target.slug}: single sign-on {verb}."))
            if sso.legacy_env() is None:
                self.stdout.write(self.style.WARNING("SOFTWARDEN_OIDC_* isn't set completely: no SSO until it is."))
            elif sso.legacy_org() is None:
                self.stdout.write(
                    self.style.WARNING("SOFTWARDEN_OIDC_* serves an installation with one organization only.")
                )
        self._show(orgs if (org or all) else list(Organization.objects.order_by("name")))

    def _show(self, orgs):
        if sso.force_local():
            self.stdout.write(self.style.WARNING("SOFTWARDEN_FORCE_LOCAL_LOGIN is set: SSO is off everywhere."))
        legacy = "set" if sso.legacy_env() is not None else "not set"
        self.stdout.write(f"Legacy SOFTWARDEN_OIDC_*: {legacy}.")
        for target in orgs:
            row = sso.settings_of(target)
            state = sso.state(target)
            status = row.status if row is not None else "legacy"
            draft = ""
            if row is not None and row.draft_revision:
                tested = "tested" if row.draft_is_tested else "not tested"
                draft = f"; draft revision {row.draft_revision} ({row.draft_provider or 'no provider'}, {tested})"
            provider = f" {state['provider']}" if state["provider"] else ""
            self.stdout.write(f"{target.slug}: {state['source']}{provider} (status {status}){draft}. {state['detail']}")
            admins = sso.local_admins(target)
            line = f"  Admins who can sign in with a password: {admins}"
            if admins:
                self.stdout.write(line)
            else:
                self.stdout.write(self.style.WARNING(line + ". Recover one with `manage.py reset_admin <email>`."))

    def _domain(self, slug, all, move, release):
        """Move or free a sign-in domain; audited in each organization whose domains change."""
        from core.problems import FieldErrors

        if all or (move and not slug) or (release and slug):
            raise CommandError("Use --move-domain <domain> --org <slug>, or --release-domain <domain> alone.")
        try:
            if move:
                target = Organization.objects.filter(slug=slug).first()
                if target is None:
                    raise CommandError(f"No organization with the slug {slug!r}. List them with `set_edition --list`.")
                previous = sso.move_domain(move, target, None, via="cli")
                origin = f" from {previous.slug}" if previous is not None and previous.pk != target.pk else ""
                self.stdout.write(self.style.SUCCESS(f"{move.lower()}: now signs in to {target.slug}{origin}."))
            else:
                previous = sso.release_domain(release, None, via="cli")
                where = f" (was {previous.slug})" if previous is not None else " (no organization had it)"
                self.stdout.write(self.style.SUCCESS(f"{release.lower()}: released{where}."))
        except FieldErrors as e:
            raise CommandError(" ".join(e.errors["domain"])) from None
