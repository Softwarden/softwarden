"""Write an organization's catalog as YAML files, for committing to a git repository (docs/CATALOG_AS_CODE.md).

    manage.py catalog_export --org <slug> --dir <path> [--include-people]

YAML files left in the kind folders by an earlier export are removed first, so a record deleted since disappears from
the tree too; other files (a README, .git) are left alone. The export is audited (catalog_code.exported)."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from catalog_code import bundle, engine
from catalog_code.kinds import FOLDERS
from core.capabilities import CATALOG_CODE, has_capability
from core.models import Organization
from core.request_context import unit_of_work


class Command(BaseCommand):
    help = "Write an organization's catalog as YAML files for a git repository."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="The organization's slug.")
        parser.add_argument("--dir", required=True, help="The folder to write into (created if missing).")
        parser.add_argument(
            "--include-people",
            action="store_true",
            help="Also write People files (names, emails, titles) and name owners by email. Personal data: only "
            "commit it to a repository whose readers may see it.",
        )

    def handle(self, *args, org, dir, include_people=False, **options):
        organization = Organization.objects.filter(slug=org).first()
        if organization is None:
            raise CommandError(f"No organization has the slug {org}.")
        if not has_capability(organization, CATALOG_CODE):
            raise CommandError(f"{organization.name}'s edition doesn't include catalog-as-code ({CATALOG_CODE}).")
        with unit_of_work():
            files = engine.export(organization, None, include_people=include_people, interface="cli")
        try:
            written = bundle.write_dir(Path(dir), files, FOLDERS)
        except bundle.BundleRejected as e:
            raise CommandError(e.message) from None
        self.stdout.write(self.style.SUCCESS(f"Wrote {written} files to {dir}."))
