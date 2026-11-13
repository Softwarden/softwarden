"""Tenant backups and data export on request (catalog/org_lifecycle.queue_backups and prune_backups,
catalog/platform_admin.request_export)."""

from __future__ import annotations

import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from catalog import org_lifecycle, platform_admin
from core import capabilities, jobs
from core.models import (
    AuditEvent,
    ExportPurpose,
    ExportStatus,
    Job,
    JobStatus,
    Organization,
    OrgExport,
    PlatformAuditEvent,
)
from tests.conftest import drain_jobs, make_org
from tests.test_org_lifecycle import zip_of
from tests.test_platform import make_staff, start

RESTRICTED_TYPE = "/problems/impersonation-restricted"


@pytest.fixture(autouse=True)
def fresh_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db):
    return make_staff()


def cloud(org):
    return capabilities.set_edition(org, "cloud")[0]


def backups(org):
    return list(OrgExport.objects.filter(organization=org, purpose=ExportPurpose.BACKUP).order_by("created_at"))


def run_backups_now():
    """Make every queued backup job due (their start is staggered) and run them."""
    Job.objects.filter(kind=org_lifecycle.EXPORT_JOB, status=JobStatus.QUEUED).update(run_after=timezone.now())
    return drain_jobs(org_lifecycle.EXPORT_JOB)


# ---- the schedule -----------------------------------------------------------------------------------------------------


def test_the_schedule_queues_only_eligible_organizations(a, b, staff):
    eligible = cloud(a["org"])
    community = b["org"]  # no org.tenant_backups
    suspended = cloud(make_org("susp")["org"])
    platform_admin.suspend(staff, suspended, {"reason": "Unpaid"})
    leaving = cloud(make_org("leaving")["org"])
    Organization.objects.filter(pk=leaving.pk).update(
        deletion_requested_at=timezone.now(), deletion_due_at=timezone.now() + timedelta(days=30)
    )
    enterprise = capabilities.set_edition(make_org("ent")["org"], "enterprise")[0]
    granted = make_org("granted")["org"]
    platform_admin.set_override(staff, granted, {"capability": "org.tenant_backups", "effect": "grant", "reason": "x"})
    revoked = cloud(make_org("revoked")["org"])
    platform_admin.set_override(staff, revoked, {"capability": "org.tenant_backups", "effect": "revoke", "reason": "x"})

    now = timezone.now()
    result = org_lifecycle.queue_backups(now)
    assert result["queued"] == 3 and result["failed"] == 0
    queued = {e.organization_id for e in OrgExport.objects.filter(purpose=ExportPurpose.BACKUP)}
    assert queued == {eligible.pk, enterprise.pk, granted.pk}
    assert not {community.pk, suspended.pk, leaving.pk, revoked.pk} & queued
    # Staggered: each job starts at its organization's fixed offset within the spread, not all at once.
    for export in OrgExport.objects.filter(purpose=ExportPurpose.BACKUP):
        assert export.job_id is not None
        job = Job.objects.get(pk=export.job_id)
        offset = job.run_after - now
        assert offset == org_lifecycle.backup_offset(export.organization_id)
        assert timedelta(0) <= offset < org_lifecycle.BACKUP_SPREAD
        assert export.requested_by is None and export.status == ExportStatus.QUEUED
    assert len({org_lifecycle.backup_offset(o) for o in queued}) > 1
    # A second run the same night doesn't double a backup still waiting.
    assert org_lifecycle.queue_backups(now)["queued"] == 0


def test_the_offset_spreads_organizations_over_the_window(db):
    offsets = {org_lifecycle.backup_offset(f"00000000-0000-0000-0000-{n:012d}") for n in range(200)}
    assert len(offsets) > 100
    assert all(timedelta(0) <= o < org_lifecycle.BACKUP_SPREAD for o in offsets)


def test_the_schedule_is_registered_nightly():
    schedule = jobs.SCHEDULES[org_lifecycle.BACKUP_SCHEDULE]
    assert schedule.func is org_lifecycle.queue_backups
    minute, hour, *_ = schedule.cron.split()
    assert minute.isdigit() and hour.isdigit()  # once a night


def test_a_backup_is_built_kept_without_expiry_and_audited(a, media_root):
    org = cloud(a["org"])
    org_lifecycle.queue_backups(timezone.now())
    run_backups_now()
    (backup,) = backups(org)
    assert backup.status == ExportStatus.READY and backup.expires_at is None
    assert org_lifecycle.downloadable(backup)
    assert (Path(media_root) / backup.storage_name).exists()
    # Expiring exports leaves backups alone: they are kept by count.
    org_lifecycle.expire_exports(timezone.now() + timedelta(days=60))
    backup.refresh_from_db()
    assert backup.status == ExportStatus.READY
    event = AuditEvent.objects.get(organization=org, action="organization.export_ready")
    assert event.changes["purpose"] == "backup" and event.actor is None


# ---- retention ---------------------------------------------------------------------------------------------------------


def _ready_backup(org, days_ago, media_root) -> OrgExport:
    export = OrgExport.objects.create(organization=org, purpose=ExportPurpose.BACKUP, status=ExportStatus.READY)
    name = f"{org_lifecycle.EXPORT_ROOT}/{org.pk}/{export.pk}.zip"
    path = Path(media_root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"zip")
    OrgExport.objects.filter(pk=export.pk).update(
        storage_name=name, created_at=timezone.now() - timedelta(days=days_ago), finished_at=timezone.now()
    )
    export.refresh_from_db()
    return export


def test_retention_keeps_the_newest_and_removes_older_rows_and_files(a, b, media_root, settings):
    settings.SOFTWARDEN_TENANT_BACKUP_RETENTION = 3
    org = cloud(a["org"])
    made = [_ready_backup(org, days, media_root) for days in range(6, 0, -1)]  # oldest first
    failed = [
        OrgExport.objects.create(organization=org, purpose=ExportPurpose.BACKUP, status=ExportStatus.FAILED)
        for _ in range(5)
    ]
    theirs = [_ready_backup(b["org"], days, media_root) for days in range(6, 0, -1)]
    plain = OrgExport.objects.create(organization=org, purpose=ExportPurpose.EXPORT, status=ExportStatus.FAILED)

    removed = org_lifecycle.prune_backups(org.pk)
    assert removed == 3 + 2
    kept = {e.pk for e in backups(org) if e.status == ExportStatus.READY}
    assert kept == {e.pk for e in made[-3:]}
    assert OrgExport.objects.filter(organization=org, purpose="backup", status="failed").count() == 3
    for export in made[:3]:
        assert not (Path(media_root) / export.storage_name).exists()
    for export in made[-3:]:
        assert (Path(media_root) / export.storage_name).exists()
    assert OrgExport.objects.filter(pk=plain.pk).exists()  # only backups are pruned
    assert OrgExport.objects.filter(organization=b["org"]).count() == len(theirs)  # another organization's untouched
    event = AuditEvent.objects.get(organization=org, action="organization.backups_pruned")
    assert event.changes == {"removed": 5, "kept": 3}
    assert org_lifecycle.prune_backups(org.pk) == 0  # nothing more to remove, nothing audited
    assert AuditEvent.objects.filter(organization=org, action="organization.backups_pruned").count() == 1
    assert failed


def test_the_schedule_and_a_finished_backup_apply_the_retention(a, b, media_root, settings):
    settings.SOFTWARDEN_TENANT_BACKUP_RETENTION = 2
    org = cloud(a["org"])
    for days in (5, 4, 3):
        _ready_backup(org, days, media_root)
    # b lost the capability (community) but still has old backups: the schedule prunes them too.
    for days in (5, 4, 3):
        _ready_backup(b["org"], days, media_root)
    result = org_lifecycle.queue_backups(timezone.now())
    assert result["queued"] == 1 and result["pruned"] == 2
    run_backups_now()  # the new one is ready: the oldest of the two kept goes
    ready = [e for e in backups(org) if e.status == ExportStatus.READY]
    assert len(ready) == 2 and max(ready, key=lambda e: e.created_at).expires_at is None
    assert len(backups(b["org"])) == 2


def test_the_retention_setting_has_a_floor(settings):
    settings.SOFTWARDEN_TENANT_BACKUP_RETENTION = 0
    assert org_lifecycle.backup_retention() == 1


# ---- listing and downloading -----------------------------------------------------------------------------------------


def test_admins_list_and_download_backups_with_the_exports(a, api):
    org = cloud(a["org"])
    org_lifecycle.queue_backups(timezone.now())
    run_backups_now()
    admin = api(a["users"]["admin"])
    (row,) = admin.get("organization/exports").json_body["results"]
    assert row["purpose"] == "backup" and row["status"] == "ready" and row["expires_at"] is None
    assert row["download_url"] == f"/api/v1/organization/exports/{row['id']}/download"
    response = admin.client.get(row["download_url"])
    assert response.status_code == 200
    names = zip_of(response).namelist()
    assert "manifest.json" in names
    assert AuditEvent.objects.filter(organization=org, action="organization.export_downloaded").exists()
    for role in ("editor", "viewer"):
        assert api(a["users"][role]).client.get(row["download_url"]).status_code == 403


def test_a_backup_download_is_refused_while_impersonating(a, staff, api):
    cloud(a["org"])
    org_lifecycle.queue_backups(timezone.now())
    run_backups_now()
    (backup,) = backups(a["org"])
    client = api(staff)
    assert start(client, a["org"], a["users"]["admin"]).status_code == 201
    response = client.client.get(f"/api/v1/organization/exports/{backup.id}/download")
    assert response.status_code == 403 and response.json()["type"] == RESTRICTED_TYPE
    assert client.get("organization/exports").status_code == 200  # the list carries no data


def test_a_backup_in_flight_does_not_block_an_admins_export(a, api):
    cloud(a["org"])
    org_lifecycle.queue_backups(timezone.now())  # queued, waiting for its offset
    response = api(a["users"]["admin"]).post("organization/exports")
    assert response.status_code == 201, response.json_body
    assert response.json_body["purpose"] == "export"


def test_backups_follow_the_organizations_capability(a, api):
    org = a["org"]  # community
    assert org_lifecycle.queue_backups(timezone.now())["queued"] == 0
    cloud(org)
    assert org_lifecycle.queue_backups(timezone.now())["queued"] == 1


# ---- data export on request (the operator) ---------------------------------------------------------------------------


def test_the_operator_makes_an_export_the_organization_asked_for(a, b, staff, api):
    org = a["org"]
    response = api(staff).post(f"platform/organizations/{org.id}/exports", {"reason": "Ticket 99: customer asked"})
    assert response.status_code == 201, response.json_body
    body = response.json_body
    assert body["purpose"] == "operator" and body["status"] == "queued" and body["download_url"] is None
    export = OrgExport.objects.get(pk=body["id"])
    assert export.organization_id == org.pk and export.requested_by == staff
    event = AuditEvent.objects.get(organization=org, action="organization.export_requested")
    assert event.changes["purpose"] == "operator" and event.changes["via"] == "platform"
    assert event.changes["reason"] == "Ticket 99: customer asked" and event.actor == staff
    platform = PlatformAuditEvent.objects.get(action="export.requested")
    assert platform.organization_id == org.pk and platform.details["export"] == str(export.pk)
    assert not AuditEvent.objects.filter(organization=b["org"], action="organization.export_requested").exists()

    drain_jobs(org_lifecycle.EXPORT_JOB)
    admin = api(a["users"]["admin"])
    (row,) = admin.get("organization/exports").json_body["results"]
    assert row["purpose"] == "operator" and row["requested_by"]["id"] == staff.id
    download = admin.client.get(row["download_url"])
    assert download.status_code == 200 and "manifest.json" in zip_of(download).namelist()
    # The platform API never serves the file: staff with no membership can't reach the tenant route either.
    assert api(staff).client.get(row["download_url"]).status_code == 403
    assert api(b["users"]["admin"]).client.get(row["download_url"]).status_code == 404


def test_the_operators_export_needs_a_reason_and_waits_for_one_in_flight(a, staff, api):
    org = a["org"]
    response = api(staff).post(f"platform/organizations/{org.id}/exports", {})
    assert response.status_code == 400 and "reason" in response.json_body["errors"]
    assert api(a["users"]["admin"]).post("organization/exports").status_code == 201
    busy = api(staff).post(f"platform/organizations/{org.id}/exports", {"reason": "Ticket 1"})
    assert busy.status_code == 409
    assert not PlatformAuditEvent.objects.filter(action="export.requested").exists()
    assert api(a["users"]["admin"]).post(f"platform/organizations/{org.id}/exports", {"reason": "x"}).status_code == 403


def test_no_platform_route_returns_an_export_file():
    from catalog import platform_urls

    routes = [str(p.pattern) for p in platform_urls.urlpatterns]
    assert not [r for r in routes if "download" in r]
    assert settings.ROOT_URLCONF == "softwarden.urls"


def test_an_export_zip_is_a_zip(a, staff, api, media_root):
    api(staff).post(f"platform/organizations/{a['org'].id}/exports", {"reason": "Ticket 5"})
    drain_jobs(org_lifecycle.EXPORT_JOB)
    export = OrgExport.objects.get(organization=a["org"])
    with zipfile.ZipFile(Path(media_root) / export.storage_name) as archive:
        assert "organization.json" in archive.namelist()
    assert export.expires_at is not None  # an operator's export expires like an Admin's


# ---- review follow-ups --------------------------------------------------------------------------------------------------


def test_a_backup_does_not_suppress_the_final_export_retry(a, media_root):
    org = cloud(a["org"])
    asked = timezone.now() - timedelta(hours=1)
    Organization.objects.filter(pk=org.pk).update(
        deletion_requested_at=asked, deletion_due_at=timezone.now() + timedelta(days=30)
    )
    OrgExport.objects.create(organization=org, purpose=ExportPurpose.DELETION, status=ExportStatus.FAILED)
    _ready_backup(org, 0, media_root)  # made after the request: not a final export
    org_lifecycle.queue_backups(timezone.now())  # scheduled for deletion: nothing queued
    OrgExport.objects.create(organization=org, purpose=ExportPurpose.BACKUP, status=ExportStatus.QUEUED)
    assert org_lifecycle.retry_final_export(org.pk) is True
    assert OrgExport.objects.filter(organization=org, purpose="deletion", status="queued").count() == 1


def test_what_admins_see_follows_each_exports_purpose(a, staff, api, media_root):
    org = a["org"]  # community: org.export, no org.tenant_backups
    made = {
        purpose: OrgExport.objects.create(organization=org, purpose=purpose, status=ExportStatus.QUEUED)
        for purpose in ("export", "deletion", "backup", "operator")
    }
    # The Admin's own export is ready, with its file, so its download can be refused for visibility alone.
    ready = made["export"]
    name = f"{org_lifecycle.EXPORT_ROOT}/{org.pk}/{ready.pk}.zip"
    (Path(media_root) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(media_root) / name).write_bytes(b"zip")
    OrgExport.objects.filter(pk=ready.pk).update(
        status=ExportStatus.READY, storage_name=name, finished_at=timezone.now(),
        expires_at=timezone.now() + timedelta(days=7),
    )  # fmt: skip
    download = f"organization/exports/{ready.id}/download"
    admin = api(a["users"]["admin"])
    assert admin.client.get(f"/api/v1/{download}").status_code == 200
    for role in ("editor", "viewer"):  # the list is Admin only, whatever the purposes
        assert api(a["users"][role]).get("organization/exports").status_code == 403

    def listed():
        return {row["purpose"] for row in admin.get("organization/exports").json_body["results"]}

    assert listed() == {"export", "deletion", "backup", "operator"}
    revoke = {"capability": "org.export", "effect": "revoke", "reason": "Test"}
    override = platform_admin.set_override(staff, org, revoke)
    assert listed() == {"operator"}  # the operator's export stays theirs
    assert admin.post("organization/exports").json_body["capability"] == "org.export"  # asking still needs it
    assert admin.get(f"organization/exports/{made['export'].id}").status_code == 404
    assert admin.client.get(f"/api/v1/{download}").status_code == 404  # the file is there; the purpose isn't visible
    assert admin.get(f"organization/exports/{made['operator'].id}").status_code == 200
    platform_admin.set_override(staff, org, {"capability": "org.tenant_backups", "effect": "grant", "reason": "T"})
    assert listed() == {"operator", "backup"}
    platform_admin.remove_override(staff, org, override, {"reason": "Done"})
    assert listed() == {"export", "deletion", "backup", "operator"}


def test_the_deletion_banner_link_downloads_after_org_export_is_revoked(a, staff, api):
    org = cloud(a["org"])  # org.self_delete
    admin = api(a["users"]["admin"])
    asked = admin.post("organization/deletion", {"confirm": org.slug, "password": "correct-horse-battery"})
    assert asked.status_code in (200, 201), asked.json_body
    drain_jobs(org_lifecycle.EXPORT_JOB)
    platform_admin.set_override(staff, org, {"capability": "org.export", "effect": "revoke", "reason": "Test"})
    banner = admin.get("auth/me").json_body["organization_deletion"]
    url = banner["final_export"]["download_url"]
    final = OrgExport.objects.get(organization=org, purpose="deletion")
    assert url == f"/api/v1/organization/exports/{final.id}/download"
    response = admin.client.get(url)
    assert response.status_code == 200 and "manifest.json" in zip_of(response).namelist()
    assert {row["purpose"] for row in admin.get("organization/exports").json_body["results"]} == {"deletion"}
