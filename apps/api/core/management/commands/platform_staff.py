"""Grant, revoke or list platform staff (platform admin console). The only way to change who is staff.
Also the recovery path for a staff member's lost second factor: `reset-second-factor <email> --reason ...`."""

from django.core.management.base import BaseCommand, CommandError

from core.models import PlatformStaff
from core.problems import FieldErrors


class Command(BaseCommand):
    help = (
        "Platform staff use the platform admin console (/api/v1/platform/): every organization, usage, capability "
        "overrides, suspension and impersonation. `grant <email>` and `revoke <email>` change one account and are "
        "written to the platform audit stream; `list` shows who is staff and whether each has set up a second "
        "factor. `enroll <email>` sets up a staff member's second factor from the host and prints its secret once. "
        "`reset-second-factor <email> --reason ...` clears a staff member's second factor (a lost or "
        "replaced device; check who is asking first): audited, and they set it up again at their next console "
        "sign-in. Staff status gives no role in any organization."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["grant", "revoke", "list", "enroll", "reset-second-factor"])
        parser.add_argument("email", nargs="?", help="The account's email (grant, revoke, reset-second-factor).")
        parser.add_argument(
            "--reason",
            default="",
            help="reset-second-factor: why, for the platform audit stream (for example: lost phone, identity checked "
            "by a call to a known number).",
        )

    def handle(self, *args, action=None, email=None, reason="", **options):
        from catalog import platform_admin, staff_second_factor  # catalog depends on core

        if action == "list":
            rows = PlatformStaff.objects.select_related("user").order_by("user__email")
            for row in rows:
                state = "active" if row.user.is_active else "deactivated"
                factor = "set up" if staff_second_factor.enrolled(row) else "not set up"
                self.stdout.write(
                    f"{row.user.email}\t{state}\tsince {row.granted_at.date().isoformat()}\tsecond factor: {factor}"
                )
            if not rows:
                self.stdout.write("No platform staff.")
            return
        if not email:
            raise CommandError(f"Give the account's email: platform_staff {action} <email>.")
        user = platform_admin.find_user(email)
        if user is None:
            raise CommandError(f"No account has the email {email!r}.")
        if action == "enroll":
            from rest_framework.exceptions import APIException

            try:
                enrollment = staff_second_factor.start_enrollment(user, via="cli")
            except APIException as e:
                raise CommandError(str(e.detail)) from None
            self.stdout.write(
                f"Second factor for {user.email}: add this to their authenticator app (shown once):\n"
                f"  {enrollment['otpauth_uri']}\n  secret: {enrollment['secret']}\n"
                "It is confirmed by the first code they send at the console (platform/second-factor/verify)."
            )
            return
        if action == "reset-second-factor":
            try:
                had = staff_second_factor.reset(user, reason)
            except FieldErrors as e:
                raise CommandError(
                    " ".join(m for messages in e.errors.values() for m in messages) + " Use --reason."
                    if "reason" in e.errors
                    else " ".join(m for messages in e.errors.values() for m in messages)
                ) from None
            self.stdout.write(
                f"{user.email}'s second factor was reset; they set it up again at their next console sign-in."
                if had
                else f"{user.email} had no second factor set up; nothing to reset (recorded anyway)."
            )
            return
        if action == "grant":
            try:
                changed = platform_admin.grant_staff(user)
            except FieldErrors as e:
                raise CommandError(" ".join(m for messages in e.errors.values() for m in messages)) from None
            self.stdout.write(f"{user.email} is {'now' if changed else 'already'} platform staff.")
        else:
            changed = platform_admin.revoke_staff(user)
            self.stdout.write(
                f"{user.email} is no longer platform staff." if changed else f"{user.email} wasn't platform staff."
            )
