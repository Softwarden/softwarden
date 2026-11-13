"""Load endoflife.date data from a saved file: the offline path for an air-gapped install (eol/cache.py).

On a machine with internet access, save one of the API's documents:

  curl -fsSL -o eol-full.json https://endoflife.date/api/v1/products/full     every product with its release cycles
  curl -fsSL -o eol-products.json https://endoflife.date/api/v1/products     the product list only
  curl -fsSL -o postgresql.json https://endoflife.date/api/v1/products/postgresql   one product

then copy it to the server and load it:

  docker compose exec -T worker python manage.py eol_import - < eol-full.json
  manage.py eol_import eol-full.json

It replaces the cached releases of the products it carries and re-derives the cycles that depend on them. It works
whether or not SOFTWARDEN_EOL_ENABLED is on; with it on, the next daily refresh updates the data again."""

import json
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from eol import cache, client

MAX_BYTES = 64 * 1024 * 1024


class Command(BaseCommand):
    help = "Load endoflife.date data from a saved /api/v1/products/full, /api/v1/products or product document."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The JSON file, or - for standard input.")

    def handle(self, *args, path: str, **options):
        try:
            if path == "-":
                raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            else:
                with Path(path).open("rb") as handle:
                    raw = handle.read(MAX_BYTES + 1)
        except OSError as error:
            raise CommandError(f"Can't read {path}: {error.strerror or type(error).__name__}") from None
        if len(raw) > MAX_BYTES:
            raise CommandError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CommandError("The file isn't JSON.") from None
        try:
            counts = cache.import_document(document)
        except client.FetchError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(
            f"Imported {counts.get('products', 0)} products and {counts.get('releases', 0)} release cycles; "
            f"{counts.get('rederived', 0)} tracked versions changed cycle."
        )
