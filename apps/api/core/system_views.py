"""The System status API: the worker's heartbeat, each
schedule's last run and state, the job queue, and the nightly backup (core/backup.py) with its warnings and
the backup key acknowledgement. Admin only.

Two views:

- `installation`: schedules, the backup block, installation-wide jobs and every warning. For the installation's
  operator: platform staff (a password session, not impersonating), or, on a self-hosted installation only (not
  SOFTWARDEN_CLOUD), an Admin of its first organization, the one bootstrap_admin made. On a cloud installation no
  organization's Admin ever gets it, whatever its edition or capabilities. Job counts and recent problems cover that organization's jobs plus installation-wide ones
  (no organization), never another organization's.
- `organization`: every other Admin, so a tenant of a shared installation sees none of the operator's internals:
  the overall status (what the public `/readyz` already says), the worker's heartbeat, this organization's own job
  counts and problems, and `stored_secrets_unreadable` for this organization's own secrets only. No `schedules`, no
  `backup`, no `jobs.system`, and the backup key acknowledgement is refused (403).

Platform staff without a membership read the installation view at `platform/system` (catalog/platform_views.py)."""

from typing import Any

from django.conf import settings
from django.db.models import Count, F, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from . import backup, health
from .capabilities import BILLING, has_capability
from .drf import ADMIN, PROBLEMS, RoleAPIView, policy, refuse_unknown_params
from .models import Job, JobStatus, Organization
from .platform_access import staff_session

RECENT_PROBLEMS = 20
NOT_THE_OPERATOR = (
    "The backup key belongs to the whole installation; its operator acknowledges it (platform staff, or an Admin of "
    "the installation's first organization)."
)


class WorkerStatusSerializer(serializers.Serializer):
    state = serializers.ChoiceField(["ok", "stale", "never"])
    last_tick = serializers.DateTimeField(allow_null=True)
    age_seconds = serializers.IntegerField(allow_null=True)
    stale_after_seconds = serializers.IntegerField()


class ScheduleStatusSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the schedule's label, not Field.label
    cron = serializers.CharField(help_text="Five-field cron in UTC.")
    enabled = serializers.BooleanField()
    state = serializers.ChoiceField([*health.STATES, "retired"])
    detail = serializers.CharField()
    next_due = serializers.DateTimeField(allow_null=True)
    last_started = serializers.DateTimeField(allow_null=True)
    last_finished = serializers.DateTimeField(allow_null=True)
    last_succeeded = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(
        help_text="The exception type only (the message is in the worker log), or a note on an abandoned run."
    )
    last_result = serializers.DictField(help_text="A short summary of the last successful run, e.g. {'sent': 3}.")


class JobCountsSerializer(serializers.Serializer):
    queued = serializers.IntegerField()
    running = serializers.IntegerField()
    succeeded = serializers.IntegerField()
    failed = serializers.IntegerField()
    abandoned = serializers.IntegerField()


class JobProblemSerializer(serializers.Serializer):
    """A failed or abandoned Job row."""

    id = serializers.UUIDField()
    kind = serializers.CharField()
    scope = serializers.SerializerMethodField()
    status = serializers.ChoiceField([JobStatus.FAILED, JobStatus.ABANDONED])
    attempts = serializers.IntegerField()
    last_error = serializers.CharField()
    finished_at = serializers.DateTimeField(allow_null=True)

    @extend_schema_field(serializers.ChoiceField(["organization", "system"]))
    def get_scope(self, job) -> str:
        return "system" if job.organization_id is None else "organization"


class JobsStatusSerializer(serializers.Serializer):
    organization = JobCountsSerializer(
        required=False, help_text="This organization's jobs. Absent in the platform console's view (no organization)."
    )
    system = JobCountsSerializer(
        required=False, help_text="Installation-wide jobs (no organization). Absent in the `organization` view."
    )
    recent_problems = JobProblemSerializer(many=True, help_text="Latest failed or abandoned jobs, newest first.")


class BackupStatusSerializer(serializers.Serializer):
    """The nightly backup (core/backup.py). Installation-wide. Never holds the key or anything derived from it."""

    state = serializers.ChoiceField(
        ["ok", "stale", "never", "not_configured"],
        help_text="`stale` once the last good backup is older than `stale_after_seconds` (26 h by default); "
        "`not_configured` when SOFTWARDEN_BACKUP_DIR is empty (outside the image).",
    )
    destination = serializers.CharField(help_text="The backup folder inside the worker container, or empty.")
    cron = serializers.CharField(help_text="When the nightly backup runs: five-field cron in UTC.")
    last_success_at = serializers.DateTimeField(allow_null=True, help_text="When the last good set was published.")
    age_seconds = serializers.IntegerField(allow_null=True)
    stale_after_seconds = serializers.IntegerField()
    last_set = serializers.CharField(help_text="The last good set's name, e.g. softwarden-20261107T023000Z.")
    last_size_bytes = serializers.IntegerField(allow_null=True)
    last_duration_seconds = serializers.FloatField(allow_null=True)
    last_failure_at = serializers.DateTimeField(allow_null=True)
    last_failure = serializers.CharField(
        help_text="The last failure: its stage and, for a tool, its exit status only (the details are in the worker log)."
    )
    running = serializers.BooleanField()
    key_source = serializers.ChoiceField(
        ["generated", "configured"],
        help_text="`generated`: the init service's key in the secrets volume; `configured`: SOFTWARDEN_BACKUP_KEY_FILE.",
    )
    key_present = serializers.BooleanField()
    key_acknowledged = serializers.BooleanField(
        help_text="An Admin confirmed the generated key is copied off the host (always true for a configured key)."
    )
    key_acknowledged_at = serializers.DateTimeField(allow_null=True)


class SystemWarningSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        [
            "backup_stale",
            "backup_failed",
            "backup_key_missing",
            "backup_key_unacknowledged",
            "stored_secrets_unreadable",
            "demo_password",
        ]
    )
    message = serializers.CharField()


class SystemStatusSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(
        ["installation", "organization"],
        help_text="`installation`: the operator's view, with schedules, the backup and installation-wide jobs. "
        "`organization`: an Admin of one organization among others on a shared installation, without them.",
    )
    status = serializers.ChoiceField(["ok", "degraded", "unhealthy"])
    checked_at = serializers.DateTimeField()
    worker = WorkerStatusSerializer()
    schedules = ScheduleStatusSerializer(many=True, required=False, help_text="Absent in the `organization` view.")
    jobs = JobsStatusSerializer()
    backup = BackupStatusSerializer(required=False, help_text="Absent in the `organization` view.")
    warnings = SystemWarningSerializer(
        many=True,
        help_text="Problems to act on that never change `status` or `/readyz`: backups, stored secrets "
        "encrypted under a credential key that isn't configured (after a restore without the old key), and (the "
        "`installation` view, outside development) demo accounts that still have seed_demo's published password.",
    )


def _counts(qs):
    row = qs.aggregate(**{status: Count("id", filter=Q(status=status)) for status in JobStatus.values})
    return {status: row[status] for status in JobStatus.values}


def job_status(org, *, system: bool = True) -> dict[str, Any]:
    """Job counts for this organization and (`system`) for installation-wide jobs, and the latest problems among them
    (never another organization's), for JobsStatusSerializer. `org` None: installation-wide jobs only (the platform
    console's view)."""
    found: dict[str, Any] = {}
    mine = Q(pk__in=[])
    if org is not None:
        found["organization"] = _counts(Job.objects.filter(organization=org))
        mine |= Q(organization=org)
    if system:
        found["system"] = _counts(Job.objects.filter(organization__isnull=True))
        mine |= Q(organization__isnull=True)
    found["recent_problems"] = list(
        Job.objects.filter(mine, status__in=[JobStatus.FAILED, JobStatus.ABANDONED])
        .order_by(F("finished_at").desc(nulls_last=True))
        .only("id", "kind", "organization_id", "status", "attempts", "last_error", "finished_at")[:RECENT_PROBLEMS]
    )
    return found


def first_organization_id():
    """The installation's first organization: on a self-hosted install, the one bootstrap_admin made."""
    return Organization.objects.order_by("created_at", "id").values_list("pk", flat=True).first()


def sees_installation(request, org) -> bool:
    """Whether this request gets the System page's `installation` view (the module docstring)."""
    if staff_session(request):
        return True
    # The installation flag, not the organization's edition or billing capability: on cloud the first organization is
    # the first signup, and platform staff may change its edition or purge it.
    if settings.SOFTWARDEN_CLOUD or has_capability(org, BILLING):
        return False
    return first_organization_id() == org.pk


def installation_status(org) -> dict[str, Any]:
    """The `installation` view: everything, with `org`'s jobs beside the installation-wide ones (None: those only)."""
    found = {**health.status(), "scope": "installation", "jobs": job_status(org)}
    return {**found, "warnings": found["warnings"] + demo_warnings()}


def demo_warnings() -> list[dict[str, str]]:
    """`demo_password` while an active `@example.test` account still has seed_demo's published password
    (outside DEBUG; core/demo.py). The operator's view only: the accounts are the installation's."""
    from .demo import accounts_with_demo_password

    found = accounts_with_demo_password()
    if not found:
        return []
    return [
        {
            "code": "demo_password",
            "message": f"{len(found)} demo account(s) still have the published demo password ({', '.join(found)}): "
            "anyone can sign in as them. Deactivate or remove them in Users & roles, or set a new password with "
            "manage.py reset_admin <email> --any-member.",
        }
    ]


def organization_status(org) -> dict[str, Any]:
    """The `organization` view: the overall status and heartbeat, this organization's jobs and its own secrets."""
    now = timezone.now()
    worker = health.worker_check(now)
    return {
        "scope": "organization",
        "status": health.overall(worker, health.schedule_checks(now)),
        "checked_at": now,
        "worker": worker,
        "jobs": job_status(org, system=False),
        "warnings": health.secrets_warnings(org.pk),
    }


class SystemView(RoleAPIView):
    role_policy = policy(ADMIN)

    @extend_schema(
        summary="Worker, schedule and job status (Admin)",
        description="Overall `unhealthy` when the heartbeat is stale or a schedule is overdue or stuck; `degraded` "
        "when a schedule's last run failed. `/readyz` answers 503 exactly when this is `unhealthy`. `scope` says "
        "which view this is: an Admin of one organization among others on a shared installation gets the "
        "`organization` view, without schedules, the backup or installation-wide jobs.",
        responses={200: SystemStatusSerializer, **{c: PROBLEMS[c] for c in (400, 403)}},
    )
    def get(self, request):
        refuse_unknown_params(request)
        org = self.organization
        found = installation_status(org) if sees_installation(request, org) else organization_status(org)
        return Response(SystemStatusSerializer(found).data)


class BackupKeyAcknowledgeView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN)

    @extend_schema(
        summary="Acknowledge that the generated backup key is copied off the host (Admin)",
        description="Clears the `backup_key_unacknowledged` warning until the key changes. Audited "
        "(`backup.key_acknowledged`) in every organization, since the key is the installation's; a repeat for the same key "
        "changes nothing. 403 for an Admin who gets the System page's `organization` view (the key isn't theirs). "
        "409 when the key is set with SOFTWARDEN_BACKUP_KEY_FILE or doesn't exist yet.",
        request=None,
        responses={200: BackupStatusSerializer, **{c: PROBLEMS[c] for c in (403, 409)}},
    )
    def post(self, request):
        if not sees_installation(request, self.organization):
            raise PermissionDenied(NOT_THE_OPERATOR)
        backup.acknowledge_key(self.organization, request.user)
        return Response(BackupStatusSerializer(backup.status()).data)
