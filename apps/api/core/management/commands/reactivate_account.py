"""Turn back on a deactivated account that belongs to no organization: SCIM
leaves such an account unlinked, and reactivating it is the operator's decision on the host. Audited in each
organization whose log names the account (catalog/admin_services.reactivate_account)."""

import logging

from django.core.management.base import BaseCommand, CommandError

from catalog.admin_services import reactivate_account
from core.problems import FieldErrors

log = logging.getLogger("softwarden.operator")


class Command(BaseCommand):
    help = (
        "Reactivate a deactivated account that has no memberships. Refuses an account that belongs to an organization "
        "(an Admin there reactivates it) or is already active."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, **options):
        try:
            user, orgs = reactivate_account(options["email"])
        except FieldErrors as e:
            raise CommandError(" ".join(e.errors["email"])) from e
        log.info("account reactivated by the operator user=%s organizations=%s", user.id, len(orgs))
        self.stdout.write(f"Reactivated account {user.id}. It belongs to no organization yet: invite it to one.")
        if orgs:
            self.stdout.write(f"Recorded in the audit log of: {', '.join(org.slug for org in orgs)}.")
        else:
            self.stdout.write("No organization's audit log names this account, so this output is the only record.")
