"""An organization's data export and self-service deletion. Routes in catalog/lifecycle_views.py.

Export (capability `org.export`, every edition). An Admin asks for one; the `org.export` job writes a zip under
MEDIA_ROOT (`exports/<organization id>/<export id>.zip`):

    manifest.json                      format, version, the organization, when, rows per model, attachment files
    organization.json                  the organization's own row
    users.jsonl                        the members' accounts: id, email, name, active, joined, last sign-in
    data/<app>.<Model>.jsonl           one JSON object per row, for every model with an organization column
    attachments/<attachment id>/<name> the attachment files

Secrets are never written: any `*_ciphertext` column and the key id beside it, token hashes and their prefixes,
password hashes, worker lease tokens, and the users' sign-in identities. EXCLUDED_MODELS are worker bookkeeping. An
export is downloadable by Admins for EXPORT_DAYS; the `org.exports_expire` schedule removes the file then, and marks
an export whose job died as failed.

Deletion (capability `org.self_delete`, cloud). An Admin asks with their password (or, for an account that signs in
only with single sign-on, a sign-in within REAUTH_WINDOW) and the organization's slug typed out. That schedules the
deletion SOFTWARDEN_ORG_DELETION_GRACE_DAYS ahead and queues a final export; members see it in `auth/me` and an Admin
can cancel until then. Afterwards the `org.purge_deleted` schedule removes every row of the organization (every model
with an organization column, its audit log included, then the organization) in one transaction and its files after
that. Accounts left with no organization are deactivated, not deleted; accounts that belong elsewhere lose sign-in
identities only this organization configured. An OrganizationTombstone outside any organization is the platform's
trace.

Tenant backups (capability `org.tenant_backups`, the cloud edition, and the reserved `enterprise`). The `org.tenant_backups` schedule
(nightly) queues a `backup`-purpose export for every organization that has the capability and is neither suspended
nor scheduled for deletion. Each job waits a fixed offset from the organization's id hash, spread over BACKUP_SPREAD,
so a large installation doesn't build every zip at the same minute. A backup has no expiry date: the newest
SOFTWARDEN_TENANT_BACKUP_RETENTION ready backups of an organization are kept (and as many failed ones, so failures
stay visible), and older rows and files are removed when a backup becomes ready and by the schedule. Admins list and
download backups with the exports; a download while impersonating is refused (core/impersonation_routes.py).
Restoring one organization from a backup isn't built: docs/04-development/platform-admin-runbook.md says how to
restore a tenant from the installation backup instead.

An export made by the operator on the organization's request (`operator`, catalog/platform_admin.request_export) is
like an Admin's: listed to the organization's Admins and downloadable only by them.

Every write validates, scopes and audits in one transaction; the export job's own status changes are worker
bookkeeping and only its outcome is audited (and a backup pruning, as organization.backups_pruned)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import zipfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, models, transaction
from django.db.models import F, Value
from django.utils import timezone
from rest_framework import exceptions

from core import jobs
from core.capabilities import ORG_EXPORT, ORG_SELF_DELETE, TENANT_BACKUPS, has_capability, require_capability
from core.models import (
    AUDIT_PURGE_SETTING,
    ExportPurpose,
    ExportStatus,
    Job,
    JobStatus,
    Membership,
    Organization,
    OrganizationTombstone,
    OrgExport,
    UserIdentity,
    active_organizations,
)
from core.problems import Conflict, FieldErrors
from core.request_context import forget_organization

from .services import audit, audit_account

log = logging.getLogger("softwarden.lifecycle")

EXPORT_JOB = "org.export"
EXPORT_LEASE = timedelta(minutes=30)
EXPORT_DAYS = 7
# Requesting and downloading the full export need a signed-in session (catalog/lifecycle_views.py), never an
# API token: the file is the whole organization, audit log and member emails included.
SESSION_ONLY = "Request and download the organization's full export from a signed-in session, not with an API token."
EXPORT_ROOT = "exports"
FORMAT = "softwarden-organization-export"
FORMAT_VERSION = 1
CHUNK = 1024 * 1024
REAUTH_WINDOW = timedelta(minutes=15)
PURGES_PER_RUN = 5
PURGE_ALERT_AFTER = 3  # consecutive failed purges of one organization before the schedule fails (purge_due)
STUCK_AFTER = timedelta(hours=6)  # a queued or running export with no live job after this is failed
BACKUP_SCHEDULE = "org.tenant_backups"
BACKUP_SPREAD = timedelta(hours=4)  # backups start at an offset in this window, by the organization's id hash

# Worker bookkeeping, not the organization's data.
# Platform bookkeeping (platform admin console): usage snapshots and capability overrides set by the operator
# aren't the organization's data either (nor limit overrides, limits); the purge still removes them with it.
EXCLUDED_MODELS = {
    "core.Job",
    "core.OrgExport",
    "core.OrganizationUsage",
    "core.CapabilityOverride",
    "core.OrganizationLimitOverride",
    # When the organization had invitation emails sent through the installation's mail server (its send budget).
    "core.InstallationEmail",
    # Licence waste: who holds a licence and when they last signed in. Personal data derived from Entra that
    # the next licence usage run reads again; kept out of exports and tenant backups so it doesn't spread
    # (privacy). The purge still removes it with the organization.
    "discovery.LicensedUser",
}
# Columns never exported, whatever model they are on (besides every *_ciphertext column and its *_key_id). `sketch_key`
# is the shadow IT source's key for hashing user IDs into its sketches (discovery.ShadowItState): without it
# an exported sketch can't be tested for a known user.
SECRET_COLUMNS = {"token_hash", "password", "password_hash", "lease_token", "claim_token", "sketch_key"}

REAUTH = "/problems/reauthentication-required"


class OrganizationGone(Exception):
    """The export's organization (or the export row) was removed while the export was being built."""


class ReauthenticationRequired(exceptions.PermissionDenied):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.extra = {"type": REAUTH}


# ---- which models and columns -------------------------------------------------------------------------------------


def _label(model) -> str:
    return model._meta.label


def tenant_models() -> list[type[models.Model]]:
    """Every concrete model with an `organization` foreign key to Organization, by label: what an organization owns."""
    found = []
    for model in apps.get_models():
        meta = model._meta
        if meta.proxy or not meta.managed:
            continue
        field = next((f for f in meta.concrete_fields if f.name == "organization"), None)
        if field is not None and field.is_relation and field.related_model is Organization:
            found.append(model)
    return sorted(found, key=_label)


def org_column(model) -> str:
    field = next(f for f in model._meta.concrete_fields if f.name == "organization")
    return str(field.column)


def exported_columns(model) -> list[str]:
    """The model's columns (attnames) an export carries: everything but secrets (see the module docstring)."""
    fields = list(model._meta.concrete_fields)
    names = {f.name for f in fields}
    slots = {n[: -len("_ciphertext")] for n in names if n.endswith("_ciphertext")}
    keep = []
    for field in fields:
        name = field.name
        if name.endswith("_ciphertext") or name in SECRET_COLUMNS:
            continue
        if name.endswith("_key_id") and name[: -len("_key_id")] in slots:
            continue
        if name == "prefix" and "token_hash" in names:
            continue
        keep.append(field.attname)
    return keep


def _json(row: dict[str, Any]) -> str:
    return json.dumps(row, cls=_Encoder, ensure_ascii=False, sort_keys=True)


class _Encoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, bytes | memoryview):
            return bytes(o).hex()
        return super().default(o)


# ---- export -------------------------------------------------------------------------------------------------------


def _path(storage_name: str) -> Path:
    return Path(settings.MEDIA_ROOT) / storage_name


def _active(org, exclude: tuple[str, ...] = ()) -> OrgExport | None:
    """The organization's oldest export queued or being built, of any purpose but `exclude`."""
    return (
        OrgExport.objects.filter(organization=org, status__in=[ExportStatus.QUEUED, ExportStatus.RUNNING])
        .exclude(purpose__in=exclude)
        .order_by("created_at")
        .first()
    )


def _queue(org, actor, purpose: str) -> OrgExport:
    export = OrgExport.objects.create(organization=org, requested_by=actor, purpose=purpose)
    job = jobs.enqueue(EXPORT_JOB, {"export_id": str(export.id)}, organization=org)
    export.job_id = job.id
    export.save(update_fields=["job_id", "updated_at"])
    return export


@transaction.atomic
def request_export(org, actor, purpose: str = ExportPurpose.EXPORT) -> OrgExport:
    """Queue an export of everything the organization holds: an Admin's (`export`), or the operator's on the
    organization's request (`operator`, catalog/platform_admin.request_export). One at a time: 409 while another is
    queued or running (a nightly backup in flight doesn't count), and 409 once its deletion is due (the purge is about
    to run; the final export was made when deletion was asked for). 404 when the organization was removed meanwhile."""
    locked = Organization.objects.select_for_update().filter(pk=org.pk).first()
    if locked is None:
        raise exceptions.NotFound("This organization no longer exists.")
    if locked.deletion_due_at is not None and locked.deletion_due_at <= timezone.now():
        raise Conflict("This organization is being deleted; no new export can be made.")
    if _active(org, exclude=(ExportPurpose.BACKUP,)) is not None:
        raise Conflict("An export is already being prepared. Wait for it to finish, then download it.")
    export = _queue(org, actor, purpose)
    audit(org, actor, "organization.export_requested", export, {"purpose": export.purpose})
    return export


def visible_purposes(org) -> list[str]:
    """Which exports the organization's Admins list and download: one the operator made on their request always (they
    asked for it); nightly backups with org.tenant_backups or org.export; their own with org.export; the final export
    before deletion with org.export or org.self_delete (the deletion banner links it, and it is the last copy). So
    revoking org.export from an organization doesn't hide what the operator, the nightly backup or its deletion made
    for it."""
    purposes: list[str] = [ExportPurpose.OPERATOR]
    exports = has_capability(org, ORG_EXPORT)
    if exports or has_capability(org, TENANT_BACKUPS):
        purposes.append(ExportPurpose.BACKUP)
    if exports:
        purposes.append(ExportPurpose.EXPORT)
    if exports or has_capability(org, ORG_SELF_DELETE):
        purposes.append(ExportPurpose.DELETION)
    return purposes


def downloadable(export: OrgExport, now=None) -> bool:
    """Ready, with its file, and not expired. A backup has no expiry: it is kept until newer backups replace it."""
    now = now or timezone.now()
    if export.status != ExportStatus.READY or not export.storage_name:
        return False
    if export.expires_at is None:
        return export.purpose == ExportPurpose.BACKUP
    return export.expires_at > now


def download_url(export: OrgExport) -> str | None:
    return f"/api/v1/organization/exports/{export.id}/download" if downloadable(export) else None


@transaction.atomic
def record_download(org, actor, export: OrgExport) -> None:
    """Who downloaded an export is audited: it is all of the organization's data in one file."""
    audit(org, actor, "organization.export_downloaded", export, {"purpose": export.purpose})


def open_export(export: OrgExport):
    """The zip opened for reading, or None when it is gone (expired, or removed from storage)."""
    if not downloadable(export):
        return None
    try:
        return _path(export.storage_name).open("rb")
    except FileNotFoundError:
        log.warning("export file missing export=%s", export.pk)
        return None


def _write_rows(archive: zipfile.ZipFile, name: str, rows) -> int:
    count = 0
    with archive.open(name, "w") as stream:
        for row in rows:
            stream.write((_json(row) + "\n").encode("utf-8"))
            count += 1
    return count


def _members(org):
    user_ids = Membership.objects.filter(organization=org).values_list("user_id", flat=True)
    return (
        get_user_model()
        .objects.filter(pk__in=user_ids)
        .order_by("pk")
        .values("id", "email", "first_name", "last_name", "is_active", "date_joined", "last_login")
    )


def _attachments(archive: zipfile.ZipFile, org, renew) -> tuple[int, list[str]]:
    from .attachments import clean_filename
    from .models import Attachment, AttachmentKind

    written, missing = 0, []
    rows = Attachment.objects.filter(organization=org, kind=AttachmentKind.FILE).exclude(storage_name="")
    for attachment in rows.order_by("pk").iterator():
        name = clean_filename(attachment.filename) or "file"
        try:
            source = default_storage.open(attachment.storage_name, "rb")
        except FileNotFoundError:
            missing.append(str(attachment.pk))
            continue
        with source, archive.open(f"attachments/{attachment.pk}/{name}", "w") as target:
            shutil.copyfileobj(source, target, CHUNK)
        written += 1
        if written % 50 == 0:
            renew()
    return written, missing


def build(export: OrgExport, renew=lambda: None) -> dict[str, Any]:
    """Write the zip for `export` and return its counts. `renew` is called between models (the job's lease)."""
    org = export.organization
    storage_name = f"{EXPORT_ROOT}/{org.pk}/{export.pk}.zip"
    target = _path(storage_name)
    if not OrgExport.objects.filter(pk=export.pk, organization_id=org.pk).exists():
        raise OrganizationGone()  # purged meanwhile: don't recreate its folder
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            own = Organization.objects.filter(pk=org.pk).values(*exported_columns(Organization)).get()
            archive.writestr("organization.json", _json(own))
            counts["users"] = _write_rows(archive, "users.jsonl", _members(org).iterator())
            for model in tenant_models():
                label = _label(model)
                if label in EXCLUDED_MODELS:
                    continue
                rows = model._base_manager.filter(organization=org).order_by("pk").values(*exported_columns(model))
                counts[label] = _write_rows(archive, f"data/{label}.jsonl", rows.iterator(chunk_size=2000))
                renew()
            files, missing = _attachments(archive, org, renew)
            manifest = {
                "format": FORMAT,
                "version": FORMAT_VERSION,
                "organization": {"id": str(org.pk), "name": org.name, "slug": org.slug},
                "export_id": str(export.pk),
                "created_at": timezone.now(),
                "rows": counts,
                "attachment_files": files,
                "missing_attachment_files": missing,
                "excluded": "Credential ciphertexts and their key ids, token hashes and prefixes, password hashes, "
                "worker leases, sign-in identities, the shadow IT sketch keys, licensed users' sign-in activity, jobs and "
                "exports are never exported.",
            }
            archive.writestr("manifest.json", json.dumps(manifest, cls=_Encoder, indent=2, sort_keys=True))
        os.chmod(partial, 0o600)
        digest = hashlib.sha256()
        with partial.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "storage_name": storage_name,
        "size": target.stat().st_size,
        "sha256": digest.hexdigest(),
        "counts": {**counts, "attachment_files": files},
    }


def run_export_job(job: Job, ctx) -> dict[str, Any]:
    """The `org.export` job: build the zip, then mark the export ready (or failed) and audit the outcome."""
    export = None
    if job.organization_id is not None:
        export = (
            OrgExport.objects.select_related("organization")
            .filter(pk=job.payload.get("export_id"), organization_id=job.organization_id)
            .first()
        )
    if export is None:
        raise jobs.PermanentError("The export this job was for no longer exists.")
    if export.status != ExportStatus.QUEUED:
        return {"skipped": export.status}
    OrgExport.objects.filter(pk=export.pk).update(status=ExportStatus.RUNNING, updated_at=timezone.now())
    try:
        result = build(export, ctx.renew)
    except jobs.LeaseLost:
        raise
    except OrganizationGone:
        return {"discarded": "the organization was deleted"}
    except Exception as error:
        try:
            if not _finish(export, ExportStatus.FAILED, error=jobs.safe_error(error)[:300]):
                _remove_empty(_path(f"{EXPORT_ROOT}/{export.organization_id}"))  # purged meanwhile
            elif export.purpose == ExportPurpose.DELETION:
                retry_final_export(export.organization_id)  # now, early in the grace period, not at purge time
        except Exception as finish_error:  # recording the failure failed too: the original error is the one to raise
            log.warning(
                "export failure: recording or retry failed export=%s error=%s", export.pk, type(finish_error).__name__
            )
        raise
    if not _finish(export, ExportStatus.READY, **result):
        # The organization was purged while the zip was written: no row tracks this file any more, so it goes now
        # rather than staying in MEDIA_ROOT (and every backup) as an untracked copy of all its data.
        _path(result["storage_name"]).unlink(missing_ok=True)
        _remove_empty(_path(result["storage_name"]).parent)
        return {"discarded": "the organization was deleted"}
    if export.purpose == ExportPurpose.BACKUP:
        prune_backups(export.organization_id)
    return {"export_id": str(export.pk), "size": result["size"]}


def _remove_empty(folder: Path) -> None:
    try:
        folder.rmdir()
    except OSError:
        pass  # not empty, or already gone


@transaction.atomic
def _finish(export: OrgExport, status: str, **fields: Any) -> bool:
    """Record the export's outcome and audit it. False, changing nothing, when the export row has gone (its
    organization was purged meanwhile)."""
    found = OrgExport.objects.select_for_update().select_related("organization").filter(pk=export.pk).first()
    if found is None:
        return False
    export = found
    now = timezone.now()
    export.status = status
    export.finished_at = now
    if status == ExportStatus.READY and export.purpose != ExportPurpose.BACKUP:  # backups are kept by count
        export.expires_at = now + timedelta(days=EXPORT_DAYS)
    for key, value in fields.items():
        setattr(export, key, value)
    export.save()
    action = "organization.export_ready" if status == ExportStatus.READY else "organization.export_failed"
    changes: dict[str, Any] = {"purpose": export.purpose}
    if status == ExportStatus.READY:
        changes["size"] = export.size
    audit(export.organization, None, action, export, changes)
    return True


def expire_exports(now) -> dict[str, int]:
    """The `org.exports_expire` schedule: remove expired export files, and fail exports whose job is gone."""
    expired = failed = 0
    for export in OrgExport.objects.filter(status=ExportStatus.READY, expires_at__lte=now).select_related(
        "organization"
    ):
        with transaction.atomic():
            row = OrgExport.objects.select_for_update().filter(pk=export.pk, status=ExportStatus.READY).first()
            if row is None:
                continue
            name = row.storage_name
            row.status = ExportStatus.EXPIRED
            row.storage_name = ""
            row.save(update_fields=["status", "storage_name", "updated_at"])
            audit(export.organization, None, "organization.export_expired", row, {"purpose": row.purpose})
        if name:
            _path(name).unlink(missing_ok=True)
        expired += 1
    live_jobs = Job.objects.filter(status__in=[JobStatus.QUEUED, JobStatus.RUNNING]).values_list("id", flat=True)
    stuck = OrgExport.objects.filter(
        status__in=[ExportStatus.QUEUED, ExportStatus.RUNNING], created_at__lte=now - STUCK_AFTER
    ).exclude(job_id__in=live_jobs)
    for export in stuck:
        if _finish(export, ExportStatus.FAILED, error="The export stopped before it finished. Ask for a new one."):
            failed += 1
            if export.purpose == ExportPurpose.DELETION:
                retry_final_export(export.organization_id)
    return {"expired": expired, "failed": failed}


# ---- tenant backups -----------------------------------------------------------------------------------------------


def backup_retention() -> int:
    return max(1, int(settings.SOFTWARDEN_TENANT_BACKUP_RETENTION))


def backup_offset(org_id) -> timedelta:
    """Where in BACKUP_SPREAD this organization's backup starts: fixed per organization, spread evenly by its id."""
    minutes = int(BACKUP_SPREAD.total_seconds() // 60)
    return timedelta(minutes=int(hashlib.sha256(str(org_id).encode()).hexdigest(), 16) % minutes)


def backup_candidates():
    """Organizations a nightly backup is for: not suspended, not scheduled for deletion, with the capability."""
    rows = active_organizations().filter(deletion_due_at__isnull=True).order_by("id")
    return [org for org in rows.iterator() if has_capability(org, TENANT_BACKUPS)]


@transaction.atomic
def queue_backup(org, now) -> OrgExport | None:
    """Queue tonight's backup of one organization, starting at its offset. None when one is still queued or being
    built (a slow one isn't doubled) or the organization went meanwhile."""
    locked = Organization.objects.select_for_update().filter(pk=org.pk, deletion_due_at__isnull=True).first()
    if locked is None or locked.suspended_at is not None:
        return None
    busy = OrgExport.objects.filter(
        organization=locked, purpose=ExportPurpose.BACKUP, status__in=[ExportStatus.QUEUED, ExportStatus.RUNNING]
    )
    if busy.exists():
        return None
    export = OrgExport.objects.create(organization=locked, requested_by=None, purpose=ExportPurpose.BACKUP)
    job = jobs.enqueue(
        EXPORT_JOB, {"export_id": str(export.id)}, organization=locked, run_after=now + backup_offset(locked.pk)
    )
    export.job_id = job.id
    export.save(update_fields=["job_id", "updated_at"])
    return export


def prune_backups(org_id, keep: int | None = None) -> int:
    """Keep the organization's newest `keep` ready backups (SOFTWARDEN_TENANT_BACKUP_RETENTION) and as many failed or
    expired ones; remove the older rows, audited as organization.backups_pruned, then their files. Returns how many
    rows went."""
    keep = keep or backup_retention()
    names: list[str] = []
    with transaction.atomic():
        org = Organization.objects.select_for_update().filter(pk=org_id).first()
        if org is None:
            return 0
        backups = OrgExport.objects.filter(organization=org, purpose=ExportPurpose.BACKUP).order_by("-created_at")
        ready = list(backups.filter(status=ExportStatus.READY).values_list("pk", "storage_name")[keep:])
        other = list(
            backups.filter(status__in=[ExportStatus.FAILED, ExportStatus.EXPIRED]).values_list("pk", "storage_name")[
                keep:
            ]
        )
        doomed = ready + other
        if not doomed:
            return 0
        names = [name for _, name in doomed if name]
        OrgExport.objects.filter(organization=org, pk__in=[pk for pk, _ in doomed]).delete()
        audit(org, None, "organization.backups_pruned", None, {"removed": len(doomed), "kept": keep})
    for name in names:
        _path(name).unlink(missing_ok=True)
    return len(doomed)


def queue_backups(now) -> dict[str, int]:
    """The `org.tenant_backups` schedule (nightly): queue each eligible organization's backup, staggered over
    BACKUP_SPREAD, then apply the retention to every organization that has backups. One organization's failure is
    logged (its id and the exception class) and the others go on."""
    queued = skipped = failed = pruned = 0
    for org in backup_candidates():
        try:
            if queue_backup(org, now) is not None:
                queued += 1
            else:
                skipped += 1
        except Exception as error:  # one organization's failure mustn't hold up the others
            failed += 1
            log.warning("tenant backup not queued org=%s error=%s", org.pk, type(error).__name__)
    with_backups = (
        OrgExport.objects.filter(purpose=ExportPurpose.BACKUP).values_list("organization_id", flat=True).distinct()
    )
    for org_id in sorted(set(with_backups), key=str):
        pruned += prune_backups(org_id)
    return {"queued": queued, "skipped": skipped, "failed": failed, "pruned": pruned}


# ---- deletion -----------------------------------------------------------------------------------------------------


MIN_GRACE_DAYS = 1  # time for the final export to be built and downloaded, and for a mistake to be cancelled
ERASED_NAME = "Former member"  # an erased account's name (_release_accounts)
# What docs/CLOUD.md and the platform runbook say about backups after a purge (a test reads both
# documents for it). GET organization/deletion's `backups_kept_days` gives the SPA the number.
BACKUP_RETENTION_SENTENCE = (
    "A purged organization's data stays in the installation backups taken before the purge until those sets are "
    "pruned: with the default retention of 14 daily and 8 weekly sets, for up to eight weeks."
)


def backups_kept_days() -> int:
    """How many days the installation's backups keep a set (the oldest weekly or daily one, core/backup.py
    retention): how long a purged organization's data can remain in them."""
    return max(int(settings.BACKUP_KEEP_DAILY), int(settings.BACKUP_KEEP_WEEKLY) * 7)


def grace() -> timedelta:
    return timedelta(days=max(MIN_GRACE_DAYS, int(settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS)))


def reauthenticate(user, password: Any, authenticated_at=None, now=None, *, counted_by_caller: bool = False) -> None:
    """The person asking is the account's owner now: its password, or for an account with no usable password (single
    sign-on only) this session's own sign-in within REAUTH_WINDOW (`authenticated_at`, core/access.authenticated_at;
    the account's `last_login` would count a sign-in from another browser). 403 /problems/reauthentication-required
    otherwise.

    The password check counts with the account's sign-in attempts and audits a failure (admin_services.check_current_password), in its own transaction: call this outside any transaction. Only a caller
    that already counts the attempt around its whole request sets `counted_by_caller` (the platform's impersonation
    start, catalog/platform_views.ImpersonationView, which counts the staff account's second factor with it)."""
    if user.has_usable_password():
        if counted_by_caller:
            matched = isinstance(password, str) and bool(password) and user.check_password(password)
        else:
            from .admin_services import check_current_password  # admin_services sits above this module

            matched = check_current_password(user, password, "reauthentication")
        if not matched:
            raise ReauthenticationRequired("Enter your current password to confirm.")
        return
    now = now or timezone.now()
    if authenticated_at is None or authenticated_at < now - REAUTH_WINDOW:
        raise ReauthenticationRequired(
            "You sign in with single sign-on: sign out and sign in again, then ask within 15 minutes."
        )


def deletion_export(org) -> OrgExport | None:
    return OrgExport.objects.filter(organization=org, purpose=ExportPurpose.DELETION).order_by("-created_at").first()


def request_deletion(
    org, actor, data: dict[str, Any], authenticated_at=None, then: Callable[[Organization], None] | None = None
) -> Organization:
    """Schedule the organization's deletion after the grace period and queue its final export. The checks that need
    no lock come first, then the re-authentication in its own transaction (a failed password stays counted and
    audited), then the write in one transaction under the organization's lock, which checks again. `then`
    (the view's response) runs inside that transaction, so a response that fails to render writes nothing."""
    require_capability(org, ORG_SELF_DELETE)
    _check_deletion_request(Organization.objects.get(pk=org.pk), data)
    reauthenticate(actor, data.get("password"), authenticated_at)
    with transaction.atomic():
        org = _schedule_deletion(org, actor, data)
        if then is not None:
            then(org)
        return org


def _check_deletion_request(org, data: dict[str, Any]) -> None:
    if org.deletion_due_at is not None:
        raise Conflict("This organization's deletion is already scheduled.")
    if data.get("confirm") != org.slug:
        raise FieldErrors({"confirm": [f"Type the organization's slug, {org.slug}, exactly to confirm."]})


def _schedule_deletion(org, actor, data: dict[str, Any]) -> Organization:
    org = Organization.objects.select_for_update().get(pk=org.pk)
    _check_deletion_request(org, data)  # again under the lock: another Admin may have asked meanwhile
    now = timezone.now()
    org.deletion_requested_at = now
    org.deletion_due_at = now + grace()
    org.deletion_requested_by = actor
    org.purge_attempted_at, org.purge_failures = None, 0  # a new deletion starts with a clean purge record
    org.save(
        update_fields=[
            "deletion_requested_at",
            "deletion_due_at",
            "deletion_requested_by",
            "purge_attempted_at",
            "purge_failures",
        ]
    )
    forget_organization(org.pk)
    export = _queue(org, actor, ExportPurpose.DELETION)
    changes = {"due_at": org.deletion_due_at.isoformat(), "export": str(export.id)}
    audit(org, actor, "organization.deletion_requested", org, changes)
    return org


@transaction.atomic
def cancel_deletion(org, actor) -> Organization:
    org = Organization.objects.select_for_update().get(pk=org.pk)
    if org.deletion_due_at is None:
        raise Conflict("This organization isn't scheduled for deletion.")
    due = org.deletion_due_at
    org.deletion_requested_at = org.deletion_due_at = org.deletion_requested_by = None
    org.purge_attempted_at, org.purge_failures = None, 0  # nothing to purge any more
    org.save(
        update_fields=[
            "deletion_requested_at",
            "deletion_due_at",
            "deletion_requested_by",
            "purge_attempted_at",
            "purge_failures",
        ]
    )
    forget_organization(org.pk)
    audit(org, actor, "organization.deletion_cancelled", org, {"due_at": due.isoformat()})
    return org


def _files(org) -> list[str]:
    from .models import Attachment, RetiredFile

    names = set(
        Attachment.objects.filter(organization=org).exclude(storage_name="").values_list("storage_name", flat=True)
    )
    names |= set(RetiredFile.objects.filter(organization=org).values_list("name", flat=True))
    names |= set(
        OrgExport.objects.filter(organization=org).exclude(storage_name="").values_list("storage_name", flat=True)
    )
    return sorted(names)


def _remove_files(org_id, names: list[str]) -> None:
    for name in names:
        try:
            default_storage.delete(name)
        except OSError:
            log.warning("purge could not remove a file org=%s", org_id)
    root = Path(settings.MEDIA_ROOT)
    for folder in (root / "attachments" / str(org_id), root / EXPORT_ROOT / str(org_id)):
        shutil.rmtree(folder, ignore_errors=True)


def _release_accounts(org_id, user_ids: list[int]) -> None:
    """After the organization's rows are gone: erase accounts with no organization left, and unlink identities no
    remaining organization of a surviving account configures (as leaving one does).

    An erased account keeps its row, inactive, so the id another organization's history or
    the tombstone may name still resolves, but nothing personal: its username and email become
    `retired+<id>@invalid` (as invitations.release_address does), its name "Former member", its password unusable
    and its sign-in identities and sessions are removed. Its old address is free for a new account at once."""
    from core import sessions

    from .invitations import configured_issuers, retired_address

    users = get_user_model().objects.filter(pk__in=user_ids)
    # Platform staff keep their account: it signs in to the console without a membership.
    orphans = users.exclude(membership__isnull=False).exclude(platform_staff__isnull=False)
    orphan_ids = list(orphans.values_list("pk", flat=True))
    for user in get_user_model().objects.select_for_update().filter(pk__in=orphan_ids).order_by("pk"):
        user.username = user.email = retired_address(user.pk)
        user.first_name, user.last_name = ERASED_NAME, ""
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["username", "email", "first_name", "last_name", "is_active", "password"])
        UserIdentity.objects.filter(user=user).delete()
    sessions.delete_for_users(orphan_ids)
    for user in users.filter(membership__isnull=False).distinct():
        remaining = Membership.objects.filter(user=user).values_list("organization_id", flat=True)
        rows = UserIdentity.objects.filter(user=user).exclude(issuer__in=configured_issuers(remaining))
        issuers = sorted(set(rows.values_list("issuer", flat=True)))
        if issuers:
            rows.delete()
            changes = {"user": user.id, "method": "sso", "issuers": issuers, "reason": "organization_deleted"}
            audit_account(user, None, "user.identity_unlinked", changes)


def _since_request(org):
    """The organization's exports made since its deletion was asked for (all of them when that time isn't known)."""
    rows = OrgExport.objects.filter(organization=org)
    if org.deletion_requested_at is not None:
        rows = rows.filter(created_at__gte=org.deletion_requested_at)
    return rows


def _postpone(org, until, reason: str) -> None:
    """Move a scheduled deletion later (never earlier), audited as organization.deletion_postponed."""
    if org.deletion_due_at is None or org.deletion_due_at >= until:
        return
    before = org.deletion_due_at
    org.deletion_due_at = until
    org.save(update_fields=["deletion_due_at"])
    forget_organization(org.pk)
    changes = {"due_at": [before.isoformat(), until.isoformat()], "reason": reason}
    audit(org, None, "organization.deletion_postponed", org, changes)


@transaction.atomic
def retry_final_export(org_id, *, postpone: bool = False, now=None) -> bool:
    """Queue one fresh deletion export when the final export failed and nothing since the deletion request succeeded
    or is in flight; once only (audited as organization.deletion_export_retried). Called when the final export fails
    (the export job, and expire_exports for one whose job died), so the retry is ready early in the grace period, and
    by the purge as a backstop, which also postpones the deletion EXPORT_DAYS so the retry can be downloaded. A second
    failure is final: the purge then goes ahead, so a broken export can't keep an organization that asked to be deleted
    for ever. True when it queued one."""
    org = Organization.objects.select_for_update().filter(pk=org_id).first()
    if org is None or org.deletion_due_at is None or _active(org, exclude=(ExportPurpose.BACKUP,)) is not None:
        return False
    since = _since_request(org).exclude(purpose=ExportPurpose.BACKUP)  # a nightly backup is no final export
    if since.filter(status__in=[ExportStatus.READY, ExportStatus.EXPIRED]).exists():
        return False
    if since.filter(purpose=ExportPurpose.DELETION, status=ExportStatus.FAILED).count() != 1:
        return False  # no final export to retry, or the retry failed too
    export = _queue(org, None, ExportPurpose.DELETION)
    audit(org, None, "organization.deletion_export_retried", export, {"purpose": export.purpose})
    if postpone:
        _postpone(org, (now or timezone.now()) + timedelta(days=EXPORT_DAYS), "final_export_retried")
    log.info("final export queued again org=%s", org.pk)
    return True


def resume_deletion(org, suspended_at, now=None):
    """On unsuspend (catalog/platform_admin.unsuspend): a deletion the suspension paused gets back the time it had
    left when the suspension began, and at least MIN_GRACE_DAYS, so its Admins can still cancel.
    It doesn't keep the final export alive: that expires EXPORT_DAYS after it was made, suspended or not, so
    after a long suspension the Admins download a fresh export instead. Audited as organization.deletion_postponed
    (reason `suspension`) when that moves the date. Call with the organization locked; returns the new due date, or None when none is scheduled."""
    if org.deletion_due_at is None:
        return None
    now = now or timezone.now()
    left = org.deletion_due_at - suspended_at if suspended_at is not None else timedelta(0)
    _postpone(org, now + max(left, timedelta(days=MIN_GRACE_DAYS)), "suspension")
    return org.deletion_due_at


def _download_window(org, now) -> bool:
    """The final export became ready less than EXPORT_DAYS ago: postpone the deletion so it can be downloaded for that
    long (a late retry, or a grace period shorter than the export's lifetime). True when it postponed."""
    final = (
        _since_request(org)
        .filter(purpose=ExportPurpose.DELETION, status=ExportStatus.READY, finished_at__isnull=False)
        .order_by("-finished_at")
        .first()
    )
    if final is None or final.finished_at is None:
        return False
    until = final.finished_at + timedelta(days=EXPORT_DAYS)
    if until <= now:
        return False
    _postpone(org, until, "final_export_download_window")
    return True


def purge(org_id, now=None) -> OrganizationTombstone | None:
    """Remove every row of a organization whose deletion is due, in one transaction, then its files. None when it
    isn't due (cancelled meanwhile, or not scheduled) or the organization is suspended: a suspension pauses the
    deletion, which resumes on unsuspend (resume_deletion). The composite foreign keys are
    deferred, so the per-table deletes can run in any order and are checked together before the tombstone is
    written."""
    now = now or timezone.now()
    with transaction.atomic():
        org = (
            Organization.objects.select_for_update()
            .filter(pk=org_id, deletion_due_at__lte=now, suspended_at__isnull=True)
            .first()
        )
        if org is None:
            return None
        if _active(org) is not None:
            # An export is queued or being written: purging now would leave its zip behind untracked, or destroy the
            # final export before it exists. Wait for it (the next run tries again); expire_exports fails one whose
            # job died, so this can't wait for ever.
            log.info("organization purge waits for an export org=%s", org.pk)
            return None
        if retry_final_export(org.pk, postpone=True, now=now) or _download_window(org, now):
            return None
        user_ids = list(Membership.objects.filter(organization=org).values_list("user_id", flat=True))
        files = _files(org)
        rows = 0
        quote = connection.ops.quote_name
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'on', true)", [AUDIT_PURGE_SETTING])
            for model in tenant_models():
                table, column = model._meta.db_table, org_column(model)
                cursor.execute(f"DELETE FROM {quote(table)} WHERE {quote(column)} = %s", [org.pk])  # noqa: S608
                rows += max(cursor.rowcount, 0)
            cursor.execute("SELECT set_config(%s, 'off', true)", [AUDIT_PURGE_SETTING])
            table = quote(Organization._meta.db_table)
            cursor.execute(f"DELETE FROM {table} WHERE id = %s", [org.pk])  # noqa: S608
        connection.check_constraints()  # nothing anywhere still points at the organization or its rows
        _release_accounts(org.pk, user_ids)
        tombstone = OrganizationTombstone.objects.create(
            former_organization_id=org.pk,
            name_sha256=hashlib.sha256(org.name.encode()).hexdigest(),
            requested_by_id=org.deletion_requested_by_id,
            requested_at=org.deletion_requested_at,
            deleted_at=now,
            rows_deleted=rows,
        )
    forget_organization(org.pk)
    _remove_files(org.pk, files)
    log.info("organization purged org=%s rows=%s files=%s", org.pk, rows, len(files))
    return tombstone


class PurgeKeepsFailing(Exception):
    """Raised by purge_due, after its run, while a due organization's purge has failed PURGE_ALERT_AFTER times in a
    row. The schedule then shows as failing (the System page's installation view, `platform/system`, /healthz/ops and
    `manage.py doctor`), with this class's name; the message has a count only, the ids are in the worker log."""


def _tried(org_id, now, *, failed: bool) -> None:
    """Record a purge attempt that removed nothing: when, and whether it raised (consecutive failures count up; a
    clean try, such as waiting for an export, resets them). The purge's bookkeeping, not an edit: not audited."""
    failures = F("purge_failures") + 1 if failed else Value(0)
    Organization.objects.filter(pk=org_id).update(purge_attempted_at=now, purge_failures=failures)


def purge_due(now) -> dict[str, int]:
    """The `org.purge_deleted` schedule: purge up to PURGES_PER_RUN organizations whose deletion is due and that
    aren't suspended, least recently tried first (never tried first, then by due date), so organizations whose purge
    keeps failing can't take every place in the run. Each in its own transaction; a failure
    is logged (ids and the exception class only), counted, recorded on the organization and retried in a later run.
    Once one has failed PURGE_ALERT_AFTER times in a row the run ends by raising PurgeKeepsFailing (after doing its
    work), so the schedule shows as failing until it is fixed."""
    due = list(
        Organization.objects.filter(deletion_due_at__lte=now, suspended_at__isnull=True)
        .order_by(F("purge_attempted_at").asc(nulls_first=True), "deletion_due_at", "pk")
        .values_list("pk", flat=True)[:PURGES_PER_RUN]
    )
    purged = failed = 0
    for org_id in due:
        try:
            done = purge(org_id, now)
        except Exception as error:  # one organization's failure mustn't hold up the others; it is retried later
            failed += 1
            log.warning("organization purge failed org=%s error=%s", org_id, type(error).__name__)
            _tried(org_id, now, failed=True)
            continue
        if done is not None:
            purged += 1
        else:
            _tried(org_id, now, failed=False)
    stuck = list(
        Organization.objects.filter(
            deletion_due_at__lte=now, suspended_at__isnull=True, purge_failures__gte=PURGE_ALERT_AFTER
        )
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if stuck:
        log.error("organization purge keeps failing orgs=%s", ",".join(str(pk) for pk in stuck))
        raise PurgeKeepsFailing(f"{len(stuck)} organization(s) failed to purge {PURGE_ALERT_AFTER} times in a row.")
    return {"purged": purged, "failed": failed}


def register() -> None:
    from . import signup

    jobs.register_job(EXPORT_JOB, run_export_job, lease=EXPORT_LEASE, max_attempts=1, lane=jobs.LONG_LANE)
    jobs.register_schedule("signup.purge", signup.purge_expired, "15 * * * *", "Delete expired unconfirmed signups")
    jobs.register_schedule("org.exports_expire", expire_exports, "25 * * * *", "Remove expired organization exports")
    jobs.register_schedule(
        "org.purge_deleted", purge_due, "*/15 * * * *", "Delete organizations whose deletion grace period has ended"
    )
    jobs.register_schedule(
        BACKUP_SCHEDULE, queue_backups, "10 1 * * *", "Queue each organization's nightly backup (org.tenant_backups)"
    )
