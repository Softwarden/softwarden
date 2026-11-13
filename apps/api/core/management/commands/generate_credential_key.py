"""Print a new credential encryption key (core/credentials.py). Nothing is saved."""

from django.core.management.base import BaseCommand

from core import credentials


class Command(BaseCommand):
    help = (
        "Print a new credential encryption key (Fernet) on stdout and its key id on stderr. Put it FIRST in "
        "SOFTWARDEN_CREDENTIAL_KEYS (or the keys file) to make it the primary key, then run rotate_credential_keys. "
        "Back it up apart from database backups. Nothing is saved."
    )

    def handle(self, *args, **options):
        key = credentials.generate_key()
        self.stdout.write(key)
        self.stderr.write(f"Key id: {credentials.key_id(key)}. Put it first in the keyring to make it primary.")
