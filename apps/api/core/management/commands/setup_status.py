"""Setup facts as key=value lines for the setup scripts (scripts/setup.ps1, scripts/setup.sh). Reads no key and no
secret value. `manage.py doctor` (core/doctor.py, which owns these facts) gives the full picture.

    migrated=1           the database has the app's tables
    setup_pending=1      first-run setup is still open (no organization or account yet)
    encrypted_secrets=3  secrets stored encrypted (a count)
"""

from django.core.management.base import BaseCommand

from core.doctor import setup_facts


class Command(BaseCommand):
    help = "Print setup facts for the setup scripts (no secrets)."

    def handle(self, *args, **options):
        facts = setup_facts()
        self.stdout.write("\n".join(f"{key}={value}" for key, value in facts.items()))
