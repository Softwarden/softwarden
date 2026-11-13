"""Plan or apply a catalog-as-code tree (docs/CATALOG_AS_CODE.md).

    manage.py catalog_apply --org <slug> --dir <path> [--dry-run] [--prune [--allow-mass-delete]]
        [--expect-hash <hash> | --no-plan-check] [--json]

--dry-run prints the plan and writes nothing. Without it the plan is applied in one transaction, through the same
service functions as the app (each record's audit event says `via: catalog_code`), and only when no file has an error.
--expect-hash refuses to apply unless the catalog and the files are still what an earlier dry run planned (its
plan_hash): use it in CI to apply exactly what a pull request reviewed. Applying needs it, or an explicit
--no-plan-check to plan and apply in one go. A prune that deletes more than 10 records, more than 20% of a kind, or all
of a kind, is refused unless --allow-mass-delete. Exits non-zero when anything is refused."""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException

from catalog_code import bundle, engine
from core.capabilities import CATALOG_CODE, has_capability
from core.models import Organization
from core.request_context import unit_of_work


def _value(value) -> str:
    return "(none)" if value is None else json.dumps(value, ensure_ascii=False)


class Command(BaseCommand):
    help = "Plan or apply a catalog-as-code tree of YAML files."

    def add_arguments(self, parser):
        parser.add_argument("--org", required=True, help="The organization's slug.")
        parser.add_argument("--dir", required=True, help="The tree's folder (catalog_export writes one).")
        parser.add_argument("--dry-run", action="store_true", help="Print the plan; write nothing.")
        parser.add_argument(
            "--prune", action="store_true", help="Delete records of the tree's kinds that have no file in it."
        )
        parser.add_argument(
            "--allow-mass-delete",
            action="store_true",
            help="With --prune: allow deleting more than 10 records, more than 20%% of a kind, or all of a kind.",
        )
        parser.add_argument("--expect-hash", default=None, help="Apply only if the plan_hash is still this.")
        parser.add_argument("--json", action="store_true", dest="as_json", help="Print the plan as JSON.")
        parser.add_argument(
            "--no-plan-check",
            action="store_true",
            help="Apply without --expect-hash: plan and apply in one go, with no reviewed plan to compare.",
        )

    def handle(
        self,
        *args,
        org,
        dir,
        dry_run=False,
        prune=False,
        expect_hash=None,
        as_json=False,
        no_plan_check=False,
        allow_mass_delete=False,
        **options,
    ):
        if not dry_run and not expect_hash and not no_plan_check:
            raise CommandError(
                "Give --expect-hash <plan_hash> from the dry run you reviewed, or --no-plan-check to apply without one."
            )
        organization = Organization.objects.filter(slug=org).first()
        if organization is None:
            raise CommandError(f"No organization has the slug {org}.")
        if not has_capability(organization, CATALOG_CODE):
            raise CommandError(f"{organization.name}'s edition doesn't include catalog-as-code ({CATALOG_CODE}).")
        try:
            files = bundle.read_dir(Path(dir))
        except bundle.BundleRejected as e:
            raise CommandError(e.message) from None
        try:
            with unit_of_work():
                result = engine.execute(
                    organization,
                    None,
                    files,
                    prune=prune,
                    apply=not dry_run,
                    expected_hash=expect_hash,
                    interface="cli",
                    allow_mass_delete=allow_mass_delete,
                )
        except APIException as e:
            raise CommandError(str(e.detail)) from None
        if as_json:
            self.stdout.write(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            self.print_plan(result)
        if result["errors"]:
            raise CommandError(f"{len(result['errors'])} problems; nothing was applied.")
        if not dry_run:
            self.stdout.write(self.style.SUCCESS("Applied."))

    def print_plan(self, result):
        for change in result["changes"]:
            where = f"  ({change['file']})" if change["file"] else ""
            self.stdout.write(f"{change['action']:<7} {change['kind']} {change['key']}{where}")
            for item in change["fields"]:
                self.stdout.write(f"          {item['path']}: {_value(item['before'])} -> {_value(item['after'])}")
        for item in result["warnings"]:
            self.stdout.write(self.style.WARNING(f"warning {item['file']}:{item['line']} {item['message']}"))
        for item in result["errors"]:
            where = f"{item['file']}:{item['line']}" if item["file"] else "(catalog)"
            path = f" {item['path']}" if item["path"] else ""
            self.stderr.write(f"error   {where}{path}: {item['message']}")
        s = result["summary"]
        self.stdout.write(
            f"{s['create']} to create, {s['update']} to update, {s['delete']} to delete, {s['unchanged']} unchanged, "
            f"{s['errors']} errors, from {s['files']} files."
        )
        self.stdout.write(f"Plan hash: {result['plan_hash']}")
