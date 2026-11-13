"""Run the attachment sweep now (the `attachments.sweep` schedule runs it nightly): delete stored files no attachment
references once past SOFTWARDEN_ATTACHMENT_RETENTION_DAYS. Safe to repeat."""

from django.core.management.base import BaseCommand

from catalog import attachments


class Command(BaseCommand):
    help = "Delete removed attachments' files after the retention window, and files nothing references."

    def handle(self, *args, **options):
        result = attachments.sweep()
        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {result['retired_removed']} retired and {result['orphans_removed']} orphaned files."
            )
        )
