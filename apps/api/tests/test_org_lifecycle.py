"""An organization's data export and self-service deletion (catalog/org_lifecycle.py,
catalog/lifecycle_views.py)."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import uuid
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from catalog import org_lifecycle
from catalog.models import Attachment, Resource
from core import capabilities as caps
from core import jobs
from core.models import (
    AuditEvent,
    ExportStatus,
    Invitation,
    Membership,
    MicrosoftConnection,
    Organization,
    OrganizationTombstone,
    OrgExport,
    Role,
    Schedule,
    ScimToken,
    SsoSettings,
    UserIdentity,
)
from tests.conftest import drain_jobs

PASSWORD = "correct-horse-battery"  # noqa: S105  (the fixture users' synthetic password)


@pytest.fixture(autouse=True)
def fresh_throttles():
    cache.clear()
    yield
    cache.clear()


def cloud(org):
    org, _ = caps.set_edition(org, "cloud")
    return org


def zip_of(response) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content)))


def export_now(api, admin) -> dict:
    response = api(admin).post("organization/exports")
    assert response.status_code == 201, response.json_body
    drain_jobs(org_lifecycle.EXPORT_JOB)
    return api(admin).get(f"organization/exports/{response.json_body['id']}").json_body


def attach_file(org, resource, content=b"hello attachment") -> Attachment:
    name = default_storage.save(f"attachments/{org.pk}/{secrets.token_hex(8)}", ContentFile(content))
    return Attachment.objects.create(
        organization=org,
        resource=resource,
        kind="file",
        title="Runbook",
        storage_name=name,
        filename="runbook.txt",
        content_type="text/plain",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


# ---- export ---------------------------------------------------------------------------------------------------------


def test_an_admin_exports_all_data_as_a_zip(a, b, api):
    admin = a["users"]["admin"]
    attachment = attach_file(a["org"], a["app"])
    attach_file(b["org"], b["app"], b"theirs")
    export = export_now(api, admin)
    assert export["status"] == "ready" and export["purpose"] == "export"
    assert export["download_url"] == f"/api/v1/organization/exports/{export['id']}/download"
    assert export["size"] > 0 and len(export["sha256"]) == 64
    response = api(admin).client.get(export["download_url"])
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"].startswith('attachment; filename="softwarden-export-orga-')
    archive = zip_of(response)
    names = set(archive.namelist())
    assert {"manifest.json", "organization.json", "users.jsonl"} <= names
    assert "data/catalog.Resource.jsonl" in names and "data/core.AuditEvent.jsonl" in names
    assert "data/core.Job.jsonl" not in names and "data/core.OrgExport.jsonl" not in names
    assert archive.read(f"attachments/{attachment.pk}/runbook.txt") == b"hello attachment"
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["organization"]["slug"] == "orga" and manifest["attachment_files"] == 1
    resources = [json.loads(line) for line in archive.read("data/catalog.Resource.jsonl").splitlines()]
    assert {r["name"] for r in resources} == {"orga App"}  # nothing of the other organization
    assert manifest["rows"]["catalog.Resource"] == 1
    users = [json.loads(line) for line in archive.read("users.jsonl").splitlines()]
    assert {u["email"] for u in users} == {"admin@orga.test", "editor@orga.test", "viewer@orga.test"}
    assert all(
        set(u) == {"id", "email", "first_name", "last_name", "is_active", "date_joined", "last_login"} for u in users
    )
    actions = list(AuditEvent.objects.filter(organization=a["org"]).values_list("action", flat=True))
    for action in ("organization.export_requested", "organization.export_ready", "organization.export_downloaded"):
        assert action in actions
    assert (
        hashlib.sha256(b"".join(api(admin).client.get(export["download_url"]).streaming_content)).hexdigest()
        == (export["sha256"])
    )


def test_only_admins_export_and_another_organizations_export_is_404(a, b, api):
    for role in ("editor", "viewer"):
        assert api(a["users"][role]).post("organization/exports").status_code == 403
        assert api(a["users"][role]).get("organization/exports").status_code == 403
    theirs = export_now(api, b["users"]["admin"])
    client = api(a["users"]["admin"])
    assert client.get(f"organization/exports/{theirs['id']}").status_code == 404
    assert client.client.get(f"/api/v1/organization/exports/{theirs['id']}/download").status_code == 404
    assert client.get("organization/exports").json_body["results"] == []


def test_one_export_at_a_time(a, api):
    client = api(a["users"]["admin"])
    assert client.post("organization/exports").status_code == 201
    assert client.post("organization/exports").status_code == 409
    drain_jobs(org_lifecycle.EXPORT_JOB)
    assert client.post("organization/exports").status_code == 201


@pytest.mark.parametrize("read_only", [True, False])
def test_an_api_token_can_neither_request_nor_download_the_full_export(read_only, a, api):
    """The whole organization (audit log, member emails, attachments) in one file is a person's decision, made
    from a signed-in session: a leaked token, even a read-only one, never gets it. Listing and status stay readable."""
    from tests.test_tokens import Bearer, expiry

    admin = a["users"]["admin"]
    export = export_now(api, admin)
    minted = (
        api(admin)
        .post(
            "api-tokens",
            {"user": admin.id, "name": "Script", "read_only": read_only, "expires_on": expiry(a["org"], 30)},
        )
        .json_body
    )
    bearer = Bearer(minted["secret"])
    downloaded = AuditEvent.objects.filter(action="organization.export_downloaded").count()
    response = bearer.client.get(export["download_url"])
    assert response.status_code == 403 and response.json()["detail"] == org_lifecycle.SESSION_ONLY
    assert AuditEvent.objects.filter(action="organization.export_downloaded").count() == downloaded
    asked = bearer.post("organization/exports", {})
    assert asked.status_code == 403
    assert asked.json()["detail"] == ("This token is read-only." if read_only else org_lifecycle.SESSION_ONLY)
    assert OrgExport.objects.filter(organization=a["org"]).count() == 1
    assert bearer.get("organization/exports").status_code == 200
    assert bearer.get(f"organization/exports/{export['id']}").status_code == 200
    assert api(admin).client.get(export["download_url"]).status_code == 200  # the session still downloads


def test_an_expired_export_is_not_downloadable_and_its_file_is_removed(a, api):
    admin = a["users"]["admin"]
    export = export_now(api, admin)
    row = OrgExport.objects.get(pk=export["id"])
    path = Path(default_storage.path(row.storage_name))
    assert path.exists()
    OrgExport.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert api(admin).client.get(export["download_url"]).status_code == 404
    assert jobs.SCHEDULES["org.exports_expire"].func is org_lifecycle.expire_exports
    assert org_lifecycle.expire_exports(timezone.now()) == {"expired": 1, "failed": 0}
    row.refresh_from_db()
    assert row.status == ExportStatus.EXPIRED and row.storage_name == "" and not path.exists()
    assert AuditEvent.objects.filter(action="organization.export_expired", target_id=row.pk).exists()
    assert api(admin).get(f"organization/exports/{row.pk}").json_body["download_url"] is None


def test_an_export_whose_job_died_is_marked_failed(a, api):
    client = api(a["users"]["admin"])
    export_id = client.post("organization/exports").json_body["id"]
    from core.models import Job, JobStatus

    Job.objects.filter(kind=org_lifecycle.EXPORT_JOB).update(status=JobStatus.ABANDONED)
    OrgExport.objects.filter(pk=export_id).update(created_at=timezone.now() - timedelta(hours=7))
    assert org_lifecycle.expire_exports(timezone.now()) == {"expired": 0, "failed": 1}
    assert OrgExport.objects.get(pk=export_id).status == ExportStatus.FAILED
    assert AuditEvent.objects.filter(action="organization.export_failed", target_id=export_id).exists()
    assert client.post("organization/exports").status_code == 201  # a new one may be asked for


def _secret_rows(org, user) -> list[str]:
    """Rows holding each kind of stored secret, with recognisable values; returns the values."""
    from api.models import ApiToken
    from discovery.models import AssistantSettings
    from money.models import CalendarFeed
    from notify.models import EmailSettings, TeamsDestination, WebhookDestination
    from tickets.models import TicketConnection

    values = [f"SECRET-{n}-{secrets.token_hex(12)}" for n in range(15)]
    Organization.objects.filter(pk=org.pk).update(licence_key_ciphertext=values[0], licence_key_key_id="lk1")
    MicrosoftConnection.objects.create(
        organization=org, name="Graph", tenant_id="simulated", secret_ciphertext=values[1], secret_key_id="k1"
    )
    SsoSettings.objects.update_or_create(
        organization=org, defaults={"secret_ciphertext": values[2], "draft_secret_ciphertext": values[3]}
    )
    ApiToken.objects.create(
        organization=org,
        user=user,
        name="CI",
        token_hash=values[4][:64],
        prefix="sw_prefix01",
        expires_on=date(2030, 1, 1),
    )
    CalendarFeed.objects.create(organization=org, user=user, token_hash=values[5][:64], prefix="cal_prefix1")
    ScimToken.objects.create(
        organization=org,
        name="Entra",
        token_hash=values[6][:64],
        prefix="scim_prefix12",
        expires_at=timezone.now() + timedelta(days=30),
    )
    Invitation.objects.create(
        organization=org,
        email="x@orga.test",
        role=Role.VIEWER,
        token_hash=values[7][:64],
        expires_at=timezone.now() + timedelta(days=7),
    )
    EmailSettings.objects.update_or_create(
        organization=org, defaults={"smtp_password_ciphertext": values[8], "graph_secret_ciphertext": values[9]}
    )
    WebhookDestination.objects.create(
        organization=org, name="Hook", url_ciphertext=values[10], signing_secret_ciphertext=values[11]
    )
    TeamsDestination.objects.create(organization=org, name="Teams", url_ciphertext=values[12])
    AssistantSettings.objects.update_or_create(organization=org, defaults={"api_key_ciphertext": values[13]})
    TicketConnection.objects.create(  # a Jira API token
        organization=org,
        system="jira",
        name="Jira",
        base_url="https://jira.simulated.invalid",
        username="it@orga.test",
        secret_ciphertext=values[14],
        secret_key_id="k2",
    )
    return [v[:64] if n in (4, 5, 6, 7) else v for n, v in enumerate(values)] + ["sw_prefix01", "scim_prefix12"]


def test_an_export_never_holds_a_secret(a, api):
    admin = a["users"]["admin"]
    planted = _secret_rows(a["org"], admin)
    password_hash = get_user_model().objects.get(pk=admin.pk).password
    export = export_now(api, admin)
    archive = zip_of(api(admin).client.get(export["download_url"]))
    blob = b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8", "replace")
    for value in [*planted, password_hash]:
        assert value not in blob, value
    for name in archive.namelist():
        if not name.endswith((".jsonl", ".json")) or name == "manifest.json":
            continue
        for line in archive.read(name).splitlines():
            keys = set(json.loads(line))
            assert not {k for k in keys if "ciphertext" in k or k in ("token_hash", "password", "password_hash")}, name
            if any(k.endswith("_key_id") for k in keys):
                assert not {k for k in keys if k.endswith("_key_id") and k[:-7] + "_ciphertext" in keys}, name
    # the rows are there, without their secrets
    assert "data/core.MicrosoftConnection.jsonl" in archive.namelist()
    connection = json.loads(archive.read("data/core.MicrosoftConnection.jsonl").splitlines()[0])
    assert connection["name"] == "Graph" and "secret_key_id" not in connection
    organization = json.loads(archive.read("organization.json"))
    assert not {k for k in organization if k.startswith("licence_key") and k != "licence_key_set_at"}


# ---- deletion: asking, cancelling -----------------------------------------------------------------------------------


def ask(api, user, **body):
    return api(user).post("organization/deletion", {"confirm": "orga", "password": PASSWORD, **body})


def test_a_community_organization_cannot_delete_itself(a, api):
    response = ask(api, a["users"]["admin"])
    assert response.status_code == 403 and response.json_body["capability"] == "org.self_delete"
    state = api(a["users"]["admin"]).get("organization/deletion").json_body
    assert state["available"] is False and state["scheduled"] is False
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at is None


def test_only_an_admin_may_ask(a, api):
    cloud(a["org"])
    for role in ("editor", "viewer"):
        response = ask(api, a["users"][role])
        assert response.status_code == 403 and "capability" not in response.json_body
        assert api(a["users"][role]).get("organization/deletion").status_code == 403


def test_asking_needs_the_slug_and_the_password(a, api):
    cloud(a["org"])
    admin = a["users"]["admin"]
    response = ask(api, admin, confirm="ORGA")
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"confirm"}
    for password in ("wrong-password-here", None):
        body = {"confirm": "orga"} if password is None else {"confirm": "orga", "password": password}
        response = api(admin).post("organization/deletion", body)
        assert response.status_code == 403 and response.json_body["type"] == "/problems/reauthentication-required"
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at is None and not OrgExport.objects.exists()


def test_asking_schedules_deletion_and_a_final_export(a, api, settings):
    settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS = 30
    cloud(a["org"])
    admin = a["users"]["admin"]
    before = timezone.now()
    response = ask(api, admin)
    assert response.status_code == 200, response.json_body
    state = response.json_body
    assert state["scheduled"] is True and state["grace_days"] == 30 and state["requested_by"]["id"] == admin.id
    a["org"].refresh_from_db()
    assert timedelta(days=30) <= a["org"].deletion_due_at - before < timedelta(days=30, minutes=1)
    assert state["export"]["purpose"] == "deletion" and state["export"]["status"] == "queued"
    event = AuditEvent.objects.get(action="organization.deletion_requested")
    assert event.organization == a["org"] and event.actor == admin and event.changes["export"] == state["export"]["id"]
    # every member sees the banner
    for role in ("admin", "editor", "viewer"):
        me = api(a["users"][role]).get("auth/me").json_body
        assert me["organization_deletion"]["due_at"] == state["due_at"]
    assert api(a["users"]["viewer"]).get("organization").json_body["deletion"]["due_at"] == state["due_at"]
    drain_jobs(org_lifecycle.EXPORT_JOB)
    export = api(admin).get("organization/deletion").json_body["export"]
    assert export["status"] == "ready" and export["download_url"]
    assert ask(api, admin).status_code == 409  # already scheduled


def _session_signed_in(client, when) -> None:
    from core import access

    session = client.client.session
    session[access.AUTHENTICATED_AT] = when.isoformat()
    session.save()


def test_a_single_sign_on_admin_reauthenticates_by_this_sessions_recent_sign_in(a, api):
    cloud(a["org"])
    admin = a["users"]["admin"]
    admin.set_unusable_password()
    admin.save()
    client = api(admin)
    _session_signed_in(client, timezone.now() - timedelta(hours=2))
    # The account signed in a moment ago somewhere else: last_login is recent, but this session's sign-in is old.
    get_user_model().objects.filter(pk=admin.pk).update(last_login=timezone.now())
    response = client.post("organization/deletion", {"confirm": "orga"})
    assert response.status_code == 403 and "sign in again" in response.json_body["detail"]
    _session_signed_in(client, timezone.now() - timedelta(minutes=5))
    assert client.post("organization/deletion", {"confirm": "orga"}).status_code == 200


def test_every_sign_in_records_when_the_session_authenticated(a):
    """Password sign-in here; OIDC, SAML, setup, invitations and signup call the same django.contrib.auth.login, whose
    user_logged_in signal records it (core/access.remember_authentication)."""
    from django.test import Client

    from core import access

    client = Client()
    response = client.post(
        "/api/v1/auth/login",
        json.dumps({"email": "admin@orga.test", "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 200
    recorded = client.session[access.AUTHENTICATED_AT]
    assert abs(timezone.now() - datetime.fromisoformat(recorded)) < timedelta(minutes=1)


def test_an_admin_cancels_during_the_grace_period(a, api):
    cloud(a["org"])
    admin = a["users"]["admin"]
    ask(api, admin)
    response = api(admin).delete("organization/deletion")
    assert response.status_code == 200 and response.json_body["scheduled"] is False
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at is None and a["org"].deletion_requested_by is None
    assert AuditEvent.objects.filter(action="organization.deletion_cancelled", organization=a["org"]).exists()
    assert api(a["users"]["viewer"]).get("auth/me").json_body["organization_deletion"] is None
    assert api(admin).delete("organization/deletion").status_code == 409
    assert api(a["users"]["editor"]).delete("organization/deletion").status_code == 403
    # a cancelled deletion is never purged
    assert org_lifecycle.purge_due(timezone.now() + timedelta(days=60)) == {"purged": 0, "failed": 0}


def test_asking_shares_the_sign_in_rate(a, api, settings):
    cloud(a["org"])
    client = api(a["users"]["admin"])
    for _ in range(10):
        assert (
            client.post("organization/deletion", {"confirm": "orga", "password": "nope-nope-nope"}).status_code == 403
        )
    assert client.post("organization/deletion", {"confirm": "orga", "password": PASSWORD}).status_code == 429
    assert client.get("organization/deletion").status_code == 200  # reads aren't counted


# ---- deletion: the purge --------------------------------------------------------------------------------------------


def _counts(org_id) -> dict[str, int]:
    return {
        model._meta.label: model._base_manager.filter(organization_id=org_id).count()
        for model in org_lifecycle.tenant_models()
    }


def test_the_purge_removes_every_row_of_the_organization_and_nothing_of_another(a, b, api):
    call_command("seed_demo", stdout=io.StringIO())
    org = cloud(Organization.objects.get(name="Contoso (synthetic)"))
    admin = Membership.objects.get(organization=org, role=Role.ADMIN).user
    other_admin_of_b = b["users"]["admin"]
    # A member of both organizations, and an identity only the deleted organization's provider configured.
    Membership.objects.create(user=other_admin_of_b, organization=org, role=Role.EDITOR)
    SsoSettings.objects.update_or_create(organization=org, defaults={"issuer": "https://idp.contoso.test"})
    UserIdentity.objects.create(user=other_admin_of_b, issuer="https://idp.contoso.test", subject="s-1")
    resource = Resource.objects.filter(organization=org).first()
    assert resource is not None
    ours = attach_file(org, resource, b"contoso file")
    theirs = attach_file(b["org"], b["app"], b"fabrikam file")
    _secret_rows(org, admin)
    export_now(api, admin)  # an export file on disk
    before_b = _counts(b["org"].pk)
    before = _counts(org.pk)
    assert sum(1 for n in before.values() if n) >= 30, before  # a well-filled organization
    assert before["core.AuditEvent"] > 0 and before["core.OrgExport"] == 1
    export_dir = Path(default_storage.path(f"exports/{org.pk}"))
    assert export_dir.exists()

    Organization.objects.filter(pk=org.pk).update(
        deletion_requested_at=timezone.now() - timedelta(days=31),
        deletion_due_at=timezone.now() - timedelta(minutes=1),
        deletion_requested_by=admin,
    )
    assert jobs.SCHEDULES["org.purge_deleted"].func is org_lifecycle.purge_due
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}

    assert not Organization.objects.filter(pk=org.pk).exists()
    assert {label: n for label, n in _counts(org.pk).items() if n} == {}
    # B gained only the audit event for the shared account's unlinked identity
    assert _counts(b["org"].pk) == {**before_b, "core.AuditEvent": before_b["core.AuditEvent"] + 1}
    assert not Path(default_storage.path(ours.storage_name)).exists() and not export_dir.exists()
    assert Path(default_storage.path(theirs.storage_name)).exists()
    # accounts: only-here accounts deactivated (not deleted), the shared account kept active
    admin.refresh_from_db()
    assert admin.is_active is False and get_user_model().objects.filter(pk=admin.pk).exists()
    other_admin_of_b.refresh_from_db()
    assert other_admin_of_b.is_active is True
    assert Membership.objects.filter(user=other_admin_of_b).count() == 1
    assert not UserIdentity.objects.filter(user=other_admin_of_b).exists()
    unlinked = AuditEvent.objects.get(action="user.identity_unlinked")
    assert unlinked.organization == b["org"] and unlinked.changes["reason"] == "organization_deleted"
    # the tombstone
    tombstone = OrganizationTombstone.objects.get()
    assert tombstone.former_organization_id == org.pk
    assert tombstone.name_sha256 == hashlib.sha256(b"Contoso (synthetic)").hexdigest()
    assert tombstone.requested_by_id == admin.pk and tombstone.rows_deleted == sum(before.values())
    # one deleted organization only: B can still be used
    assert api(b["users"]["viewer"]).get("applications").status_code == 200


def test_the_purge_waits_for_the_due_date(a, api):
    cloud(a["org"])
    ask(api, a["users"]["admin"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}
    assert Organization.objects.filter(pk=a["org"].pk).exists() and not OrganizationTombstone.objects.exists()
    assert org_lifecycle.purge(a["org"].pk) is None


def test_purge_itself_refuses_an_organization_not_yet_due(a, b):
    """`purge()`'s own due-date filter, with nothing else (no export, no download window) that would also stop
    it. A caller that names an organization early, or a clock that moved, must not remove it."""
    now = timezone.now()
    Organization.objects.filter(pk=a["org"].pk).update(deletion_due_at=now + timedelta(days=1))
    assert org_lifecycle.purge(a["org"].pk, now) is None
    assert Organization.objects.filter(pk=a["org"].pk).exists() and not OrganizationTombstone.objects.exists()
    assert Membership.objects.filter(organization=a["org"]).exists()
    # The same organization once due is purged by the same call, so the refusal above was the date's.
    assert org_lifecycle.purge(a["org"].pk, now + timedelta(days=2)) is not None
    assert not Organization.objects.filter(pk=a["org"].pk).exists()
    assert Organization.objects.filter(pk=b["org"].pk).exists()


def test_an_api_token_can_neither_export_nor_ask_for_or_cancel_deletion(a):
    from tests.test_api_lifecycle import TokenClient

    cloud(a["org"])
    admin_token = TokenClient(a, "admin")
    exported = admin_token.call("post", "organization/exports")  # the full export is session-only too
    assert exported.status_code == 403 and exported.body["detail"] == org_lifecycle.SESSION_ONLY
    asked = admin_token.call("post", "organization/deletion", {"confirm": "orga", "password": PASSWORD})
    assert asked.status_code == 403 and "session" in asked.body["detail"]
    assert admin_token.call("delete", "organization/deletion").status_code == 403
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at is None


# ---- exports in flight, isolated purge failures, the grace floor -----------------------------------------------------


def _due_now(org) -> None:
    Organization.objects.filter(pk=org.pk).update(deletion_due_at=timezone.now() - timedelta(minutes=1))


def test_the_purge_waits_for_an_export_in_flight_then_removes_it(a, b, api):
    cloud(a["org"])
    admin = a["users"]["admin"]
    assert ask(api, admin).status_code == 200  # queues the final export
    _due_now(a["org"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}  # the export is still queued
    assert Organization.objects.filter(pk=a["org"].pk).exists()
    drain_jobs(org_lifecycle.EXPORT_JOB)
    export = OrgExport.objects.get(organization=a["org"])
    assert export.status == ExportStatus.READY
    folder = Path(default_storage.path(f"exports/{a['org'].pk}"))
    assert folder.exists()
    _age_final_export(a["org"])  # its download window has passed (see the download-window test)
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}
    assert not folder.exists() and not Organization.objects.filter(pk=a["org"].pk).exists()


def test_an_export_finished_after_its_organization_was_purged_leaves_no_file(a, api, monkeypatch):
    admin = a["users"]["admin"]
    assert api(admin).post("organization/exports").status_code == 201
    real_build = org_lifecycle.build

    def purged_meanwhile(export, renew=lambda: None):
        result = real_build(export, renew)
        OrgExport.objects.filter(pk=export.pk).delete()  # what the purge's DELETE does to the row
        return result

    monkeypatch.setattr(org_lifecycle, "build", purged_meanwhile)
    [job] = drain_jobs(org_lifecycle.EXPORT_JOB)
    job.refresh_from_db()
    assert job.result == {"discarded": "the organization was deleted"}
    assert not Path(default_storage.path(f"exports/{a['org'].pk}")).exists()


def test_building_refuses_to_recreate_a_purged_organizations_folder(a, api):
    export = OrgExport.objects.create(organization=a["org"], requested_by=a["users"]["admin"])
    OrgExport.objects.filter(pk=export.pk).delete()
    with pytest.raises(org_lifecycle.OrganizationGone):
        org_lifecycle.build(export)
    assert not Path(default_storage.path(f"exports/{a['org'].pk}")).exists()


def test_a_failed_build_whose_row_has_gone_raises_its_own_error(a, api, monkeypatch):
    admin = a["users"]["admin"]
    assert api(admin).post("organization/exports").status_code == 201

    def broken(export, renew=lambda: None):
        OrgExport.objects.filter(pk=export.pk).delete()
        raise ValueError("disk full")

    monkeypatch.setattr(org_lifecycle, "build", broken)
    [job] = drain_jobs(org_lifecycle.EXPORT_JOB)
    job.refresh_from_db()
    assert job.status == "failed" and "ValueError" in job.last_error  # not DoesNotExist


def test_no_new_export_once_deletion_is_due(a, api):
    cloud(a["org"])
    admin = a["users"]["admin"]
    ask(api, admin)
    drain_jobs(org_lifecycle.EXPORT_JOB)
    assert api(admin).post("organization/exports").status_code == 201  # during the grace period
    drain_jobs(org_lifecycle.EXPORT_JOB)
    _due_now(a["org"])
    assert api(admin).post("organization/exports").status_code == 409


def test_requesting_an_export_for_a_removed_organization_is_404(a):
    from rest_framework.exceptions import NotFound

    org = a["org"]
    ghost = Organization(pk=uuid.uuid4(), name="Gone", slug="gone")  # purged between the request and its lock
    with pytest.raises(NotFound):
        org_lifecycle.request_export(ghost, a["users"]["admin"])
    assert not OrgExport.objects.filter(organization=org).exists()


def test_the_grace_period_is_at_least_a_day(a, api, settings):
    settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS = 0
    cloud(a["org"])
    before = timezone.now()
    assert ask(api, a["users"]["admin"]).json_body["grace_days"] == 1
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at - before >= timedelta(days=1)


def test_one_failing_purge_does_not_stop_the_others(a, b, monkeypatch):
    for org in (a["org"], b["org"]):
        Organization.objects.filter(pk=org.pk).update(deletion_due_at=timezone.now() - timedelta(minutes=5))
    Organization.objects.filter(pk=a["org"].pk).update(deletion_due_at=timezone.now() - timedelta(hours=1))
    real_purge = org_lifecycle.purge

    def flaky(org_id, now=None):
        if org_id == a["org"].pk:
            raise RuntimeError("lock timeout")
        return real_purge(org_id, now)

    monkeypatch.setattr(org_lifecycle, "purge", flaky)
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 1}
    assert Organization.objects.filter(pk=a["org"].pk).exists()
    assert not Organization.objects.filter(pk=b["org"].pk).exists()


def _failing_builds(monkeypatch):
    def broken(export, renew=lambda: None):
        raise OSError("disk full")

    monkeypatch.setattr(org_lifecycle, "build", broken)


def _run_one_export() -> None:
    """Run the oldest queued export job only (drain_jobs would run the retry it queues as well)."""
    job = jobs.claim()
    assert job is not None and job.kind == org_lifecycle.EXPORT_JOB
    jobs.run(job)


def _final_exports(org):
    return list(OrgExport.objects.filter(organization=org, purpose="deletion").order_by("created_at"))


def _age_final_export(org, days=8):
    """As if the final export had been ready, and downloadable, for `days` days."""
    OrgExport.objects.filter(organization=org, status="ready").update(finished_at=timezone.now() - timedelta(days=days))


def test_a_failed_final_export_is_retried_when_it_fails_not_at_purge_time(a, api, monkeypatch):
    cloud(a["org"])
    admin = a["users"]["admin"]
    ask(api, admin)  # 30 days of grace
    _failing_builds(monkeypatch)
    _run_one_export()
    # The retry is queued by the failure itself, long before the deletion is due; no purge has run.
    assert [e.status for e in _final_exports(a["org"])] == ["failed", "queued"]
    assert AuditEvent.objects.filter(action="organization.deletion_export_retried", organization=a["org"]).count() == 1
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at > timezone.now() + timedelta(days=29)
    monkeypatch.undo()
    drain_jobs(org_lifecycle.EXPORT_JOB)  # the retry works
    retried = _final_exports(a["org"])[-1]
    assert retried.status == "ready"
    # The Admins' banner points at it, and it downloads, all during the grace period.
    banner = api(admin).get("auth/me").json_body["organization_deletion"]["final_export"]
    assert banner["status"] == "ready" and banner["download_url"].endswith(f"{retried.id}/download")
    assert api(a["users"]["viewer"]).get("auth/me").json_body["organization_deletion"]["final_export"] is None
    assert api(admin).client.get(banner["download_url"]).status_code == 200
    _age_final_export(a["org"])
    _due_now(a["org"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}


def test_a_second_failed_final_export_lets_the_purge_go_ahead(a, api, monkeypatch):
    cloud(a["org"])
    ask(api, a["users"]["admin"])
    _failing_builds(monkeypatch)
    drain_jobs(org_lifecycle.EXPORT_JOB)  # fails, queues the retry
    drain_jobs(org_lifecycle.EXPORT_JOB)  # the retry fails too: no third
    assert [e.status for e in _final_exports(a["org"])] == ["failed", "failed"]
    _due_now(a["org"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}
    assert not Organization.objects.filter(pk=a["org"].pk).exists()


def test_a_stuck_final_export_is_retried_when_it_is_marked_failed(a, api):
    from core.models import Job, JobStatus

    cloud(a["org"])
    ask(api, a["users"]["admin"])
    Job.objects.filter(kind=org_lifecycle.EXPORT_JOB).update(status=JobStatus.ABANDONED)
    OrgExport.objects.filter(organization=a["org"]).update(created_at=timezone.now() - timedelta(hours=7))
    Organization.objects.filter(pk=a["org"].pk).update(deletion_requested_at=timezone.now() - timedelta(hours=8))
    assert org_lifecycle.expire_exports(timezone.now()) == {"expired": 0, "failed": 1}
    assert [e.status for e in _final_exports(a["org"])] == ["failed", "queued"]


def test_a_final_export_ready_lately_postpones_the_purge_for_its_download_window(a, api, settings):
    settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS = 1
    cloud(a["org"])
    ask(api, a["users"]["admin"])
    drain_jobs(org_lifecycle.EXPORT_JOB)
    _due_now(a["org"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}
    a["org"].refresh_from_db()
    final = _final_exports(a["org"])[-1]
    assert a["org"].deletion_due_at == final.finished_at + timedelta(days=org_lifecycle.EXPORT_DAYS)
    event = AuditEvent.objects.get(action="organization.deletion_postponed")
    assert event.changes["reason"] == "final_export_download_window"
    _age_final_export(a["org"])
    _due_now(a["org"])
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}


def test_the_purge_backstop_retries_a_failed_final_export_and_postpones(a, api):
    """A failure recorded without its retry (an older version, or the retry's own write failed): the purge retries it
    and gives the retry its download window."""
    cloud(a["org"])
    ask(api, a["users"]["admin"])
    OrgExport.objects.filter(organization=a["org"]).update(status="failed")
    _due_now(a["org"])
    before = timezone.now()
    assert org_lifecycle.purge_due(before) == {"purged": 0, "failed": 0}
    assert [e.status for e in _final_exports(a["org"])] == ["failed", "queued"]
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at >= before + timedelta(days=org_lifecycle.EXPORT_DAYS)
    assert AuditEvent.objects.get(action="organization.deletion_postponed").changes["reason"] == "final_export_retried"


# ---- a suspended organization's deletion waits --------------------------------------------


def _scheduled_and_exported(a, api, settings, grace_days=30):
    settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS = grace_days
    cloud(a["org"])
    assert ask(api, a["users"]["admin"]).status_code == 200
    drain_jobs(org_lifecycle.EXPORT_JOB)
    _age_final_export(a["org"], days=60)  # its download window is long past, so only the due date counts


def _as_if_suspended(org, ago: timedelta, due_in: timedelta) -> None:
    """As if the suspension had started `ago`, and the deletion were due `due_in` from now (negative: past)."""
    now = timezone.now()
    Organization.objects.filter(pk=org.pk).update(suspended_at=now - ago, deletion_due_at=now + due_in)


def test_a_suspended_organization_is_not_purged_and_resumes_with_its_remaining_grace(a, api, settings):
    from tests.test_platform import make_staff

    _scheduled_and_exported(a, api, settings)
    staff = make_staff()
    client = api(staff)
    response = client.post(f"platform/organizations/{a['org'].id}/suspend", {"reason": "Billing dispute"})
    assert response.status_code == 200
    deletion = response.json_body["deletion"]  # the console's suspend response shows the scheduled deletion
    assert deletion is not None and deletion["paused"] is True and deletion["due_at"]
    event = AuditEvent.objects.get(organization=a["org"], action="organization.suspended")
    assert event.changes["deletion_paused"] is True and event.changes["deletion_due_at"] == deletion["due_at"]

    # The suspension began 31 days ago with 30 days of grace left; the due date has passed meanwhile.
    _as_if_suspended(a["org"], ago=timedelta(days=31), due_in=timedelta(days=-1))
    past_due = timezone.now() + timedelta(days=1)
    assert org_lifecycle.purge_due(past_due) == {"purged": 0, "failed": 0}
    assert org_lifecycle.purge(a["org"].pk, past_due) is None
    assert Organization.objects.filter(pk=a["org"].pk).exists() and not OrganizationTombstone.objects.exists()

    before = timezone.now()
    response = client.post(f"platform/organizations/{a['org'].id}/unsuspend", {"reason": "Paid"})
    assert response.status_code == 200 and response.json_body["deletion"]["paused"] is False
    org = Organization.objects.get(pk=a["org"].pk)
    assert org.deletion_due_at is not None
    assert before + timedelta(days=30) <= org.deletion_due_at <= timezone.now() + timedelta(days=30)
    resumed = AuditEvent.objects.get(organization=org, action="organization.deletion_postponed")
    assert resumed.changes["reason"] == "suspension"
    assert AuditEvent.objects.get(organization=org, action="organization.unsuspended").changes["deletion_resumed"]

    assert org_lifecycle.purge_due(timezone.now() + timedelta(days=29)) == {"purged": 0, "failed": 0}
    assert org_lifecycle.purge_due(timezone.now() + timedelta(days=30, minutes=1)) == {"purged": 1, "failed": 0}
    assert not Organization.objects.filter(pk=a["org"].pk).exists()


def test_unsuspending_leaves_at_least_the_minimum_grace(a, api, settings):
    from catalog import platform_admin
    from tests.test_platform import make_staff

    _scheduled_and_exported(a, api, settings)
    staff = make_staff()
    platform_admin.suspend(staff, a["org"], {"reason": "Abuse review"})
    _as_if_suspended(a["org"], ago=timedelta(days=5), due_in=timedelta(days=-4, hours=-23))  # 1 hour was left
    before = timezone.now()
    org = platform_admin.unsuspend(staff, Organization.objects.get(pk=a["org"].pk), {"reason": "Cleared"})
    assert org.deletion_due_at is not None
    assert org.deletion_due_at >= before + timedelta(days=org_lifecycle.MIN_GRACE_DAYS)
    assert org_lifecycle.purge_due(timezone.now() + timedelta(hours=2)) == {"purged": 0, "failed": 0}


def test_suspending_without_a_scheduled_deletion_changes_no_date(a, api):
    from catalog import platform_admin
    from tests.test_platform import make_staff

    staff = make_staff()
    platform_admin.suspend(staff, a["org"], {"reason": "Abuse review"})
    assert "deletion_paused" not in AuditEvent.objects.get(action="organization.suspended").changes
    org = platform_admin.unsuspend(staff, Organization.objects.get(pk=a["org"].pk), {"reason": "Cleared"})
    assert org.deletion_due_at is None
    assert not AuditEvent.objects.filter(action="organization.deletion_postponed").exists()


# ---- what a purge leaves of its members ------------------------------------------------


def test_a_purge_erases_accounts_that_belonged_only_to_the_organization(a, b, api):
    from tests.test_platform import make_staff

    User = get_user_model()
    cloud(a["org"])
    only_here = list(a["users"].values())
    shared = b["users"]["editor"]
    Membership.objects.create(user=shared, organization=a["org"], role=Role.VIEWER)
    staff = make_staff(email="support@platform.test", org=a["org"], role=Role.EDITOR)
    UserIdentity.objects.create(user=a["users"]["viewer"], issuer="https://idp.orga.test", subject="v-1")
    # Staff impersonated a member once: the platform stream must not keep the member's address either.
    from tests.test_platform import start

    client = api(staff)
    assert start(client, a["org"], a["users"]["editor"]).status_code in (200, 201)
    assert client.delete("platform/impersonation").status_code in (200, 204)
    addresses = {u.email for u in only_here}
    signed_in, kept = Client(), Client()
    signed_in.force_login(a["users"]["viewer"])
    kept.force_login(shared)
    from django.contrib.sessions.models import Session

    Organization.objects.filter(pk=a["org"].pk).update(deletion_due_at=timezone.now() - timedelta(minutes=1))
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}

    # No auth_user row holds a former member's address, name or a usable password, but for the account with another
    # membership and the platform staff account.
    assert not User.objects.filter(email__in=addresses).exists()
    assert not User.objects.filter(username__in=addresses).exists()
    for user in only_here:
        user.refresh_from_db()
        assert user.email == user.username == f"retired+{user.pk}@invalid"
        assert (user.first_name, user.last_name) == (org_lifecycle.ERASED_NAME, "")
        assert user.is_active is False and not user.has_usable_password()
        assert not UserIdentity.objects.filter(user=user).exists()
    shared.refresh_from_db()
    assert shared.email == "editor@orgb.test" and shared.is_active and shared.has_usable_password()
    staff.refresh_from_db()
    assert staff.email == "support@platform.test" and staff.has_usable_password()
    # The address is free again: a new account with it is a different row.
    assert User.objects.filter(email="admin@orga.test").count() == 0
    # The erased account's sessions are deleted; the shared member's isn't.
    assert not Session.objects.filter(session_key=signed_in.session.session_key).exists()
    assert Session.objects.filter(session_key=kept.session.session_key).exists()
    # And nothing in the platform stream names a former member by address.
    from core.models import PlatformAuditEvent

    stored = json.dumps(list(PlatformAuditEvent.objects.values_list("details", "actor_email")))
    assert not any(address in stored for address in addresses)


def test_the_documents_say_how_long_backups_keep_a_purged_organization():
    """The operator and the customer can read how long installation backups keep a purged organization."""
    root = Path(__file__).resolve().parents[3]
    for doc in ("docs/CLOUD.md", "docs/04-development/platform-admin-runbook.md"):
        text = " ".join((root / doc).read_text(encoding="utf-8").split())
        assert org_lifecycle.BACKUP_RETENTION_SENTENCE in text, doc


def test_the_deletion_state_says_how_long_backups_keep_the_data(a, api, settings):
    settings.BACKUP_KEEP_DAILY, settings.BACKUP_KEEP_WEEKLY = 14, 8
    cloud(a["org"])
    body = api(a["users"]["admin"]).get("organization/deletion").json_body
    assert body["backups_kept_days"] == 56
    settings.BACKUP_KEEP_DAILY, settings.BACKUP_KEEP_WEEKLY = 30, 2
    assert api(a["users"]["admin"]).get("organization/deletion").json_body["backups_kept_days"] == 30


# ---- purges that keep failing don't block the queue -----------------------------------------


def test_failing_purges_do_not_block_the_queue_and_repeated_failure_fails_the_schedule(db, monkeypatch):
    now = timezone.now()
    orgs = [Organization.objects.create(name=f"Due {n}") for n in range(6)]
    for n, org in enumerate(orgs):  # the first five are the oldest due
        Organization.objects.filter(pk=org.pk).update(deletion_due_at=now - timedelta(hours=10 - n))
    broken = {org.pk for org in orgs[:5]}
    real_purge = org_lifecycle.purge

    def flaky(org_id, now=None):
        if org_id in broken:
            raise RuntimeError("constraint check failed")
        return real_purge(org_id, now)

    monkeypatch.setattr(org_lifecycle, "purge", flaky)
    assert org_lifecycle.purge_due(now) == {"purged": 0, "failed": 5}
    assert Organization.objects.filter(pk=orgs[5].pk).exists()  # the sixth had no place in the first run
    assert {o.purge_failures for o in Organization.objects.filter(pk__in=broken)} == {1}
    second = org_lifecycle.purge_due(now + timedelta(minutes=15))
    assert second["purged"] == 1 and not Organization.objects.filter(pk=orgs[5].pk).exists()

    # The platform console shows the failures.
    from tests.test_platform import make_staff

    detail = Client()
    detail.force_login(make_staff())
    body = detail.get(f"/api/v1/platform/organizations/{orgs[0].pk}").json()
    assert body["deletion"]["purge_failures"] == 2 and body["deletion"]["purge_attempted_at"]

    # A third failure in a row fails the schedule's run (after its work), so it shows as failing.
    with pytest.raises(org_lifecycle.PurgeKeepsFailing) as raised:
        org_lifecycle.purge_due(now + timedelta(minutes=30))
    assert str(orgs[0].pk) not in str(raised.value)  # a count only; the ids are in the worker log
    broken.clear()  # fixed: the next run purges them and the schedule succeeds again
    assert org_lifecycle.purge_due(now + timedelta(minutes=45)) == {"purged": 5, "failed": 0}


def test_cancelling_and_asking_again_starts_a_clean_purge_record(a, api, monkeypatch):
    """Three failures, then cancel and ask again: the schedule doesn't fail before the new due date, and
    the first failure after it isn't counted as the fourth."""
    cloud(a["org"])
    admin = a["users"]["admin"]
    assert ask(api, admin).status_code == 200
    drain_jobs(org_lifecycle.EXPORT_JOB)
    _age_final_export(a["org"], days=60)
    _due_now(a["org"])

    def broken(org_id, now=None):
        raise RuntimeError("x")

    monkeypatch.setattr(org_lifecycle, "purge", broken)
    now = timezone.now()
    org_lifecycle.purge_due(now)
    org_lifecycle.purge_due(now + timedelta(minutes=15))
    with pytest.raises(org_lifecycle.PurgeKeepsFailing):
        org_lifecycle.purge_due(now + timedelta(minutes=30))

    assert api(admin).delete("organization/deletion").status_code == 200
    org = Organization.objects.get(pk=a["org"].pk)
    assert (org.purge_failures, org.purge_attempted_at) == (0, None)
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}  # nothing due: no alert
    assert ask(api, admin).status_code == 200
    org = Organization.objects.get(pk=a["org"].pk)
    assert (org.purge_failures, org.purge_attempted_at) == (0, None)
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}  # not due yet
    assert org.deletion_due_at is not None
    after = org.deletion_due_at + timedelta(minutes=1)
    assert org_lifecycle.purge_due(after) == {"purged": 0, "failed": 1}  # the first failure, not the fourth


def test_a_suspended_organizations_failures_do_not_fail_the_schedule(a, monkeypatch):
    org = a["org"]
    Organization.objects.filter(pk=org.pk).update(
        deletion_due_at=timezone.now() - timedelta(hours=1),
        purge_failures=org_lifecycle.PURGE_ALERT_AFTER,
        suspended_at=timezone.now(),
    )
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 0, "failed": 0}


def test_the_schedule_shows_failing_while_a_purge_keeps_failing(db, monkeypatch):
    from core import health

    org = Organization.objects.create(name="Stuck")
    Organization.objects.filter(pk=org.pk).update(
        deletion_due_at=timezone.now() - timedelta(hours=1), purge_failures=org_lifecycle.PURGE_ALERT_AFTER - 1
    )

    def broken(org_id, now=None):
        raise RuntimeError("x")

    monkeypatch.setattr(org_lifecycle, "purge", broken)
    now = timezone.now()
    jobs.sync_schedules(now)
    Schedule.objects.filter(name="org.purge_deleted").update(next_due=now - timedelta(minutes=1))
    outcome, _ = jobs.run_schedule(jobs.SCHEDULES["org.purge_deleted"], now)
    assert outcome == "failed"
    check = next(c for c in health.schedule_checks(now) if c["name"] == "org.purge_deleted")
    assert check["state"] == "failing" and check["last_error"] == "PurgeKeepsFailing"
