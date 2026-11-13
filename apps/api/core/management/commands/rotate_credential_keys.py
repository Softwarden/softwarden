"""Re-encrypt every stored secret with the primary credential key (core/credentials.py)."""

from django.core.management.base import BaseCommand, CommandError

from core import credentials


class Command(BaseCommand):
    help = (
        "Re-encrypt every stored secret with the primary key (the first in SOFTWARDEN_CREDENTIAL_KEYS, or in the "
        "keys file). Run after putting a new key first; remove old keys only once no backup you may restore still "
        "needs them. A secret no configured key opens is skipped and named (model, row, organization, slot) with the "
        "page where it is entered again; the rest are re-encrypted. Prints counts, ids and key ids, never a value."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="Decrypt and report what would change; change nothing."
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit non-zero when any stored secret can't be read (the readable ones are still re-encrypted).",
        )

    def handle(self, *args, dry_run=False, strict=False, **options):
        try:
            result = credentials.rotate_all(dry_run=dry_run)
        except credentials.CredentialError as e:
            raise CommandError(f"{e} Nothing was changed.") from None
        verb = "Would re-encrypt" if dry_run else "Re-encrypted"
        self.stdout.write(f"{verb} {result.count} secret(s) with the primary key {result.primary}.")
        if not result.unreadable:
            return
        self.stdout.write(
            f"{len(result.unreadable)} can't be read with any configured key and "
            f"{'would be' if dry_run else 'was'} left as it is. Put the key that encrypted it back in the ring and run "
            "this again, or enter the secret again:"
        )
        for slot in result.unreadable:
            self.stdout.write(
                f"  {slot.model} {slot.row_id} (organization {slot.organization_id}) slot {slot.slot}: "
                f"{slot.reenter_at}"
            )
        if strict:
            raise CommandError(f"{len(result.unreadable)} stored secret(s) can't be read.")
