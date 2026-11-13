"""Deprecated: the pre-keyring name of rotate_credential_keys, kept so existing runbooks still work."""

from django.core.management import call_command
from django.core.management.base import BaseCommand

from core import credentials


class Command(BaseCommand):
    help = "Deprecated: use rotate_credential_keys (and generate_credential_key instead of --new-key)."

    def add_arguments(self, parser):
        parser.add_argument("--new-key", action="store_true", help="Print a newly generated key and exit.")

    def handle(self, *args, new_key=False, **options):
        self.stderr.write("rotate_secrets is deprecated; use rotate_credential_keys and generate_credential_key.")
        if new_key:
            self.stdout.write(credentials.generate_key())
            return
        call_command("rotate_credential_keys", stdout=self.stdout, stderr=self.stderr)
