"""First-run setup from the host (core/bootstrap.py). No default credentials
ship.

    bootstrap_admin             print the one-time setup token and the address to enter it at; the first time, also
                                the credential encryption key with backup instructions (afterwards only its key id)
    bootstrap_admin --prepare   at container start: write a token while setup is open, print nothing secret
    bootstrap_admin --create    create the first organization and Admin here instead (prompts; or --no-input with
                                the options below and BOOTSTRAP_ADMIN_PASSWORD; --email and --name fall back to
                                BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_NAME for unattended runs such as a Helm Job)

In cloud mode (SOFTWARDEN_DEPLOYMENT=cloud, docs/CLOUD.md) there is no web setup and no token: `--create` makes the
operator's own platform staff account (email, name, password; no organization, no timezone), and customers create
organizations by signing up.
"""

import getpass
import os

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from catalog.admin_services import bootstrap_admin, bootstrap_platform_staff
from core import bootstrap, credentials
from core.models import BootstrapState, PlatformStaff
from core.problems import Conflict, FieldErrors
from softwarden.env import read_secret_file, secrets_dir

PASSWORD_ENV = "BOOTSTRAP_ADMIN_PASSWORD"
EMAIL_ENV = "BOOTSTRAP_ADMIN_EMAIL"
NAME_ENV = "BOOTSTRAP_ADMIN_NAME"
# --no-input: where each value is read from, named in the error when one is missing or invalid. An option given on the
# command line wins over its environment variable.
ENV_FALLBACKS = {"email": EMAIL_ENV, "name": NAME_ENV}
SOURCES = {
    "org_name": "--org-name",
    "timezone": "--timezone",
    "email": f"--email or {EMAIL_ENV}",
    "name": f"--name or {NAME_ENV}",
    "password": PASSWORD_ENV,
}
FIELDS = [
    ("org_name", "Organization name"),
    ("timezone", "Timezone (e.g. America/New_York)"),
    ("email", "Admin email"),
    ("name", "Admin display name"),
]
# Cloud mode: the operator's platform staff account, in no organization.
STAFF_FIELDS = [
    ("email", "Platform staff email"),
    ("name", "Display name"),
]


def _unattended(data: dict) -> None:
    """--no-input: the password from its variable; email and name from theirs where no option gave them."""
    for key, env in ENV_FALLBACKS.items():
        if not (data.get(key) or "").strip():
            data[key] = os.environ.get(env, "")
    data["password"] = os.environ.get(PASSWORD_ENV, "")


def _not_created(e: FieldErrors, *, unattended: bool) -> CommandError:
    """Each problem; unattended, also where its value is read from, so an operator knows what to set."""
    lines = []
    for key, msgs in e.errors.items():
        source = f" (set {SOURCES[key]})" if unattended and key in SOURCES else ""
        lines.append(f"  {key}: {' '.join(msgs)}{source}")
    return CommandError("Nothing was created. Fix these:\n" + "\n".join(lines))


class Command(BaseCommand):
    help = (
        "Show the one-time first-run setup token (valid one hour; closed once any organization or account exists) "
        "and, the first time, the credential encryption key to back up. --prepare writes the token at start-up "
        "without printing it; --create makes the first organization and Admin from this terminal instead."
    )

    def add_arguments(self, parser):
        parser.add_argument("--prepare", action="store_true", help="Prepare the token at start-up; print no secret.")
        parser.add_argument(
            "--create", action="store_true", help="Create the first organization and Admin from this terminal."
        )
        parser.add_argument("--org-name", dest="org_name")
        parser.add_argument("--timezone")
        parser.add_argument("--email")
        parser.add_argument("--name")
        parser.add_argument(
            "--no-input",
            action="store_false",
            dest="interactive",
            help=(
                f"With --create: don't prompt. The password comes from {PASSWORD_ENV}; --email and --name fall back "
                f"to {EMAIL_ENV} and {NAME_ENV}."
            ),
        )

    def handle(self, *args, **options):
        creating = options["create"] or not options["interactive"] or any(options.get(k) for k, _ in FIELDS)
        if creating:
            return self._create(options)
        if BootstrapState._meta.db_table not in connection.introspection.table_names():
            self.stdout.write("Initial setup: run `manage.py migrate` first, then `manage.py bootstrap_admin`.")
            return
        try:
            token = bootstrap.prepare_token()
        except OSError:
            raise CommandError(
                f"Can't write the private setup token file in {bootstrap.token_path().parent}. Check that the "
                "directory exists and belongs to the app user."
            ) from None
        if settings.SOFTWARDEN_CLOUD:
            self._cloud_status(prepare=options["prepare"])
            if not options["prepare"]:
                self._credential_key()
            return
        if options["prepare"]:
            if token:
                self.stdout.write("Initial setup is open. Run `manage.py bootstrap_admin` on the host for its token.")
            else:
                self.stdout.write("Initial setup is closed.")
            return
        if token:
            self.stdout.write(
                f"Open {settings.SOFTWARDEN_BASE_URL}/setup and enter this one-time token within one hour:"
            )
            self.stdout.write(token)
            self.stdout.write(
                "Or create the first organization and Admin from this terminal: "
                "`docker compose exec -it app python manage.py bootstrap_admin --create`."
            )
        else:
            self.stdout.write(
                f"Initial setup is closed: sign in at {settings.SOFTWARDEN_BASE_URL}. To recover an Admin account, "
                "use `manage.py reset_admin <email>`."
            )
        self._credential_key()

    def _cloud_status(self, prepare):
        if not PlatformStaff.objects.exists():  # the first staff account, even after customers signed up
            command = "python manage.py bootstrap_admin --create"
            self.stdout.write(
                "Hosted (cloud) installation: no web setup. Create the operator's platform staff account on the "
                f"server with `{command}`; customers sign up at {settings.SOFTWARDEN_BASE_URL}/signup."
                if not prepare
                else f"Hosted (cloud) installation: create the first platform staff account with `{command}`."
            )
        else:
            self.stdout.write(
                "Initial setup is closed (platform staff exists)."
                if prepare
                else f"Initial setup is closed (platform staff exists): sign in at {settings.SOFTWARDEN_BASE_URL}. To make another account "
                "platform staff, use `manage.py platform_staff grant <email>`."
            )

    # ---- the credential key, shown once -----------------------------------------------------------------------

    def _credential_key(self):
        state = credentials.status()
        again = f"docker compose exec app cat {(secrets_dir() / credentials.GENERATED_FILE).as_posix()}"
        self.stdout.write("")
        if not state["available"]:
            self.stdout.write(self.style.WARNING(f"Credential encryption key: {state['error']}"))
            return
        if state["source"] == "development":
            self.stdout.write("Credential encryption key: the development key (DEBUG only); nothing to back up.")
            return
        if state["source"] != "generated":
            self.stdout.write(
                f"Credential encryption key: id {state['primary']}, from {state['source']}. Keep your copy of it "
                "backed up off this server."
            )
            return
        if bootstrap.key_already_shown():
            self.stdout.write(
                f"Credential encryption key: id {state['primary']}. It was shown once already; to see it again run "
                f"`{again}`."
            )
            return
        self.stdout.write(self.style.WARNING("Credential encryption key (shown this once). Back it up now:"))
        self.stdout.write(read_secret_file(credentials.GENERATED_FILE))
        self.stdout.write(
            f"Key id {state['primary']}. Keep a copy off this server, for example in your password manager. Database "
            "backups don't include it, and without it no secret saved in Softwarden (connector client secrets) can "
            f"be read after a restore. `docker compose down -v` deletes it. To see it again: `{again}`."
        )
        try:
            bootstrap.mark_key_shown()
        except OSError:
            self.stderr.write("Couldn't record that the key was shown; it will be shown again next time.")

    # ---- creating the first Admin here ------------------------------------------------------------------------

    def _create(self, options):
        if settings.SOFTWARDEN_CLOUD:
            return self._create_staff(options)
        data = {key: options.get(key) for key, _ in FIELDS}
        if options["interactive"]:
            for key, label in FIELDS:
                while not (data[key] or "").strip():
                    data[key] = input(f"{label}: ")
            data["password"] = self._prompt_password()
        else:
            _unattended(data)
        try:
            membership = bootstrap_admin(data)
        except Conflict as e:
            raise CommandError(str(e.detail)) from e
        except FieldErrors as e:
            raise _not_created(e, unattended=not options["interactive"]) from e
        bootstrap.discard_token()
        self.stdout.write(
            self.style.SUCCESS(f"Created {membership.organization.name} with Admin {membership.user.email}.")
        )

    def _create_staff(self, options):
        """Cloud mode: the first platform staff account (catalog/admin_services.bootstrap_platform_staff)."""
        if options.get("org_name") or options.get("timezone"):
            raise CommandError(
                "Nothing was created: a hosted (cloud) installation's first account is platform staff in no "
                "organization, so --org-name and --timezone don't apply. Customers create organizations by signing up."
            )
        data = {key: options.get(key) for key, _ in STAFF_FIELDS}
        if options["interactive"]:
            for key, label in STAFF_FIELDS:
                while not (data[key] or "").strip():
                    data[key] = input(f"{label}: ")
            data["password"] = self._prompt_password()
        else:
            _unattended(data)
        try:
            user = bootstrap_platform_staff(data)
        except Conflict as e:
            raise CommandError(str(e.detail)) from e
        except FieldErrors as e:
            raise _not_created(e, unattended=not options["interactive"]) from e
        bootstrap.discard_token()
        self.stdout.write(
            self.style.SUCCESS(
                f"Created platform staff account {user.email} (no organization). Sign in at "
                f"{settings.SOFTWARDEN_BASE_URL} for the platform console."
            )
        )

    def _prompt_password(self):
        """Ask until the password passes the validators (12+ characters, not common) and is typed the same twice."""
        while True:
            password = getpass.getpass("Password (12+ characters): ")
            try:
                validate_password(password)
            except ValidationError as e:
                self.stderr.write(" ".join(e.messages))
                continue
            if getpass.getpass("Password again: ") != password:
                self.stderr.write("The passwords don't match.")
                continue
            return password
