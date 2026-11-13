"""Change the simulated Entra tenant for a demo: the next run of a connector on the `simulated` tenant sees it."""

from django.core.management.base import BaseCommand, CommandError

from core import simulation
from discovery import entra
from discovery.models import Connector

OPS = (
    "rename",
    "disable",
    "enable",
    "remove",
    "add",
    "add_registration",
    "owners",
    "secret",
    "remove_credentials",
    "tag",
    "reset",
)


class Command(BaseCommand):
    help = (
        "Edit the synthetic tenant behind connectors whose tenant ID is 'simulated'. Examples: "
        "rename 'Contract Vault' 'Contract Vault 2'; disable Zoom; add 'New Vendor Portal'; "
        "add_registration 'Build Pipeline'; owners Zoom dana.ortiz@example.test; secret Zoom 'Zoom API' 14; "
        "remove_credentials 'Payroll Sync Daemon'; tag 'Expense Hub' AgentCreatedBy:CopilotStudio; reset."
    )

    def add_arguments(self, parser):
        parser.add_argument("op", choices=OPS)
        parser.add_argument("target", nargs="?", help="The app's display name (or the new name for add).")
        parser.add_argument(
            "value",
            nargs="*",
            help="The new name for rename, owner emails for owners, a tag for tag, or a secret's name and days left.",
        )

    def handle(self, *args, op, target=None, value=(), **options):
        if not simulation.allowed():
            raise CommandError(simulation.command_refused("simulate_entra_change"))
        connectors = list(Connector.objects.filter(microsoft_connection__tenant_id=entra.SIMULATED))
        if not connectors:
            raise CommandError(
                "No connector uses the simulated tenant. Run seed_demo, or add one with tenant ID 'simulated'."
            )
        if op != "reset" and not target:
            raise CommandError("Name the app.")
        if op in ("rename", "tag") and len(value) != 1:
            raise CommandError("Give the new name." if op == "rename" else "Give the tag.")
        if op == "secret" and (len(value) != 2 or not value[1].lstrip("-").isdigit()):
            raise CommandError("Give the secret's name and the days until it expires.")
        for c in connectors:
            config = dict(c.config)
            if op == "reset":
                config.pop("simulator_changes", None)
            else:
                change = {"op": op, "id": target}
                if op in ("add", "add_registration"):
                    change["value"] = target
                elif op in ("rename", "tag"):
                    change["value"] = value[0]
                elif op == "owners":
                    change["value"] = list(value)
                elif op == "secret":
                    change["value"] = {"name": value[0], "days": int(value[1])}
                config["simulator_changes"] = [*config.get("simulator_changes", []), change]
            c.config = config
            c.save(update_fields=["config"])
        self.stdout.write(
            f"Changed the simulated tenant for {len(connectors)} connector(s). Run the connector to see it."
        )
