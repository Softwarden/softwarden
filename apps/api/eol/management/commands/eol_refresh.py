"""Refresh the endoflife.date cache now (eol/cache.py), in this process, without waiting for the daily job.

  manage.py eol_refresh                          the product list and every tracked product's release cycles
  manage.py eol_refresh --product postgresql     just these products' release cycles (repeat --product)

Refused with SOFTWARDEN_EOL_ENABLED=0 (use eol_import). Prints the counts; exit status 1 when nothing could be read."""

from django.core.management.base import BaseCommand, CommandError

from eol import cache


class Command(BaseCommand):
    help = "Refresh the end-of-life data from endoflife.date now."

    def add_arguments(self, parser):
        parser.add_argument("--product", action="append", default=None, help="Only this product (repeatable).")

    def handle(self, *args, product=None, **options):
        if not cache.enabled():
            raise CommandError("End-of-life downloads are off (SOFTWARDEN_EOL_ENABLED=0). Load a file with eol_import.")
        try:
            counts = cache.refresh(product)
        except cache.RefreshFailed as error:
            raise CommandError(f"endoflife.date couldn't be read: {error}") from None
        self.stdout.write(", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "Nothing to refresh.")
