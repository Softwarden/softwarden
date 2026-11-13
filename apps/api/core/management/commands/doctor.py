"""How this installation is set up, without secrets (core/doctor.py has the checks).

    manage.py doctor           one line per check: OK, WARN or FAIL, the finding, and a fix for anything not OK
    manage.py doctor --json    the same as one JSON document, for scripts

Exit status 1 when any check fails (a warning alone exits 0). On the Docker stack:
`docker compose exec app python manage.py doctor`.
"""

import json
import os
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from core import doctor

CONNECT_TIMEOUT_ENV = "PGCONNECT_TIMEOUT"  # libpq's own variable, read when a connection opens
LABEL = {doctor.OK: "OK", doctor.WARN: "WARN", doctor.FAIL: "FAIL"}


class Command(BaseCommand):
    help = "Check the installation's setup (database, worker, keys, backups, URL, email, SSO, discovery). No secrets."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print one JSON document instead of lines.")

    def handle(self, *args, **options):
        now = timezone.now()
        # A short connection timeout for libpq (doctor's own process only), so an unreachable database is reported
        # in seconds instead of hanging on the operating system's TCP timeout. An operator's own value wins.
        previous = os.environ.get(CONNECT_TIMEOUT_ENV)
        os.environ.setdefault(CONNECT_TIMEOUT_ENV, "5")
        try:
            checks = doctor.run(now)
        finally:
            if previous is None:
                os.environ.pop(CONNECT_TIMEOUT_ENV, None)
        status = doctor.overall(checks)
        if options["json"]:
            document = {
                "status": status,
                "checked_at": now.isoformat(),
                "counts": {s: sum(1 for c in checks if c.status == s) for s in (doctor.OK, doctor.WARN, doctor.FAIL)},
                "checks": [c.as_dict() for c in checks],
            }
            self.stdout.write(json.dumps(document, indent=2))
        else:
            self._lines(checks, now)
        if status == doctor.FAIL:
            sys.exit(1)

    def _lines(self, checks, now):
        width = min(36, max(len(self._name(c)) for c in checks))
        self.stdout.write(f"Softwarden doctor, {now:%Y-%m-%d %H:%M} UTC")
        style = {doctor.OK: self.style.SUCCESS, doctor.WARN: self.style.WARNING, doctor.FAIL: self.style.ERROR}
        for c in checks:
            label = style[c.status](f"{LABEL[c.status]:<4}")
            self.stdout.write(f"  {label}  {self._name(c):<{width}}  {c.message}")
            if c.fix:
                self.stdout.write(f"        {'':<{width}}  fix: {c.fix}")
        counts = {s: sum(1 for c in checks if c.status == s) for s in (doctor.OK, doctor.WARN, doctor.FAIL)}
        self.stdout.write(f"{counts['ok']} ok, {counts['warn']} warning(s), {counts['fail']} failure(s).")

    @staticmethod
    def _name(check):
        return f"[{check.organization}] {check.name}" if check.organization else check.name
