"""Checks around an upgrade or a restore. See docs/04-development/upgrade-runbook.md.

  manage.py upgrade_check --write-manifest before.json   before the backup and the upgrade: what must survive
  manage.py upgrade_check --manifest before.json         after the upgrade (or a restore drill): the post-upgrade checks,
                                                         and nothing in the manifest lost or changed
  manage.py upgrade_check                                the post-upgrade checks alone
  manage.py upgrade_check --precheck                     with the new image, before it migrates: rows that would
                                                         stop a pending migration (cross-organization references)

The checks go through the real HTTP stack as each organization's first active Admin (a session made for the check,
then removed): sign-in options, the signed-in user, Needs attention, a record with reviews and a renewal with completion
evidence. Exit status 1 if anything fails."""

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import timezone

from catalog.models import KIND_PATHS, RecordReview, Resource
from core.local_apps import local_app_configs
from core.models import AuditEvent, Membership, Organization, Role
from core.worker import heartbeat_age
from money.models import Contract, RenewalCycle

SLOW_SECONDS = 2.0  # a page this slow right after an upgrade is worth a look, not a failure


def _digest(rows):
    h = hashlib.sha256()
    for row in rows:
        h.update(json.dumps(row, default=str, sort_keys=True).encode())
    return h.hexdigest()


def _rows(queryset, *fields):
    """[[id, field, ...], ...] as strings, sorted: the rows a manifest keeps by value."""
    return [[str(v) for v in row] for row in queryset.order_by("id").values_list("id", *fields)]


def snapshot(audit_cutoff=None):
    """What must survive: every review, every completed renewal cycle, and the audit log up to the cutoff, each
    as a count and a digest; and every record, contract and membership by key, with the
    columns an upgrade must not move (organization, a record's kind, a membership's user; not its role, which an Admin
    may change before the upgrade). Those are kept row by row, not as a digest,
    because the app keeps running between the manifest and the upgrade: a row added since is fine, a row lost or
    moved to another organization is not."""
    reviews = list(
        RecordReview.objects.order_by("id").values_list(
            "id", "resource_id", "confirmed_by_id", "confirmation_date", "next_review_date", "evidence"
        )
    )
    completions = list(
        RenewalCycle.objects.filter(completed_on__isnull=False)
        .order_by("id")
        .values_list("id", "obligation_id", "sequence", "decision", "completed_on", "completion_evidence")
    )
    audit = AuditEvent.objects.order_by("created_at", "id")
    if audit_cutoff is not None:
        audit = audit.filter(created_at__lte=audit_cutoff)
    events = list(audit.values_list("id", "action", "target_id", "created_at"))
    records = _rows(Resource.objects, "organization_id", "kind")
    contracts = _rows(Contract.objects, "organization_id")
    memberships = _rows(Membership.objects, "organization_id", "user_id")
    return {
        "reviews": {"count": len(reviews), "sha256": _digest(reviews)},
        "renewal_completions": {"count": len(completions), "sha256": _digest(completions)},
        "audit_events": {"count": len(events), "sha256": _digest(events)},
        "records": {"count": len(records), "rows": records},
        "contracts": {"count": len(contracts), "rows": contracts},
        "memberships": {"count": len(memberships), "rows": memberships},
    }


# What check_manifest compares, in order: digests must match; rows must all still be there, unchanged. A manifest
# written by an older release has only the first three; the rest are reported as not in it rather than failed.
MANIFEST_PARTS = (
    ("reviews", "Record reviews"),
    ("renewal_completions", "Renewal completions"),
    ("audit_events", "Audit events"),
    ("records", "Records"),
    ("contracts", "Contracts"),
    ("memberships", "Memberships"),
)


def migrated_apps():
    """Softwarden's own apps that have migrations, at any depth under the API directory (core/local_apps.py; a
    top-level-only scan missed an app under ee/). ee/saml and ee/scim have no tables of their own today."""
    return [c.label for c in local_app_configs() if (Path(c.path) / "migrations").is_dir()]


def latest_migrations():
    """The latest applied migration of each local app with migrations, e.g. {"catalog": "0019_split_apps", ...}."""
    executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
    applied = executor.loader.applied_migrations
    latest = {}
    for label in migrated_apps():
        names = sorted(name for app, name in applied if app == label)
        latest[label] = names[-1] if names else None
    return latest


def manifest_migrations(body):
    """The migrations recorded in a manifest. Before the catalog split a manifest held one string under
    "migration", the latest catalog migration; it is read as {"catalog": that}."""
    if isinstance(body.get("migrations"), dict):
        return body["migrations"]
    if body.get("migration"):
        return {"catalog": body["migration"]}
    return {}


# Tenancy migrations add composite foreign keys. A database holding a cross-organization reference makes
# them fail with an IntegrityError, so these queries find the rows first. Each works on the schema before and after its
# migration (organization is read through the parent row, not the column the migration adds). The runbook's
# "Cross-organization references" section lists the same queries.
CROSS_ORG_CHECKS = [
    # (migration, constraint, what, query returning the offending rows' ids)
    (
        ("catalog", "0021_composite_keys"),
        "catalog_person_org_department_fk",
        "Person.department",
        "SELECT p.id FROM catalog_person p JOIN catalog_department d ON d.id = p.department_id "
        "WHERE d.organization_id <> p.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_resource_org_business_owner_fk",
        "Resource.business_owner",
        "SELECT r.id FROM catalog_resource r JOIN catalog_person t ON t.id = r.business_owner_id "
        "WHERE t.organization_id <> r.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_resource_org_technical_owner_fk",
        "Resource.technical_owner",
        "SELECT r.id FROM catalog_resource r JOIN catalog_person t ON t.id = r.technical_owner_id "
        "WHERE t.organization_id <> r.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_resource_org_support_team_fk",
        "Resource.support_team",
        "SELECT r.id FROM catalog_resource r JOIN catalog_team t ON t.id = r.support_team_id "
        "WHERE t.organization_id <> r.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_resource_org_created_by_import_fk",
        "Resource.created_by_import",
        "SELECT r.id FROM catalog_resource r JOIN catalog_importrun t ON t.id = r.created_by_import_id "
        "WHERE t.organization_id <> r.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_application_org_vendor_fk",
        "Application.vendor",
        "SELECT a.resource_id FROM catalog_application a JOIN catalog_resource r ON r.id = a.resource_id "
        "JOIN catalog_vendor v ON v.id = a.vendor_id WHERE v.organization_id <> r.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_recordreview_org_resource_fk",
        "RecordReview.resource",
        "SELECT x.id FROM catalog_recordreview x JOIN catalog_resource t ON t.id = x.resource_id "
        "WHERE t.organization_id <> x.organization_id",
    ),
    (
        ("catalog", "0021_composite_keys"),
        "catalog_recordreview_org_confirmed_by_fk",
        "RecordReview.confirmed_by",
        "SELECT x.id FROM catalog_recordreview x JOIN catalog_person t ON t.id = x.confirmed_by_id "
        "WHERE t.organization_id <> x.organization_id",
    ),
    (
        ("imports", "0002_importrow_tenancy"),
        "catalog_importrow_org_matched_resource_fk",
        "ImportRow.matched_resource",
        "SELECT x.id FROM catalog_importrow x JOIN catalog_importrun run ON run.id = x.run_id "
        "JOIN catalog_resource t ON t.id = x.matched_resource_id WHERE t.organization_id <> run.organization_id",
    ),
    (
        ("imports", "0002_importrow_tenancy"),
        "catalog_importrow_org_possible_match_fk",
        "ImportRow.possible_match",
        "SELECT x.id FROM catalog_importrow x JOIN catalog_importrun run ON run.id = x.run_id "
        "JOIN catalog_resource t ON t.id = x.possible_match_id WHERE t.organization_id <> run.organization_id",
    ),
    (
        ("imports", "0002_importrow_tenancy"),
        "catalog_importrow_org_result_resource_fk",
        "ImportRow.result_resource",
        "SELECT x.id FROM catalog_importrow x JOIN catalog_importrun run ON run.id = x.run_id "
        "JOIN catalog_resource t ON t.id = x.result_resource_id WHERE t.organization_id <> run.organization_id",
    ),
]


def applied_migrations():
    return set(MigrationExecutor(connections[DEFAULT_DB_ALIAS]).loader.applied_migrations)


def cross_org_references():
    """[(what, constraint, ids)] for each pending tenancy migration's check that finds rows; [] when none would fail.
    Checks whose migration is already applied are skipped: the constraint is in place, so no such row can exist."""
    applied = applied_migrations()
    found = []
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        for migration, constraint, what, sql in CROSS_ORG_CHECKS:
            if migration in applied:
                continue
            cursor.execute(sql)
            ids = [str(row[0]) for row in cursor.fetchall()]
            if ids:
                found.append((what, constraint, ids))
    return found


def tenancy_checks_pending():
    applied = applied_migrations()
    return any(migration not in applied for migration, *_ in CROSS_ORG_CHECKS)


def _number(name):
    return int(name.split("_", 1)[0]) if name and name[:4].isdigit() else -1


def marks(stream) -> tuple[str, str]:
    """The pass and fail marks: check and cross where the console can show them, else OK and FAIL (a Windows console
    on cp1252 can't encode the check mark, and writing one there raised UnicodeEncodeError)."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:  # an in-memory text stream holds any character
        return "✓", "✗"
    try:
        "✓✗".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return "OK", "FAIL"
    return "✓", "✗"


def tolerant(stream) -> None:
    """Characters the console can't encode (a record or organization name, say) print as "?" instead of stopping the
    checks half way."""
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # a stream that can't be reconfigured now keeps its own handling
            pass


def _describe(migrations):
    return ", ".join(f"{app} {name}" for app, name in migrations.items() if name) or "none"


class Command(BaseCommand):
    help = (
        "Post-upgrade and restore checks, and the manifest of what must survive an upgrade (see the upgrade runbook)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--write-manifest", metavar="PATH", help="Write the manifest and exit.")
        parser.add_argument("--manifest", metavar="PATH", help="Also check that nothing in this manifest was lost.")
        parser.add_argument(
            "--precheck",
            action="store_true",
            help="Before migrating: report rows that would stop a pending migration, and exit.",
        )

    def handle(self, *args, write_manifest=None, manifest=None, precheck=False, **options):
        out = getattr(self.stdout, "_out", self.stdout)  # Django's OutputWrapper around the real stream
        tolerant(out)
        self.pass_mark, self.fail_mark = marks(out)
        if precheck:
            self.failures = 0
            self.check_cross_org_references()
            if self.failures:
                raise CommandError("Fix the rows above before upgrading (see the runbook).")
            self.stdout.write(self.style.SUCCESS("Ready to migrate."))
            return
        if write_manifest:
            cutoff = timezone.now()
            body = {
                "written_at": cutoff.isoformat(),
                "migrations": latest_migrations(),
                **snapshot(cutoff),
            }
            Path(write_manifest).write_text(json.dumps(body, indent=2), encoding="utf-8")
            self.stdout.write(
                f"Manifest written to {write_manifest}: {body['reviews']['count']} reviews, "
                f"{body['renewal_completions']['count']} renewal completions, {body['audit_events']['count']} audit events, "
                f"{body['records']['count']} records, {body['contracts']['count']} contracts, "
                f"{body['memberships']['count']} memberships."
            )
            return
        self.failures = 0
        self.check_database()
        if tenancy_checks_pending():
            self.check_cross_org_references()
        if manifest:
            self.check_manifest(manifest)
        for org in Organization.objects.all():
            self.check_organization(org)
        age = heartbeat_age()
        if age is None or age > 180:
            # Reported, not failed: the web app serves every action without the worker.
            self.stdout.write(f"! Worker: {'never ran' if age is None else f'last tick {int(age)} s ago'}. Start it.")
        else:
            self.ok("Worker ticking")
        if self.failures:
            raise CommandError(f"{self.failures} check(s) failed. Restore the pre-upgrade backup (see the runbook).")
        self.stdout.write(self.style.SUCCESS("All upgrade checks passed."))

    def ok(self, text):
        self.stdout.write(f"{self.pass_mark} {text}")

    def fail(self, text):
        self.failures += 1
        self.stdout.write(self.style.ERROR(f"{self.fail_mark} {text}"))

    def check_database(self):
        executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if pending:
            self.fail(f"{len(pending)} migration(s) not applied. Run `manage.py migrate`.")
        else:
            self.ok(f"Database migrated (latest {_describe(latest_migrations())})")

    def check_cross_org_references(self):
        """Rows that reference another organization's row would stop the tenancy migrations."""
        if not tenancy_checks_pending():
            self.ok("Tenancy migrations already applied; no cross-organization references possible")
            return
        found = cross_org_references()
        for what, constraint, ids in found:
            shown = ", ".join(ids[:5]) + (f" and {len(ids) - 5} more" if len(ids) > 5 else "")
            self.fail(
                f"{what}: {len(ids)} row(s) point at another organization's row and would stop the migration "
                f"adding {constraint} (ids {shown}). See 'Cross-organization references' in the runbook."
            )
        if not found:
            self.ok("No cross-organization references block the pending tenancy migrations")

    def check_manifest(self, path):
        try:
            before = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise CommandError(f"Can't read the manifest: {e}") from None
        now = snapshot(datetime.fromisoformat(before["written_at"]))
        self.check_manifest_migrations(manifest_migrations(before))
        for key, label in MANIFEST_PARTS:
            if key not in before:
                self.stdout.write(f"- {label}: not in this manifest (written by an earlier release); not checked")
            elif "rows" in before[key]:
                have = {tuple(row) for row in now[key]["rows"]}
                lost = [row for row in before[key]["rows"] if tuple(row) not in have]
                if lost:
                    shown = ", ".join(row[0] for row in lost[:5]) + (
                        f" and {len(lost) - 5} more" if len(lost) > 5 else ""
                    )
                    self.fail(
                        f"{label}: {len(lost)} of the manifest's {before[key]['count']} are missing or moved to another "
                        f"organization (ids {shown}). One deleted after the manifest was written shows here too: see "
                        "the runbook, step 1."
                    )
                else:
                    self.ok(f"{label} intact ({before[key]['count']} in the manifest, {now[key]['count']} now)")
            elif now[key] == before[key]:
                self.ok(f"{label} intact ({now[key]['count']})")
            else:
                self.fail(f"{label} differ from the manifest: {before[key]['count']} before, {now[key]['count']} now.")

    def check_manifest_migrations(self, before):
        """An upgrade only moves migrations forward; an app behind the manifest means an older backup was restored
        than the one the manifest was written for (or a migration was removed)."""
        if not before:
            return
        now = latest_migrations()
        behind = [
            f"{app} {now.get(app) or 'none'} < {name}"
            for app, name in sorted(before.items())
            if name and _number(now.get(app)) < _number(name)
        ]
        if behind:
            self.fail(f"Migrations are behind the manifest: {'; '.join(behind)}.")
        else:
            self.ok(f"Migrations at or past the manifest ({_describe(before)})")

    def check_organization(self, org):
        admin = (
            Membership.objects.filter(organization=org, role=Role.ADMIN, user__is_active=True)
            .select_related("user")
            .order_by("user_id")
            .first()
        )
        if admin is None:
            self.fail(f"{org.name}: no active Admin to check with.")
            return
        host = next((h for h in settings.ALLOWED_HOSTS if h and h != "*" and not h.startswith(".")), "localhost")
        client = Client(HTTP_HOST=host)
        anonymous = client.get("/api/v1/auth/options")
        if anonymous.status_code == 200:
            self.ok(f"{org.name}: sign-in page options load")
        else:
            self.fail(f"{org.name}: sign-in options answered {anonymous.status_code}")
        client.force_login(admin.user)
        try:
            self.page(client, org, "Signed-in user", "/api/v1/auth/me")
            self.page(client, org, "Needs attention", "/api/v1/needs-attention?scope=all")
            review = (
                RecordReview.objects.filter(organization=org, resource__archived_at__isnull=True)
                .select_related("resource")
                .first()
            )
            if review is None:
                self.stdout.write(f"- {org.name}: no record has a review yet; skipped")
            else:
                r = review.resource
                url = f"/api/v1/{KIND_PATHS[r.kind]}/{r.id}"  # applications, components, ai-agents
                self.page(client, org, f"Record with reviews ({r.name})", url)
                self.page(client, org, f"Its review history ({r.name})", f"{url}/reviews")
            cycle = RenewalCycle.objects.filter(organization=org, completed_on__isnull=False).first()
            if cycle is None:
                self.stdout.write(f"- {org.name}: no renewal cycle is completed yet; skipped")
            else:
                body = self.page(
                    client, org, "Renewal with completion evidence", f"/api/v1/renewals/{cycle.obligation_id}"
                )
                cycles = [(body or {}).get("cycle") or {}, *((body or {}).get("history") or [])]
                if body is not None and not any(c.get("completion_evidence") for c in cycles):
                    self.fail(f"{org.name}: the renewal's completion evidence is missing from its history.")
        finally:
            client.logout()  # the check's session ends (an empty tombstone for two minutes: core/sessions.py)

    def page(self, client, org, label, url):
        started = time.monotonic()
        response = client.get(url)
        took = time.monotonic() - started
        if response.status_code != 200:
            self.fail(f"{org.name}: {label} answered {response.status_code}")
            return None
        note = f" (slow: {took:.1f} s)" if took > SLOW_SECONDS else ""
        self.ok(f"{org.name}: {label} loads{note}")
        return response.json()
