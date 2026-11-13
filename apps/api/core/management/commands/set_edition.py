"""Set an organization's edition. Operator only: no tenant API changes the edition (platform staff can, from the
platform admin console, catalog/platform_admin.change_edition)."""

from django.core.management.base import BaseCommand, CommandError

from core import capabilities
from core.models import Edition, Organization
from core.problems import FieldErrors


class Command(BaseCommand):
    help = (
        "Set an organization's edition (community, cloud or enterprise) by its slug. The change is written to that "
        "organization's audit log. With --list, show every organization's slug and edition and change nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="The organization's slug (see --list).")
        parser.add_argument("new_edition", nargs="?", metavar="edition", choices=Edition.values)
        parser.add_argument(
            "--list", dest="list_orgs", action="store_true", help="List organizations with their slug and edition."
        )

    def handle(self, *args, slug=None, new_edition=None, list_orgs=False, **options):
        if list_orgs:
            for row in Organization.objects.order_by("slug"):
                self.stdout.write(f"{row.slug}\t{row.edition}\t{row.name}")
            return
        if not slug or not new_edition:
            raise CommandError("Give an organization slug and an edition, or --list.")
        found = Organization.objects.filter(slug=slug).first()
        if found is None:
            raise CommandError(f"No organization has the slug {slug!r}. Run with --list to see them.")
        try:
            org, changed = capabilities.set_edition(found, new_edition)
        except FieldErrors as e:
            raise CommandError(str(e.errors)) from None
        if changed:
            self.stdout.write(f"{org.name} ({org.slug}) is now {org.edition}.")
        else:
            self.stdout.write(f"{org.name} ({org.slug}) was already {org.edition}; nothing changed.")
