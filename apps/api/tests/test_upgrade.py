"""Upgrades and restores: forward-only migrations and the upgrade_check command."""

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog import services
from catalog.models import RecordReview
from core.management.commands.upgrade_check import migrated_apps
from money import renewals
from softwarden import ee

API_DIR = Path(__file__).resolve().parent.parent
FOREIGN = {".venv", "venv", "site-packages", "node_modules"}  # other people's code under the API directory


def migration_files():
    """Every migration file under the API directory at any depth (ee/<module>/migrations too), but other people's."""
    found = []
    for folder, dirs, files in os.walk(API_DIR):
        dirs[:] = [d for d in dirs if d not in FOREIGN and not d.startswith((".", "__"))]
        if Path(folder).name == "migrations":
            found += [Path(folder) / f for f in files if f.startswith("0") and f.endswith(".py")]
    return sorted(found)


def shipped_migrations():
    """Every migration file of every local app, keyed app/name (catalog/0019_split_apps.py)."""
    return {f"{p.parent.parent.name}/{p.name}": p for p in migration_files()}


def test_every_local_app_is_guarded():
    """Every local app, at any depth (an app under ee/ escaped a top-level scan), with a table has migrations
    the guard below hashes and the manifest records; and every migration folder belongs to an installed local app,
    keyed by its label."""
    from core.local_apps import local_app_configs

    configs = local_app_configs()
    assert {
        "core",
        "catalog",
        "money",
        "notify",
        "discovery",
        "imports",
        "api",
        "eol",
        "tickets",
        *(("saml", "scim") if ee.INSTALLED else ()),  # the optional `ee` package's apps, when installed
    } <= {c.label for c in configs}
    with_tables = {c.label for c in configs if list(c.get_models(include_auto_created=True))}
    assert with_tables <= set(migrated_apps()), "an app with models needs migrations"
    folders = {p.parent.parent.resolve() for p in migration_files()}
    assert folders == {Path(c.path).resolve() for c in configs if c.label in migrated_apps()}
    guarded = {key.split("/", 1)[0] for key in shipped_migrations()}
    assert guarded == set(migrated_apps())  # the folder name is the app label, as migration_hashes.json keys it


def test_applied_migrations_are_never_edited():
    """Forward-only: a change to the schema is a new migration. Editing one already shipped would leave upgraded
    databases different from new ones. A new migration is added to migration_hashes.json (keyed app/file) on purpose."""
    known = json.loads((Path(__file__).parent / "migration_hashes.json").read_text(encoding="utf-8"))
    found = {
        key: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for key, p in shipped_migrations().items()
    }
    edited = [name for name, digest in known.items() if found.get(name) != digest]
    assert edited == [], f"Shipped migrations changed or removed: {edited}"
    assert set(found) - set(known) == set(), "Add new migrations to tests/migration_hashes.json"


def test_no_migration_replaces_or_squashes_others():
    for key, p in shipped_migrations().items():
        assert "replaces =" not in p.read_text(encoding="utf-8"), key


@pytest.fixture
def history(a):
    org, editor = a["org"], a["users"]["editor"]
    services.record_review(
        org,
        editor,
        a["app"],
        {
            "confirmed_by": str(a["people"]["dana"].id),
            "confirmation_date": "2026-10-01",
            "evidence": "Checked in the quarterly review",
        },
        today=date(2026, 10, 2),
    )
    obligation = renewals.create_renewal(
        org,
        editor,
        {"name": "Workday", "auto_renew": "yes", "renewal_date": "2026-12-31", "applications": [str(a["app"].id)]},
    )
    cycle = obligation.cycles.get()
    renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "renew", "decided_on": "2026-11-01"})
    renewals.record_completion(
        org, editor, obligation, cycle.id, {"completed_on": "2026-11-02", "completion_evidence": "PO 4471"}
    )
    return a


def test_post_upgrade_checks_pass(history, capsys):
    call_command("upgrade_check")
    out = capsys.readouterr().out
    for line in (
        "Database migrated",
        "sign-in page options load",
        "Needs attention loads",
        "Record with reviews",
        "Renewal with completion evidence loads",
        "All upgrade checks passed.",
    ):
        assert line in out, out


def test_post_upgrade_checks_follow_an_ai_agents_review(history, capsys):
    """The newest review belongs to an AI agent (seed_demo's HR Policy Assistant): its page is /ai-agents/<id>."""
    org, editor = history["org"], history["users"]["editor"]
    bot = services.create_resource(org, editor, {"name": "Bot", "kind": "ai_agent", "lifecycle": "active"})
    services.record_review(
        org,
        editor,
        bot,
        {"confirmed_by": str(history["people"]["dana"].id), "confirmation_date": "2026-10-05", "evidence": "Call"},
        today=date(2026, 10, 6),
    )
    call_command("upgrade_check")
    out = capsys.readouterr().out
    assert "Record with reviews (Bot)" in out and "All upgrade checks passed." in out, out


def test_manifest_catches_a_lost_review(history, tmp_path, capsys):
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    assert body["reviews"]["count"] == 1 and body["renewal_completions"]["count"] == 1
    # New audit events after the manifest (a sign-in, say) are expected and don't fail it.
    services.audit(history["org"], None, "user.login", None, {})
    call_command("upgrade_check", manifest=str(manifest))
    assert "Record reviews intact (1)" in capsys.readouterr().out
    RecordReview.objects.all().update(evidence="tampered")
    with pytest.raises(CommandError):
        call_command("upgrade_check", manifest=str(manifest))
    assert "Record reviews differ from the manifest" in capsys.readouterr().out


def test_manifest_keeps_records_contracts_and_memberships_row_by_row(history, tmp_path, capsys):
    """A record, contract or membership lost, or moved to another organization, fails the
    check; one added after the manifest (the app keeps running until the upgrade) doesn't."""
    from catalog.models import Resource
    from core.models import Membership, Organization, Role
    from money.models import Contract

    org, editor = history["org"], history["users"]["editor"]
    services.create_resource(org, editor, {"name": "Before", "kind": "application", "lifecycle": "active"})
    contract = Contract.objects.create(organization=org, name="Workday subscription")
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    assert body["records"]["count"] == Resource.objects.count() >= 2
    assert body["contracts"] == {"count": 1, "rows": [[str(contract.id), str(org.id)]]}
    assert body["memberships"]["count"] == Membership.objects.count() >= 3
    assert "records, 1 contracts" in capsys.readouterr().out

    services.create_resource(org, editor, {"name": "After", "kind": "application", "lifecycle": "active"})
    call_command("upgrade_check", manifest=str(manifest))
    out = capsys.readouterr().out
    for line in ("Records intact (", "Contracts intact (1 in the manifest, 1 now)", "Memberships intact ("):
        assert line in out, out
    # A role an Admin changes before the upgrade is not a membership lost.
    Membership.objects.filter(user=editor, organization=org).update(role=Role.VIEWER)
    call_command("upgrade_check", manifest=str(manifest))
    assert "Memberships intact (" in capsys.readouterr().out

    other = Organization.objects.create(name="Elsewhere")
    Membership.objects.filter(user=editor, organization=org).update(organization=other)
    Contract.objects.filter(id=contract.id).delete()
    with pytest.raises(CommandError):
        call_command("upgrade_check", manifest=str(manifest))
    out = capsys.readouterr().out
    assert f"Contracts: 1 of the manifest's 1 are missing or moved to another organization (ids {contract.id})." in out
    assert "One deleted after the manifest was written shows here too" in out
    assert "Memberships: 1 of the manifest's" in out and "Records intact" in out


def test_a_manifest_from_before_f43_is_checked_for_what_it_has(history, tmp_path, capsys):
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    for key in ("records", "contracts", "memberships"):
        del body[key]
    manifest.write_text(json.dumps(body), encoding="utf-8")
    call_command("upgrade_check", manifest=str(manifest))
    out = capsys.readouterr().out
    assert "- Records: not in this manifest (written by an earlier release); not checked" in out
    assert "Record reviews intact (1)" in out and "All upgrade checks passed." in out


def test_pending_migration_fails(history, monkeypatch, capsys):
    from django.db.migrations.executor import MigrationExecutor

    monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets: ["pending"])
    with pytest.raises(CommandError):
        call_command("upgrade_check")
    assert "1 migration(s) not applied" in capsys.readouterr().out


def test_manifest_records_each_apps_latest_migration(history, tmp_path, capsys):
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(body["migrations"]) == set(migrated_apps())
    assert all(body["migrations"].values())
    call_command("upgrade_check", manifest=str(manifest))
    out = capsys.readouterr().out
    assert f"Database migrated (latest api {body['migrations']['api']}, catalog {body['migrations']['catalog']}" in out
    assert "Migrations at or past the manifest" in out


def test_manifest_from_before_the_split_is_accepted(history, tmp_path, capsys):
    """Installs upgraded from before the catalog split hold a manifest with one catalog migration string."""
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    del body["migrations"]
    body["migration"] = "0018_import_tags_tokens"
    manifest.write_text(json.dumps(body), encoding="utf-8")
    call_command("upgrade_check", manifest=str(manifest))
    out = capsys.readouterr().out
    assert "Migrations at or past the manifest (catalog 0018_import_tags_tokens)" in out
    assert "All upgrade checks passed." in out


def test_manifest_ahead_of_the_database_fails(history, tmp_path, capsys):
    """A restore of a backup older than the manifest shows up as migrations behind it."""
    manifest = tmp_path / "before.json"
    call_command("upgrade_check", write_manifest=str(manifest))
    body = json.loads(manifest.read_text(encoding="utf-8"))
    current = body["migrations"]["money"]  # the newest money migration, whichever it is now
    body["migrations"]["money"] = "0099_future"
    manifest.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("upgrade_check", manifest=str(manifest))
    assert f"Migrations are behind the manifest: money {current} < 0099_future." in (capsys.readouterr().out)
    assert current.startswith("00") and current != "0099_future"


# ---- Pre-check for the tenancy migrations --------------------------------------------------------------------------


def test_precheck_after_the_tenancy_migrations_has_nothing_to_find(a, capsys):
    call_command("upgrade_check", precheck=True)
    out = capsys.readouterr().out
    assert "Tenancy migrations already applied" in out and "Ready to migrate." in out


@pytest.fixture
def unmigrated(monkeypatch):
    """As if catalog 0021 and imports 0002 were not applied yet. Rows a test makes that break the deferred composite
    keys must be put right before the test ends: Django checks deferred constraints at teardown."""
    from core.management.commands import upgrade_check

    monkeypatch.setattr(upgrade_check, "applied_migrations", lambda: set())


def test_precheck_passes_a_clean_database(a, b, unmigrated, capsys):
    call_command("upgrade_check", precheck=True)
    out = capsys.readouterr().out
    assert "No cross-organization references block the pending tenancy migrations" in out


def test_precheck_reports_cross_org_rows(a, b, unmigrated, capsys):
    from catalog.models import Resource
    from imports.models import ImportRow, ImportRun

    Resource.objects.filter(id=a["app"].id).update(business_owner=b["people"]["dana"], support_team=b["team"])
    run = ImportRun.objects.create(
        organization=a["org"], kind="application", filename="x.csv", purge_after=date(2030, 1, 1)
    )
    row = ImportRow.objects.create(
        organization=a["org"], run=run, row_number=2, outcome="update", matched_resource=b["app"]
    )
    try:
        with pytest.raises(CommandError):
            call_command("upgrade_check", precheck=True)
    finally:
        # Django checks deferred constraints at test teardown, so put the rows right before the test ends.
        Resource.objects.filter(id=a["app"].id).update(business_owner=None, support_team=None)
        ImportRow.objects.filter(id=row.id).update(matched_resource=None)
    out = capsys.readouterr().out
    assert "Resource.business_owner: 1 row(s)" in out and "catalog_resource_org_business_owner_fk" in out
    assert str(a["app"].id) in out
    assert "Resource.support_team: 1 row(s)" in out
    assert "ImportRow.matched_resource: 1 row(s)" in out and str(row.id) in out
    assert "Resource.technical_owner" not in out and "Ready to migrate." not in out


def test_full_check_runs_the_precheck_only_while_migrations_are_pending(history, monkeypatch, capsys):
    call_command("upgrade_check")
    assert "cross-organization" not in capsys.readouterr().out
    from core.management.commands import upgrade_check

    monkeypatch.setattr(upgrade_check, "applied_migrations", lambda: set())
    call_command("upgrade_check")
    assert "No cross-organization references block the pending tenancy migrations" in capsys.readouterr().out


# ---- console output -------------------------------------------------------------------------------------------------


def test_output_on_a_console_that_cant_show_check_marks(history):
    """A Windows console on cp1252 can't encode the check mark: the checks print OK and FAIL there instead of
    stopping with UnicodeEncodeError, and other characters it can't show (in a record's name, say) print as "?"."""
    import io

    from catalog.models import Resource

    Resource.objects.filter(id=history["app"].id).update(name="Ledger ✓ 帳簿")
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    call_command("upgrade_check", stdout=console)
    console.flush()
    out = raw.getvalue().decode("cp1252")
    assert "OK Database migrated" in out and "All upgrade checks passed." in out, out
    assert "✓" not in out and "Record with reviews (Ledger ? ??)" in out, out


def test_output_keeps_check_marks_where_the_console_shows_them(history):
    import io

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="utf-8")
    call_command("upgrade_check", stdout=console)
    console.flush()
    assert "✓ Database migrated" in raw.getvalue().decode("utf-8")
