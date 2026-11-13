"""Break-glass recovery for a locked-out Admin. Run on the host; there is no email-based reset."""

from django.core.management.base import BaseCommand, CommandError

from catalog.admin_services import break_glass_reset
from core.problems import FieldErrors


class Command(BaseCommand):
    help = (
        "Set a temporary password for an Admin, who must change it at next sign-in. Reactivates the account if needed."
    )

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument(
            "--any-member",
            action="store_true",
            help="Also reset someone who isn't an Admin (an account in several organizations, which no Admin can reset).",
        )

    def handle(self, *args, **options):
        try:
            membership, temp = break_glass_reset(options["email"], any_member=options["any_member"])
        except FieldErrors as e:
            raise CommandError(" ".join(e.errors["email"])) from e
        self.stdout.write(f"Temporary password for {membership.user.email}: {temp}")
        self.stdout.write("Give it to them over a separate channel. They must change it when they sign in.")
