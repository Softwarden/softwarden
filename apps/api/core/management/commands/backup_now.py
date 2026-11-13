"""Take a backup now (core/backup.py).

  manage.py backup_now                queue the backup job (or join the one queued or running) and wait for the worker
  manage.py backup_now --inline       run it in this process, without the worker (the same lock, so never two at once)
  manage.py backup_now --no-wait      queue it and return

Before an upgrade: `docker compose exec worker python manage.py backup_now --inline`. Exit status 1 when the backup
fails or the wait times out. Prints the set's name, size and duration; never a secret. A run refuses a destination that
isn't a mounted volume (the app container has no backup volume: run it in the worker), unless
SOFTWARDEN_BACKUP_REQUIRE_MOUNT=0 (development and tests)."""

from django.core.management.base import BaseCommand, CommandError

from core import backup, jobs
from core.models import JobStatus
from core.worker import heartbeat_age


class Command(BaseCommand):
    help = "Take an encrypted backup set now (the worker's job, or --inline in this process)."

    def add_arguments(self, parser):
        parser.add_argument("--inline", action="store_true", help="Run the backup in this process, not the worker.")
        parser.add_argument("--no-wait", action="store_true", help="Queue the job and return at once.")
        parser.add_argument(
            "--timeout", type=int, default=7200, help="Seconds to wait for the worker's job (default 7200)."
        )

    def handle(self, *args, inline=False, no_wait=False, timeout=7200, **options):
        dest = backup.destination()
        if dest is None:
            raise CommandError("Backups aren't configured: set SOFTWARDEN_BACKUP_DIR (the image sets /data/backups).")
        if inline:
            try:  # a destination that isn't a mount is refused here, before a run: not recorded as a failed backup
                backup.check_mounted(dest)
            except backup.BackupError as error:
                raise CommandError(f"Backup refused: {jobs.safe_error(error)}") from None
            try:
                result = backup.run_backup()
            except Exception as error:  # every failure, redacted: an error may echo a configured secret
                raise CommandError(f"Backup failed: {jobs.safe_error(error)}") from None
            self.report(result)
            return
        job, created = backup.request_backup("backup_now")
        self.stdout.write(f"{'Queued' if created else 'Joined'} backup job {job.id}.")
        if no_wait:
            return
        age = heartbeat_age()
        if age is None or age > 180:
            self.stderr.write("The worker isn't running, so the job won't start; use --inline or start the worker.")
        job = backup.wait_for(job, timeout)
        if job.status == JobStatus.SUCCEEDED:
            self.report(job.result)
            return
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            raise CommandError(f"The backup job is still {job.status} after {timeout} s; see the System page.")
        raise CommandError(f"Backup {job.status}: {job.last_error}")

    def report(self, result):
        size_mb = (result.get("size_bytes") or 0) / (1024 * 1024)
        self.stdout.write(
            self.style.SUCCESS(
                f"Backup set {result.get('set')} published: {size_mb:.1f} MB in {result.get('duration_seconds')} s, "
                f"{result.get('media_files')} attachment files"
                + (", also kept as this week's" if result.get("weekly") else "")
                + f"; {result.get('pruned')} old sets pruned."
            )
        )
