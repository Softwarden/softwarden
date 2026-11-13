"""Clear one account's failed sign-in count (core/signin_limits.py).

Failed sign-ins are counted per email from any address, and after five each attempt must wait longer (at most five
minutes). Anyone who knows an email can make its owner wait; this is the operator's way out for one account, audited
in the platform stream (`signin.failures_forgotten`, no actor, via cli, with the reason). It never clears every
counter, and it doesn't touch the per-address limits."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "`forget <email> --reason ...` clears one account's failed sign-in count, so its owner can sign in at once. "
        "Check who is asking first. Audited in the platform stream."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["forget"])
        parser.add_argument("email", help="The email the sign-ins were for.")
        parser.add_argument("--reason", default="", help="Why, for the platform audit stream.")

    def handle(self, *args, action=None, email="", reason="", **options):
        from catalog import platform_admin  # catalog depends on core
        from core import signin_limits

        reason = (reason or "").strip()
        if not reason:
            raise CommandError("Say why with --reason (for example: owner locked out, identity checked by a call).")
        email = (email or "").strip().lower()
        if not email:
            raise CommandError("Give the email: signin_limits forget <email> --reason ...")
        user = platform_admin.find_user(email)
        with transaction.atomic():
            removed = signin_limits.forget_account(email)
            # The email itself is recorded only when it belongs to an account (unknown emails leave no trace).
            details = {
                "user": user.pk if user else None,
                "via": "cli",
                "reason": reason[:500],
                "had_failures": bool(removed),
            }
            if user is not None:
                details["email"] = user.email
            platform_admin.platform_audit(None, "signin.failures_forgotten", details=details)
        self.stdout.write(
            f"Cleared the failed sign-in count for {email}." if removed else f"{email} had no failed sign-ins counted."
        )
