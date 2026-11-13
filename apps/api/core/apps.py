from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_save


def _close_setup(sender, instance, created, raw=False, **kwargs):
    """The first organization or user created by any path (setup page, CLI, seed, Entra) closes first-run setup."""
    if created and not raw:
        from . import bootstrap

        bootstrap.close()


class CoreConfig(AppConfig):
    name = "core"

    def ready(self):
        from . import backup, jobs
        from .models import Organization

        # The nightly backup first (02:30 by default, SOFTWARDEN_BACKUP_CRON), before the 03:20 attachments.sweep and
        # the 03:40 jobs.prune.
        backup.register()
        jobs.register_schedule("jobs.prune", jobs.prune, "40 3 * * *", "Delete finished jobs after 30 days")
        from . import sessions

        # Expired sessions and rotated-key tombstones (core/sessions.py); nothing else deletes them.
        jobs.register_schedule("sessions.clear", sessions.clear_expired, "50 3 * * *", "Delete expired sessions")
        from . import sso

        # Sign-in domain claims still pending after sso.PENDING_DAYS lapse.
        jobs.register_schedule(
            "sso.domains_lapse", sso.lapse_pending, "35 4 * * *", "Forget sign-in domain claims never verified"
        )
        from . import counters

        # The shared rate-limit counters (core/counters.py): every 10 minutes, the rows whose window ended.
        counters.register()
        from django.contrib.auth.signals import user_logged_in

        from .access import remember_authentication

        # Every sign-in path notes when the session authenticated (re-authentication by recency).
        user_logged_in.connect(remember_authentication, dispatch_uid="core.remember_authentication")
        post_save.connect(_close_setup, sender=Organization, dispatch_uid="core.close_setup.organization")
        post_save.connect(_close_setup, sender=settings.AUTH_USER_MODEL, dispatch_uid="core.close_setup.user")
