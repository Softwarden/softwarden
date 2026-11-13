"""Check every attachment's stored file: it exists and matches its recorded size and SHA-256.
Run after a restore (docs/04-development/backup-runbook.md; ops/restore.sh runs it) or at any time. Exits non-zero when a file fails."""

from django.core.management.base import BaseCommand, CommandError

from catalog import attachments
from core.models import Organization


class Command(BaseCommand):
    help = "Verify that attachment files exist and match their recorded size and SHA-256."

    def add_arguments(self, parser):
        parser.add_argument("--organization", help="Only this organization (its slug).")

    def handle(self, *args, organization=None, **options):
        org = None
        if organization:
            org = Organization.objects.filter(slug=organization).first()
            if org is None:
                raise CommandError(f"No organization has the slug {organization}.")
        checked, problems = attachments.verify(org)
        for line in problems:
            self.stderr.write(line)
        if problems:
            raise CommandError(f"{len(problems)} of {checked} attachment files failed verification.")
        self.stdout.write(self.style.SUCCESS(f"All {checked} attachment files are present and match."))
