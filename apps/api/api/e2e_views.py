"""Test-only endpoints for the Playwright suite. Registered only when SOFTWARDEN_E2E=1 and the database
name ends in _e2e (api/urls.py), so they never exist in development or on a server.

The e2e server sends email through Django's locmem backend, so a digest, a signup confirmation or an invitation lands
in django.core.mail.outbox in the server process; run-digest and outbox read it back. Nothing leaves the machine."""

import copy
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.mail.backends.locmem import EmailBackend as LocmemBackend
from django.db import transaction
from django.test import override_settings
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core import graph, outbound
from core.access import org_role, require_admin
from core.drf import ADMIN, VIEWER, min_role
from notify import channels, deliveries, digest, rules, webhooks
from notify import subscriptions as view_subscriptions
from notify.models import Channel, Delivery, DeliveryKind, DeliveryStatus, SavedViewSubscription

# Teams and webhook destinations on hosts under `e2e.test` (a reserved name that never resolves) are answered here
# instead of over the network: 202 for any host, 500 for a host starting with `fail.` and 410 Gone for
# one starting with `gone.` (an event delivery answered 410 turns its webhook endpoint off). Every other
# address goes through the real, SSRF-guarded POST, as in production. The API's own tests fake the same function.
# Installed only by install_hooks(), from the gated block in api/urls.py: importing this module changes nothing (pytest
# imports it to test the fixtures).
E2E_HOOK_DOMAIN = ".e2e.test"
_real_post = channels.post_https


def _e2e_post(url, body, headers, *args, **kwargs):
    host = (outbound.check_url(url).hostname or "").lower()
    if not host.endswith(E2E_HOOK_DOMAIN):
        return _real_post(url, body, headers, *args, **kwargs)
    if host.startswith("fail."):
        return graph.Reply(500, {}, b"The e2e test endpoint refused the post.")
    if host.startswith("gone."):
        return graph.Reply(410, {}, b"This e2e test endpoint is gone.")
    return graph.Reply(202, {}, b"")


def install_hooks() -> None:
    """Answer posts to `e2e.test` hosts in process, and give the assistant spec organization's batches a fake Anthropic
    client (the Playwright API server only; see api/urls.py)."""
    from discovery import assistant

    channels.post_https = _e2e_post
    assistant.make_client = _e2e_make_client


# A mail run (run-digest's digest, run-subscriptions) on this thread: the messages it sent, and whether its mail server
# is down. The Playwright server answers requests on several threads at once and the specs run in parallel, so a run
# can't take "the mail added to the outbox while it ran" (another spec's mail landed there too: a weekly saved view
# email was taken out of the outbox by a digest run, and a `fail` run made that email fail).
_mail_run = threading.local()
# One mail run at a time: each switches the process's EMAIL_BACKEND to RunBackend for its length (override_settings),
# and overlapping switches would end one run's switch inside another's.
_MAIL_RUNS = threading.Lock()


class RunBackend(LocmemBackend):
    """The locmem outbox, as the e2e server's own backend. On a thread inside a mail run the messages are also recorded
    for that run, and with its mail server down (`failing`) the connection is refused there before anything is sent (a
    retryable failure); mail from every other thread is sent as usual meanwhile."""

    def open(self):
        if getattr(_mail_run, "failing", False):
            raise ConnectionRefusedError("Connection refused by the test mail server")
        return super().open()

    def send_messages(self, messages):
        if getattr(_mail_run, "failing", False):
            raise ConnectionRefusedError("Connection refused by the test mail server")
        sent = getattr(_mail_run, "sent", None)
        count = 0
        for message in messages:  # as locmem does; the copy in the outbox is the one recorded
            message.message()  # header validation
            kept = copy.deepcopy(message)
            mail.outbox.append(kept)
            if sent is not None:
                sent.append(kept)
            count += 1
        return count


@contextmanager
def mail_run(failing: bool = False) -> Iterator[list]:
    """Record the mail this thread sends (yielded, in order), with the mail server down when `failing`."""
    sent: list = []
    with _MAIL_RUNS, override_settings(EMAIL_BACKEND="api.e2e_views.RunBackend"):
        _mail_run.sent, _mail_run.failing = sent, failing
        try:
            yield sent
        finally:
            _mail_run.sent, _mail_run.failing = None, False


@api_view(["POST"])
@min_role(ADMIN)
def run_digest(request):
    """Run today's digest for the caller's organization now, as at its digest time, and send its deliveries in the
    request; `fail: true` makes every send fail (SMTP down) and `exhaust: true` uses up the retries. Returns the
    emails this run sent and takes them out of the outbox; other mail there (signup confirmations, invitations) stays
    for e2e/outbox. The run's emails are the ones this request sent (mail_run), never another request's sent at the
    same moment, and `fail` refuses only this run's mail. Two digest runs still replace each other's day: the digest
    specs run on their own (playwright.config.ts). `rules: true` runs the notification rules instead (run_rules) and leaves the
    digest and the outbox alone; `subscriptions: {"weekday": 0-6}` runs the weekly saved view emails instead
    (run_subscriptions); `webhooks: true` makes one attempt at each queued webhook delivery instead (run_webhooks);
    `outbox: true` only drains the organization's webhook outbox into queued deliveries, as the tick's
    `webhooks.outbox` schedule does before it runs any job, and answers the drain's counts."""
    org, role = org_role(request)
    require_admin(role)
    if request.data.get("rules"):
        return Response(run_rules(org))
    if request.data.get("webhooks"):
        return Response(run_webhooks(org))
    if request.data.get("outbox"):
        return Response(webhooks.drain(organization_id=org.id))
    if request.data.get("subscriptions"):
        spec = request.data["subscriptions"]
        weekday = spec.get("weekday") if isinstance(spec, dict) else None
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            return Response({"detail": 'subscriptions: {"weekday": 0 (Monday) to 6 (Sunday)}.'}, status=400)
        return Response(run_subscriptions(org, weekday))
    zone = ZoneInfo(org.timezone)
    today = datetime.now(zone).date()
    digest.discard_day(org, today)  # a rerun replaces today's run and its deliveries
    now = datetime.combine(today, org.digest_time, tzinfo=zone) + timedelta(minutes=1)
    with mail_run(failing=bool(request.data.get("fail"))) as sent:
        digest.run_for_org(org, now)
        # One attempt, or with `exhaust` every attempt a delivery gets, so a failure ends Failed at once.
        rounds = deliveries.MAX_ATTEMPTS if request.data.get("exhaust") else 1
        digest.send_now(org, today, datetime.now(zone) + timedelta(days=1), rounds=rounds)
        _take(sent)
    return Response([_message(m) for m in sent])


def _take(messages: list) -> None:
    """Take these messages out of the outbox, one by one, so mail another thread adds meanwhile stays (rebuilding the
    list could drop it)."""
    box = _outbox()
    for m in messages:
        try:
            box.remove(m)  # EmailMessage has no __eq__: by identity
        except ValueError:
            pass  # already taken (e2e/outbox with clear)


def _outbox() -> list:
    """The locmem backend's outbox, made if no email was sent yet (the backend makes it on first use)."""
    box = getattr(mail, "outbox", None)
    if not isinstance(box, list):
        box = []
        mail.outbox = box  # locmem's own attribute
    return box


def _message(m) -> dict:
    return {"to": list(m.to), "subject": m.subject, "body": m.body}


def run_rules(org):
    """The organization's notification rules for its local today, as the hourly `notify.rules` run would at its send
    time, then one attempt at each reminder and escalation delivery it queued (the notification log's
    rule deliveries). Emails go to the locmem outbox, which is left as it is; Teams and webhook posts to `e2e.test`
    hosts are answered by _e2e_post. Returns how many were queued and how each ended."""
    zone = ZoneInfo(org.timezone)
    today = datetime.now(zone).date()
    now = datetime.combine(today, rules.send_time(org), tzinfo=zone) + timedelta(minutes=1)
    queued = rules.run_for_org(org, now)
    ids = list(
        Delivery.objects.filter(
            organization=org,
            status=DeliveryStatus.QUEUED,
            kind__in=[DeliveryKind.REMINDER, DeliveryKind.ESCALATION],
        ).values_list("id", flat=True)
    )
    ended: dict[str, int] = {}
    for delivery_id in ids:
        done = deliveries.attempt(delivery_id, datetime.now(zone) + timedelta(minutes=1))
        if done is not None:
            ended[done.status] = ended.get(done.status, 0) + 1
    return {"queued": queued, "ended": ended}


def run_webhooks(org):
    """One attempt at each of the organization's queued webhook deliveries (events, resends, rule notices), as the
    worker's next tick would, a minute from now so a retry's wait is over (the webhook delivery log, and an
    endpoint on a `gone.e2e.test` host turned off by a 410). Posts to `e2e.test` hosts are answered by _e2e_post.
    Returns how many were attempted and how each ended. The organization's outbox is drained first, as the tick's
    `webhooks.outbox` schedule would."""
    webhooks.drain(organization_id=org.id)
    ids = list(
        Delivery.objects.filter(organization=org, channel=Channel.WEBHOOK, status=DeliveryStatus.QUEUED)
        .order_by("created_at")
        .values_list("id", flat=True)
    )
    ended: dict[str, int] = {}
    for delivery_id in ids:
        done = deliveries.attempt(delivery_id, datetime.now(ZoneInfo(org.timezone)) + timedelta(minutes=1))
        if done is not None:
            ended[done.status] = ended.get(done.status, 0) + 1
    return {"attempted": len(ids), "ended": ended}


def next_local_date(org, weekday: int) -> date:
    """The organization's next local date (today included) that falls on `weekday` (0 Monday)."""
    today = datetime.now(ZoneInfo(org.timezone)).date()
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def run_subscriptions(org, weekday: int):
    """The weekly saved view emails as the hourly `saved_views.subscriptions` run would make them on
    the organization's next local date that is a `weekday` (today included), a minute after its send time, then one
    attempt at each email it queued. Emails go to the locmem outbox, as the server's other email does; the ones this
    run sent are returned (to, subject, text and HTML) and left in the outbox. Returns the date, the run's counts and
    those emails."""
    zone = ZoneInfo(org.timezone)
    day = next_local_date(org, weekday)
    now = datetime.combine(day, rules.send_time(org), tzinfo=zone) + timedelta(minutes=1)
    with mail_run() as mine:  # this run's mail only, never a digest's sent at the same moment (mail_run)
        counts = view_subscriptions.run_for_org(org, now)
        keys = {
            view_subscriptions.delivery_key(s, day)
            for s in SavedViewSubscription.objects.filter(organization=org, weekday=weekday)
        }
        ids = list(
            Delivery.objects.filter(
                organization=org, kind=DeliveryKind.SUBSCRIPTION, status=DeliveryStatus.QUEUED, dedupe_key__in=keys
            ).values_list("id", flat=True)
        )
        # A delivery is due from when it was queued (the real clock), so the attempt is made a minute after the later of
        # the run's time and the real time. When the organization's day is already the weekday and past its send time
        # (Auckland, Wednesday after 08:00, while it is Tuesday evening in UTC), the run's time was earlier than the
        # queue time and the email was never attempted.
        attempt_at = max(now, datetime.now(zone)) + timedelta(minutes=1)
        for delivery_id in ids:
            deliveries.attempt(delivery_id, attempt_at)
    sent = [
        {
            "to": m.to,
            "subject": m.subject,
            "body": m.body,
            "html": next((body for body, kind in getattr(m, "alternatives", []) if kind == "text/html"), ""),
        }
        for m in mine
        if m.subject.startswith("Saved view")
    ]
    return {"date": day.isoformat(), "counts": dict(counts), "emails": sent}


@api_view(["POST"])
@min_role(ADMIN)
def run_connectors(request):
    """Run the caller's organization's queued connector runs now, as the worker would on its next tick (each
    queued run's own `discovery.run` job is claimed and run, so the run keeps its trigger and the
    Admin who pressed Run now)."""
    from core import jobs
    from discovery.models import ConnectorRun, RunStatus

    org, role = org_role(request)
    require_admin(role)
    ran = []
    # A connector turned off since its run was queued goes through its job too, which fails the run in words.
    queued = ConnectorRun.objects.filter(organization=org, status=RunStatus.QUEUED)
    for run in queued.exclude(job_id=None).order_by("created_at"):
        job = jobs.claim(ids=[run.job_id])
        if job is None:
            continue
        jobs.run(job)
        run.refresh_from_db()
        ran.append({"connector": str(run.connector_id), "status": run.status, "counts": run.counts})
    return Response(ran)


SECOND_ORG = "Fabrikam (synthetic)"
MULTI_ORG_EMAIL = "multi@example.test"


@api_view(["POST"])
@min_role(ADMIN)
def multi_org_user(request):
    """Make sure a second organization exists and that `multi@example.test` (demo password) is an Editor in the
    caller's organization and in it, joined in that order so the caller's is the account's default (SPA-2 organization
    switcher). Idempotent. Synthetic data in the throwaway e2e database only."""
    from django.contrib.auth import get_user_model

    from catalog.management.commands.seed_demo import DEMO_PASSWORD
    from core.models import Membership, Organization, Role

    org, role = org_role(request)
    require_admin(role)
    second, _ = Organization.objects.get_or_create(name=SECOND_ORG)
    user, created = get_user_model().objects.get_or_create(
        username=MULTI_ORG_EMAIL, defaults={"email": MULTI_ORG_EMAIL, "first_name": "Morgan", "last_name": "Multi"}
    )
    if created:
        user.set_password(DEMO_PASSWORD)
        user.save()
    for target in (org, second):
        Membership.objects.get_or_create(user=user, organization=target, defaults={"role": Role.EDITOR})
    return Response({"email": MULTI_ORG_EMAIL, "organizations": [str(org.id), str(second.id)]})


# ---- single sign-on -----------------------------------------------------------------------------------------

SSO_SEED_EMAIL = "editor@example.test"
SSO_CLIENT_ID = "11111111-2222-4333-8444-555555555555"  # the simulator's client, as playwright.config.ts's OIDC env
SSO_SECRET = "synthetic-oidc-secret"  # noqa: S105  (a synthetic client secret for the simulator)


@api_view(["POST"])
@min_role(ADMIN)
def sso_seed(request):
    """Give the seed organization (the one `editor@example.test` belongs to) active Entra sign-in settings through the
    simulated tenant, as an activated tested draft would be, so the sign-in page offers SSO at `/login?org=<slug>`
    whatever other organizations the suite has made. Idempotent. Answers {"org": "<slug>"}. Synthetic data in the
    throwaway e2e database only."""
    import os

    from core import credentials, oidc_discovery
    from core.models import Membership, SsoProvider, SsoSettings, SsoStatus

    org, role = org_role(request)
    require_admin(role)
    seed = (
        Membership.objects.select_related("organization").filter(user__username=SSO_SEED_EMAIL).order_by("created_at")
    )
    target = seed.first().organization if seed.exists() else org  # type: ignore[union-attr]
    client_id = os.environ.get("SOFTWARDEN_OIDC_CLIENT_ID") or SSO_CLIENT_ID
    secret = os.environ.get("SOFTWARDEN_OIDC_CLIENT_SECRET") or SSO_SECRET
    discovery_url, issuer = oidc_discovery.urls(SsoProvider.ENTRA, tenant=oidc_discovery.SIMULATED)
    row, _ = SsoSettings.objects.get_or_create(organization=target)
    ready = (
        row.status == SsoStatus.ACTIVE
        and row.provider == SsoProvider.ENTRA
        and row.tenant == oidc_discovery.SIMULATED
        and row.client_id == client_id
        and bool(row.secret_ciphertext)
    )
    if not ready:
        for prefix in ("", "draft_"):
            setattr(row, f"{prefix}provider", SsoProvider.ENTRA)
            setattr(row, f"{prefix}tenant", oidc_discovery.SIMULATED)
            setattr(row, f"{prefix}discovery_url", discovery_url)
            setattr(row, f"{prefix}issuer", issuer)
            setattr(row, f"{prefix}client_id", client_id)
            credentials.slot_set(row, f"{prefix}secret", secret)
        row.draft_revision += 1
        row.draft_tested_revision, row.draft_test_ok = row.draft_revision, True
        row.status = SsoStatus.ACTIVE
        row.active_revision += 1
        row.save()
    return Response({"org": target.slug})


# ---- System page ---------------------------------------------------------------------------------------------

E2E_JOB_KIND = "e2e.sample"
SCHEDULE_STATES = ("ok", "failing", "running", "stuck", "overdue", "disabled", "never_run")


def set_system_states(org, spec, now=None):
    """Put the installation-wide worker heartbeat and Schedule rows, and a few sample jobs, into known states, so the
    System page can be checked without a worker running. Every Schedule row and the heartbeat are replaced first.

    `spec`: `worker` ok / stale / never; `schedules` {registered name: state}, a state from SCHEDULE_STATES (a
    registered schedule not named, or named never_run, gets no row); `retired` names of rows the code doesn't register;
    `jobs` true adds a failed job for `org` and an abandoned installation-wide one. Returns errors by field, or {}."""
    import uuid

    from django.utils import timezone

    from core import jobs, worker
    from core.models import Job, JobStatus, Schedule, WorkerHeartbeat

    now = now or timezone.now()
    if not isinstance(spec, dict):
        return {"spec": ["Send an object."]}
    worker_state = spec.get("worker", "never")
    states = spec.get("schedules") or {}
    retired = spec.get("retired") or []
    errors: dict[str, list[str]] = {}
    if worker_state not in ("ok", "stale", "never"):
        errors["worker"] = ["Choose ok, stale or never."]
    if not isinstance(states, dict) or any(s not in SCHEDULE_STATES for s in states.values()):
        errors["schedules"] = [f"Map schedule names to one of {', '.join(SCHEDULE_STATES)}."]
    elif unknown := sorted(set(states) - set(jobs.SCHEDULES)):
        errors["schedules"] = [f"Not a registered schedule: {', '.join(unknown)}."]
    if not isinstance(retired, list) or any(not isinstance(n, str) or n in jobs.SCHEDULES for n in retired):
        errors["retired"] = ["List names that aren't registered schedules."]
    if errors:
        return errors

    Schedule.objects.all().delete()
    WorkerHeartbeat.objects.all().delete()
    if worker_state == "ok":
        worker.beat(now - timedelta(seconds=20))
    elif worker_state == "stale":
        worker.beat(now - timedelta(minutes=10))

    ran = now - timedelta(minutes=2)
    rows = {
        "ok": {
            "last_started": ran,
            "last_finished": ran + timedelta(seconds=4),
            "last_succeeded": ran + timedelta(seconds=4),
            "last_result": {"sent": 3},
        },
        "failing": {
            "last_started": ran,
            "last_finished": ran + timedelta(seconds=4),
            "last_succeeded": now - timedelta(days=1),
            "last_error": "SMTPException",
        },
        "running": {
            "lease_token": uuid.uuid4(),
            "lease_expires": now + timedelta(minutes=10),
            "last_started": now - timedelta(seconds=30),
            "last_succeeded": now - timedelta(days=1),
        },
        "stuck": {
            "lease_token": uuid.uuid4(),
            "lease_expires": now - timedelta(minutes=5),
            "last_started": now - timedelta(minutes=70),
        },
        "overdue": {
            "next_due": now - timedelta(hours=2),
            "last_started": now - timedelta(days=1),
            "last_finished": now - timedelta(days=1),
            "last_succeeded": now - timedelta(days=1),
        },
        "disabled": {"enabled": False},
    }
    for name, state in states.items():
        if state == "never_run":
            continue
        fields = {"cron": jobs.SCHEDULES[name].cron, "next_due": now + timedelta(hours=1), **rows[state]}
        Schedule.objects.create(name=name, **fields)
    for name in retired:
        Schedule.objects.create(name=name, cron="0 4 * * *", next_due=now - timedelta(days=30))

    if spec.get("jobs"):
        Job.objects.create(
            organization=org,
            kind=E2E_JOB_KIND,
            status=JobStatus.FAILED,
            attempts=3,
            last_error="ConnectionError",
            finished_at=now - timedelta(minutes=5),
        )
        Job.objects.create(
            organization=None,
            kind=E2E_JOB_KIND,
            status=JobStatus.ABANDONED,
            attempts=1,
            last_error="Lease expired before the job finished.",
            finished_at=now - timedelta(minutes=8),
        )
    return {}


@api_view(["POST"])
@min_role(ADMIN)
def system_states(request):
    """Set the worker heartbeat, schedule rows and sample jobs for the System page spec (set_system_states). The e2e
    server runs no worker, so without this every schedule is never_run and the worker never seen."""
    from core.problems import FieldErrors

    org, role = org_role(request)
    require_admin(role)
    errors = set_system_states(org, request.data)
    if errors:
        raise FieldErrors(errors)
    return Response({"ok": True})


# ---- Compliance profile -------------------------------------------------------------------------------

ENTERPRISE_ORG = "Northwind Enterprise (synthetic)"
ENTERPRISE_ADMIN = "enterprise-admin@example.test"
# Each group of specs gets its own enterprise organization, because a call turns that organization's compliance
# profile off: `compliance` (the default) for compliance.spec.ts, which turns it on and relies on it staying on;
# `secondary` for the bulk-edit and audit-export specs, which run beside it.
ENTERPRISE_ORGS = {
    "compliance": (ENTERPRISE_ORG, ENTERPRISE_ADMIN, "Erin", "Enterprise"),
    "secondary": ("Litware Enterprise (synthetic)", "litware-admin@example.test", "Lee", "Enterprise"),
}


def make_enterprise_org(which: str = "compliance"):
    """An enterprise organization of its own, with an Admin account in it only (its IT coordinator) and its compliance
    profile off, so the SPA's compliance switch can be checked without changing the edition of the organization the
    rest of the suite shares (only an operator changes an edition: manage.py set_edition). `which` names one of
    ENTERPRISE_ORGS. Idempotent; a rerun turns the profile off again. Returns (organization, user)."""
    from django.contrib.auth import get_user_model

    from catalog import compliance
    from catalog.management.commands.seed_demo import DEMO_PASSWORD
    from core import capabilities
    from core.models import Membership, Organization, Role

    org_name, email, first, last = ENTERPRISE_ORGS[which]
    org, _ = Organization.objects.get_or_create(name=org_name)
    # The operator path (manage.py set_edition uses it too), audited in that organization.
    org, _ = capabilities.set_edition(org, "enterprise", via="e2e")
    settings_ = {k: v for k, v in (org.settings or {}).items() if k != compliance.SETTING}
    Organization.objects.filter(pk=org.pk).update(settings=settings_)
    compliance.forget(org.pk)
    user, created = get_user_model().objects.get_or_create(
        username=email, defaults={"email": email, "first_name": first, "last_name": last}
    )
    if created:
        user.set_password(DEMO_PASSWORD)
        user.save()
    Membership.objects.get_or_create(user=user, organization=org, defaults={"role": Role.ADMIN})
    # Every real organization has an IT coordinator (bootstrap and setup make the first Admin one); settings need it.
    Organization.objects.filter(pk=org.pk, coordinator=None).update(coordinator=user)
    org.refresh_from_db()
    return org, user


@api_view(["POST"])
@min_role(ADMIN)
def enterprise_org(request):
    """Make sure a synthetic enterprise organization and its Admin (demo password, Admin there only) exist, with the
    compliance profile off (make_enterprise_org). Body `which`: `compliance` (the default, Northwind and
    `enterprise-admin@example.test`) or `secondary`; the answer names the account. Synthetic data in the throwaway e2e
    database only."""
    _, role = org_role(request)
    require_admin(role)
    which = request.data.get("which", "compliance") if isinstance(request.data, dict) else "compliance"
    if not isinstance(which, str) or which not in ENTERPRISE_ORGS:
        return Response({"detail": "which: compliance or secondary."}, status=400)
    org, user = make_enterprise_org(which)
    return Response({"email": user.email, "organization": str(org.id)})


# ---- Custom fields -------------------------------------------------------------------------------------

CUSTOM_FIELDS_ORG = "Tailspin Custom Fields (synthetic)"
CUSTOM_FIELDS_USERS = (
    ("cf-admin@example.test", "Casey", "Admin", "admin"),
    ("cf-editor@example.test", "Corey", "Editor", "editor"),
    ("cf-viewer@example.test", "Cam", "Viewer", "viewer"),
)
CUSTOM_FIELDS_PEOPLE = (
    ("Priya Raman", "priya.raman@tailspin.example.test", "HR"),
    ("Dana Ortiz", "dana.ortiz@tailspin.example.test", "IT"),
)


def make_custom_fields_org():
    """An organization of its own for the custom field specs, with an Admin (its IT coordinator), an Editor and a
    Viewer in it only, two people, a team and a vendor. Custom fields are organization-wide: a required field the
    specs define must never reach the forms the rest of the suite fills in the shared Contoso organization. Idempotent.
    Returns the organization."""
    from django.contrib.auth import get_user_model

    from catalog.management.commands.seed_demo import DEMO_PASSWORD
    from catalog.models import Department, Person, Team, Vendor
    from core.models import Membership, Organization

    org, _ = Organization.objects.get_or_create(name=CUSTOM_FIELDS_ORG)
    User = get_user_model()
    for email, first, last, role in CUSTOM_FIELDS_USERS:
        user, created = User.objects.get_or_create(
            username=email, defaults={"email": email, "first_name": first, "last_name": last}
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        Membership.objects.get_or_create(user=user, organization=org, defaults={"role": role})
        if role == "admin":
            Organization.objects.filter(pk=org.pk, coordinator=None).update(coordinator=user)
    for name, email, dept in CUSTOM_FIELDS_PEOPLE:
        department, _ = Department.objects.get_or_create(organization=org, name=dept)
        Person.objects.get_or_create(organization=org, name=name, defaults={"email": email, "department": department})
    Team.objects.get_or_create(organization=org, name="Service Desk")
    Vendor.objects.get_or_create(organization=org, name="Zylker Software")
    org.refresh_from_db()
    return org


@api_view(["POST"])
@min_role(ADMIN)
def custom_fields_org(request):
    """Make sure the synthetic custom fields organization and its three accounts (demo password) exist
    (make_custom_fields_org). Synthetic data in the throwaway e2e database only."""
    _, role = org_role(request)
    require_admin(role)
    org = make_custom_fields_org()
    return Response({"organization": str(org.id), "accounts": {r: e for e, _, _, r in CUSTOM_FIELDS_USERS}})


# ---- Data quality, attachment settings and the journey ---------------------------------------------------

QUALITY_ORG = "Fabrikam Data Quality (synthetic)"
QUALITY_USERS = (
    ("dq-admin@example.test", "Drew", "Admin", "admin"),
    ("dq-editor@example.test", "Devi", "Editor", "editor"),
    ("dq-viewer@example.test", "Dale", "Viewer", "viewer"),
)
JOURNEY_ORG = "Woodgrove Journey (synthetic)"
JOURNEY_USERS = (
    ("pj-admin@example.test", "Pat", "Admin", "admin"),
    ("pj-editor@example.test", "Parker", "Editor", "editor"),
    ("pj-viewer@example.test", "Piper", "Viewer", "viewer"),
)
ORG_PEOPLE = (
    ("Ines Okafor", "ines.okafor@{domain}.example.test", "Finance"),
    ("Mateo Silva", "mateo.silva@{domain}.example.test", "IT"),
)
NOTIFY_ORG = "Litware Notifications (synthetic)"
NOTIFY_USERS = (
    ("nt-admin@example.test", "Noor", "Admin", "admin"),
    ("nt-editor@example.test", "Nico", "Editor", "editor"),
    ("nt-viewer@example.test", "Nell", "Viewer", "viewer"),
)
CONTRACTS_ORG = "Adatum Contracts (synthetic)"
CONTRACTS_USERS = (
    ("ct-admin@example.test", "Cleo", "Admin", "admin"),
    ("ct-editor@example.test", "Cyrus", "Editor", "editor"),
    ("ct-viewer@example.test", "Coral", "Viewer", "viewer"),
)
VIEWS_ORG = "Proseware Saved Views (synthetic)"
VIEWS_USERS = (
    ("sv-admin@example.test", "Sasha", "Admin", "admin"),
    ("sv-editor@example.test", "Sol", "Editor", "editor"),
    ("sv-viewer@example.test", "Sky", "Viewer", "viewer"),
)
SSO_ORG = "Fourth Coffee Single Sign-On (synthetic)"
SSO_USERS = (
    ("sso-admin@example.test", "Sam", "Admin", "admin"),
    ("sso-editor@example.test", "Sid", "Editor", "editor"),
    ("sso-viewer@example.test", "Sage", "Viewer", "viewer"),
)
DISCOVERY_ORG = "Tailwind Discovery Setup (synthetic)"
DISCOVERY_USERS = (
    ("ds-admin@example.test", "Dara", "Admin", "admin"),
    ("ds-editor@example.test", "Dev", "Editor", "editor"),
    ("ds-viewer@example.test", "Dee", "Viewer", "viewer"),
)
WEBHOOKS_ORG = "Wingtip Webhooks (synthetic)"
WEBHOOKS_USERS = (
    ("wh-admin@example.test", "Wren", "Admin", "admin"),
    ("wh-editor@example.test", "Wade", "Editor", "editor"),
    ("wh-viewer@example.test", "Willa", "Viewer", "viewer"),
)
RECONCILE_ORG = "Northwind Reconciliation (synthetic)"
RECONCILE_USERS = (
    ("rc-admin@example.test", "Rae", "Admin", "admin"),
    ("rc-editor@example.test", "Remy", "Editor", "editor"),
    ("rc-viewer@example.test", "Rio", "Viewer", "viewer"),
)
VENDOR_RISK_ORG = "Relecloud Vendor Risk (synthetic)"
VENDOR_RISK_USERS = (
    ("vr-admin@example.test", "Vera", "Admin", "admin"),
    ("vr-editor@example.test", "Vik", "Editor", "editor"),
    ("vr-viewer@example.test", "Val", "Viewer", "viewer"),
)
LIFECYCLE_ORG = "Lamna Lifecycle and Data Flows (synthetic)"
LIFECYCLE_USERS = (
    ("lf-admin@example.test", "Lior", "Admin", "admin"),
    ("lf-editor@example.test", "Lena", "Editor", "editor"),
    ("lf-viewer@example.test", "Luca", "Viewer", "viewer"),
)
SHADOW_IT_ORG = "Trey Research Shadow IT (synthetic)"
SHADOW_IT_USERS = (
    ("si-admin@example.test", "Sia", "Admin", "admin"),
    ("si-editor@example.test", "Simon", "Editor", "editor"),
    ("si-viewer@example.test", "Siri", "Viewer", "viewer"),
)
OFFBOARDING_ORG = "Lamna Offboarding (synthetic)"
OFFBOARDING_USERS = (
    ("ob-admin@example.test", "Oba", "Admin", "admin"),
    ("ob-editor@example.test", "Olu", "Editor", "editor"),
    ("ob-viewer@example.test", "Ona", "Viewer", "viewer"),
)
CATALOG_CODE_ORG = "Alpine Ski House Catalog Code (synthetic)"
CATALOG_CODE_USERS = (
    ("cc-admin@example.test", "Cass", "Admin", "admin"),
    ("cc-editor@example.test", "Cato", "Editor", "editor"),
    ("cc-viewer@example.test", "Cyd", "Viewer", "viewer"),
)
SSO_COVERAGE_ORG = "Tailspin Toys SSO Coverage (synthetic)"
SSO_COVERAGE_USERS = (
    ("sc-admin@example.test", "Scout", "Admin", "admin"),
    ("sc-editor@example.test", "Sage", "Editor", "editor"),
    ("sc-viewer@example.test", "Skye", "Viewer", "viewer"),
)
TICKETS_ORG = "Coho Winery Tickets (synthetic)"
TICKETS_USERS = (
    ("tk-admin@example.test", "Tara", "Admin", "admin"),
    ("tk-editor@example.test", "Theo", "Editor", "editor"),
    ("tk-viewer@example.test", "Tove", "Viewer", "viewer"),
)
# Two Admins saving the same organization settings. They belong to the whole
# organization, so the spec changes them in an organization of its own.
IF_MATCH_ORG = "Wingtip If-Match (synthetic)"
IF_MATCH_USERS = (
    ("im-admin@example.test", "Ines", "Admin", "admin"),
    ("im-admin2@example.test", "Ivo", "Admin", "admin"),
    ("im-viewer@example.test", "Ida", "Viewer", "viewer"),
)
SPEC_ORGS = {
    "quality": (QUALITY_ORG, QUALITY_USERS, "fabrikam"),
    "journey": (JOURNEY_ORG, JOURNEY_USERS, "woodgrove"),
    # Contracts, licence pools and their seat review (licence observations and a required contract
    # custom field are organization-wide, so they stay out of the organization the rest of the suite shares).
    "contracts": (CONTRACTS_ORG, CONTRACTS_USERS, "adatum"),
    # Notification rules, channels and the notification log are organization-wide (a rule or a Teams
    # destination added here would send for every reminder of Contoso), so their specs get their own organization.
    "notify": (NOTIFY_ORG, NOTIFY_USERS, "litware"),
    # Saved views and their weekly emails. The weekly run is organization-wide (every subscription due
    # that day sends), and the emails say the send time in the organization's timezone, so the specs get their own
    # organization with a fixed timezone and digest time.
    "views": (VIEWS_ORG, VIEWS_USERS, "proseware"),
    # Connections, the guided walkthrough and run history. Connectors and connections are
    # organization-wide (discovery.spec.ts expects Contoso to start with none), so these specs get their own.
    "discovery": (DISCOVERY_ORG, DISCOVERY_USERS, "tailwind"),
    # The reconciliation queue, Needs attention's "No longer seen in source" and licence proposals.
    # Simulator changes, the badge counts and the unbound SKUs are organization-wide, so these specs get their own.
    "reconcile": (RECONCILE_ORG, RECONCILE_USERS, "northwind"),
    # Webhook endpoints are organization-wide (one subscribed to record.* would get every spec's changes,
    # and an endpoint turned off shows a banner to every Admin), so the webhook specs get their own organization.
    "webhooks": (WEBHOOKS_ORG, WEBHOOKS_USERS, "wingtip"),
    # SSO settings, sign-in domains, auto-provision and pending access are organization-wide (Contoso's
    # Entra settings back login.spec and discovery.spec), so the SSO settings specs get their own organization.
    "sso": (SSO_ORG, SSO_USERS, "fourthcoffee"),
    # The vendor risk file. Its gaps are a Needs attention queue and count in the badge, so the specs get
    # their own organization with seed_demo's vendor risk scenario (Workday, Zoom, Zylker), put back on every call.
    "vendor_risk": (VENDOR_RISK_ORG, VENDOR_RISK_USERS, "relecloud"),
    # The end-of-life calendar and the data-flow map. Both are Needs attention queues counted in the
    # badge, and the endoflife.date cache is the installation's, so the specs get their own organization with a fixed
    # scenario (lifecycle_scenario), put back on every call.
    "lifecycle": (LIFECYCLE_ORG, LIFECYCLE_USERS, "lamna"),
    # Shadow IT. Connectors and their candidates are organization-wide (discovery.spec.ts expects Contoso
    # to start with none, and the candidates count in Needs attention), so the shadow IT spec gets its own.
    "shadow_it": (SHADOW_IT_ORG, SHADOW_IT_USERS, "treyresearch"),
    # Offboarding and access review. Reassigning moves every role a person holds in the organization and
    # the access review lists every record, so the specs get their own organization.
    "offboarding": (OFFBOARDING_ORG, OFFBOARDING_USERS, "lamna"),
    # Catalog as code. A plan covers the whole catalog (its hash changes with any record), so a plan made
    # in Contoso would go stale under the specs sharing it.
    "catalog_code": (CATALOG_CODE_ORG, CATALOG_CODE_USERS, "alpineskihouse"),
    # SSO and MFA coverage. Its connector and the applications matched to the simulated tenant's apps are
    # organization-wide (the report lists every application), so the spec gets its own (sso_coverage_scenario).
    "sso_coverage": (SSO_COVERAGE_ORG, SSO_COVERAGE_USERS, "tailspintoys"),
    # Ticket links. Ticket connections are one per system for the whole organization, so the specs get
    # their own, with no connection and no link on every call (tickets_scenario).
    "tickets": (TICKETS_ORG, TICKETS_USERS, "cohowinery"),
    "if_match": (IF_MATCH_ORG, IF_MATCH_USERS, "wingtip"),
}
# Organization fields a spec organization is (re)set to, beyond the defaults.
SPEC_ORG_FIELDS: dict[str, dict] = {
    "views": {"timezone": "Pacific/Auckland", "digest_time": time(8, 0)},
    "if_match": {"review_interval_days": 90},
}


def make_spec_org(which: str = "quality"):
    """An organization of its own for one group of specs, with an Admin (its IT coordinator), an Editor and a Viewer in
    it only, two active people, a team and a vendor. `quality`: the data-quality and attachment-settings specs (rule
    switches, attachment limits and the dashboard's numbers are organization-wide, so they must never move under the
    specs sharing Contoso). `journey`: the AI agent journey (its "Business case" custom field is
    organization-wide too). Idempotent; a rerun puts every rule back on and the attachment limits back to the
    installation's. Returns the organization."""
    from django.contrib.auth import get_user_model

    from catalog import quality
    from catalog.attachments import ORG_KEYS
    from catalog.management.commands.seed_demo import DEMO_PASSWORD
    from catalog.models import Department, Person, Team, Vendor
    from core.models import Membership, Organization

    name, users, domain = SPEC_ORGS[which]
    org, _ = Organization.objects.get_or_create(name=name)
    User = get_user_model()
    for email, first, last, role in users:
        user, created = User.objects.get_or_create(
            username=email, defaults={"email": email, "first_name": first, "last_name": last}
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        Membership.objects.get_or_create(user=user, organization=org, defaults={"role": role})
        if role == "admin":
            Organization.objects.filter(pk=org.pk, coordinator=None).update(coordinator=user)
    for person, email, dept in ORG_PEOPLE:
        department, _ = Department.objects.get_or_create(organization=org, name=dept)
        Person.objects.get_or_create(
            organization=org, name=person, defaults={"email": email.format(domain=domain), "department": department}
        )
    Team.objects.get_or_create(organization=org, name="Platform Team")
    Vendor.objects.get_or_create(organization=org, name="Northwind Traders")
    reset = {k: v for k, v in (org.settings or {}).items() if k not in (*ORG_KEYS, quality.SETTING)}
    Organization.objects.filter(pk=org.pk).update(settings=reset, **SPEC_ORG_FIELDS.get(which, {}))
    org.refresh_from_db()
    if which == "vendor_risk":
        vendor_risk_scenario(org)
    if which == "lifecycle":
        lifecycle_scenario(org)
    if which == "sso_coverage":
        sso_coverage_scenario(org)
    if which == "tickets":
        tickets_scenario(org)
    return org


# (name, the simulated tenant's app it is or None, criticality, sso_enabled, scim_provisioning): the coverage report's
# applications. Workday's record says no SSO and Zoom's says SCIM is on, where Entra says otherwise ("source differs").
SSO_COVERAGE_RECORDS: tuple[tuple[str, str | None, str, bool | None, bool | None], ...] = (
    ("Workday", "Workday", "critical", False, True),
    ("Zoom", "Zoom", "high", True, True),
    ("ServiceNow", "ServiceNow", "high", None, None),
    ("Expense Hub", "Expense Hub", "medium", True, None),
    ("Finance Ledger", "Finance Ledger SSO", "critical", None, None),
    ("Contract Vault", "Contract Vault", "low", None, None),
    ("Legacy Intranet", None, "", None, None),
)


def sso_coverage_scenario(org):
    """The SSO and MFA coverage scenario, put back on every call through the services (which audit it): seven live
    applications, six matched by entra_app_id to the simulated tenant's apps (discovery/entra_simulator.py: Workday SAML
    with SCIM on and MFA for everyone; Zoom SAML by its certificate, SCIM paused, MFA only report-only; ServiceNow no
    mode, SCIM quarantined, MFA as one option; Expense Hub OIDC, MFA by strength; Finance Ledger password SSO, MFA for
    administrators only; Contract Vault no SSO, MFA behind an application filter) and Legacy Intranet not linked. The
    connector and its run are the spec's (a tested connection on the simulated tenant), as for shadow IT."""
    from django.contrib.auth import get_user_model

    from catalog import services
    from catalog.models import Resource
    from discovery.entra_simulator import _id

    editor = get_user_model().objects.get(username=SSO_COVERAGE_USERS[1][0])
    for name, app, criticality, sso, scim in SSO_COVERAGE_RECORDS:
        data: dict[str, Any] = {
            "lifecycle": "active",
            "criticality": criticality,
            "entra_app_id": _id(f"app/{app}") if app else "",
            "sso_enabled": sso,
            "scim_provisioning": scim,
        }
        record = Resource.objects.filter(organization=org, name=name, kind="application").first()
        if record is None:
            services.create_resource(org, editor, {"name": name, "kind": "application", **data})
        else:
            services.update_resource(org, editor, record, data)


def tickets_scenario(org):
    """The ticket links specs start with no ticket connection and no link (test-only synthetic rows, deleted directly,
    without an audit event). Their assessments and renewals are the specs' own, made through the API."""
    from tickets.models import TicketConnection, TicketLink

    TicketLink.objects.filter(organization=org).delete()
    TicketConnection.objects.filter(organization=org).delete()


# endoflife.date products for the lifecycle scenario: (name, label, {cycle: (eol in days from today, active support
# ended days ago or None)}). The cache is the installation's; no other spec tracks these products.
LIFECYCLE_PRODUCTS = (
    ("postgresql", "PostgreSQL", {"17": (1000, None), "16": (300, None), "14": (60, None), "12": (-200, None)}),
    ("ubuntu", "Ubuntu", {"22.04": (400, 30)}),
)
# (name, kind, version, product, cycle or None, criticality, data classification); product None: not tracked.
LIFECYCLE_RECORDS = (
    ("Ledger DB", "application", "12.4", "postgresql", None, "high", "restricted"),
    ("Payments API", "application", "14.2", "postgresql", None, "critical", "confidential"),
    ("Intranet", "application", "16.1", "postgresql", None, "low", "internal"),
    ("Mystery App", "application", "99.1", "postgresql", None, "medium", None),
    ("Untracked App", "application", "17.0", None, None, "medium", None),
    ("Build Server", "component", "", "ubuntu", "22.04", None, "internal"),
    ("HR Core", "component", "", None, None, None, "restricted"),
    ("Payroll Cloud", "application", "", None, None, "high", "internal"),
    ("CRM", "application", "", None, None, "medium", "confidential"),
)
LIFECYCLE_VENDORS = {"Payroll Cloud": "Fabrikam Payroll", "CRM": "Northwind Traders"}
# Owners (ORG_PEOPLE) for the calendar's owner filter: {record: (business owner, technical owner)}.
LIFECYCLE_OWNERS = {"Ledger DB": ("Ines Okafor", None), "Payments API": (None, "Mateo Silva")}
# (name, source, target, classification, categories, auth method): the data-flow map's flows.
LIFECYCLE_FLOWS: tuple[tuple[str, str, str, str, list[str], str], ...] = (
    ("Payroll export", "HR Core", "Payroll Cloud", "restricted", ["personal", "financial"], ""),
    ("CRM sync", "CRM", "HR Core", "confidential", ["customer_content"], "oauth"),
    ("Marketing feed", "HR Core", "CRM", "internal", [], "api_key"),
    ("Bare link", "CRM", "Payroll Cloud", "", [], "none"),
)


def lifecycle_scenario(org):
    """The end-of-life calendar and data-flow map scenario, put back on every call (records are made once through the
    services, then their tracking, classifications and categories are reset through the services, which audit them;
    the shared endoflife.date cache rows, instance tracking rows and the two queues' snoozes are deleted directly,
    without an audit event, since this is test-only synthetic data):

    - endoflife.date cache: PostgreSQL 12 (end of life 200 days ago), 14 (in 60 days), 16 (in 300 days), 17 (in
      1,000 days); Ubuntu 22.04 (security fixes only, end of life in 400 days).
    - Tracked: Ledger DB 12.4 (past end of life), Payments API 14.2 (ends in 60 days), Intranet 16.1 (supported),
      Mystery App 99.1 (a version that matches no cycle: unknown), Build Server Ubuntu 22.04 (component); Ines Okafor
      owns Ledger DB (business), Mateo Silva Payments API (technical). Untracked
      App (17.0) isn't tracked; a spec links it.
    - Flows: Payroll export (restricted personal and financial data from HR Core to Payroll Cloud, whose vendor
      Fabrikam Payroll has no DPA, no authentication recorded: all three risks), CRM sync (confidential, OAuth, clean),
      Marketing feed (internal, no categories, API key, clean until a spec adds personal data), Bare link (no
      classification, no authentication to a vendor: no auth method, and the flow_classification data-quality rule).
    Every record and flow has the Editor as IT steward. Needs attention snoozes of the two queues are removed."""

    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from catalog import services
    from catalog.models import Person, Resource, Vendor
    from core.dates import org_today
    from eol import services as eol_services
    from eol.models import EolProduct, EolRelease, TrackedVersion
    from notify.models import NotificationSnooze

    editor = get_user_model().objects.get(username=LIFECYCLE_USERS[1][0])
    today = org_today(org)
    now = timezone.now()
    for name, label, cycles in LIFECYCLE_PRODUCTS:
        product, _ = EolProduct.objects.update_or_create(
            name=name,
            defaults={
                "label": label,
                "category": "database" if name == "postgresql" else "os",
                "listed": True,
                "link": f"https://endoflife.date/{name}",
                "releases_fetched_at": now,
                "releases_checked_at": now,
                "error": "",
                "renamed_to": "",
            },
        )
        EolRelease.objects.filter(product=product).delete()
        for position, (cycle, (eol_days, eoas_ago)) in enumerate(cycles.items()):
            EolRelease.objects.create(
                product=product,
                name=cycle,
                label=f"{label} {cycle}",
                position=position,
                release_date=today - timedelta(days=1500 - position),
                is_eol=eol_days <= 0,
                eol_from=today + timedelta(days=eol_days),
                has_eoas=eoas_ago is not None,
                is_eoas=True if eoas_ago is not None else None,
                eoas_from=today - timedelta(days=eoas_ago) if eoas_ago is not None else None,
                is_lts=name == "ubuntu",
                latest=f"{cycle}.9",
                latest_date=today - timedelta(days=20),
            )
    vendors = {}
    for vendor_name in set(LIFECYCLE_VENDORS.values()):
        vendors[vendor_name], _ = Vendor.objects.get_or_create(organization=org, name=vendor_name)
    people = {p.name: p for p in Person.objects.filter(organization=org)}
    records = {}
    for name, kind, version, product_name, record_cycle, criticality, classification in LIFECYCLE_RECORDS:
        business, technical = LIFECYCLE_OWNERS.get(name, (None, None))
        data: dict[str, Any] = {
            "criticality": criticality,
            "data_classification": classification or "",
            "it_steward": editor.id,
            "business_owner": str(people[business].id) if business else None,
            "technical_owner": str(people[technical].id) if technical else None,
        }
        if kind == "application":
            vendor = LIFECYCLE_VENDORS.get(name)
            data |= {"version": version, "vendor": str(vendors[vendor].id) if vendor else None}
        record = Resource.objects.filter(organization=org, name=name, kind=kind).first()
        if record is None:
            record = services.create_resource(org, editor, {"name": name, "kind": kind, "lifecycle": "active", **data})
        else:
            services.update_resource(org, editor, record, {"lifecycle": "active", **data})
        records[name] = record
        TrackedVersion.objects.filter(organization=org, resource=record).exclude(instance__isnull=True).delete()
        if product_name is None:
            eol_services.untrack(org, editor, record)
        else:
            eol_services.track(org, editor, record, {"product": product_name, "version": "", "cycle": record_cycle})
    for name, source, target, classification, categories, auth in LIFECYCLE_FLOWS:
        flow_data: dict[str, Any] = {
            "data_classification": classification,
            "data_categories": categories,
            "auth_method": auth,
            "direction": "one_way",
            "it_steward": editor.id,
        }
        flow = Resource.objects.filter(organization=org, name=name, kind="integration").first()
        if flow is None:
            services.create_integration(
                org,
                editor,
                {
                    "name": name,
                    "lifecycle": "active",
                    "source": str(records[source].id),
                    "target": str(records[target].id),
                    **flow_data,
                },
            )
        else:
            services.update_integration(org, editor, flow, {"lifecycle": "active", **flow_data})
    NotificationSnooze.objects.filter(organization=org, subject_type__in=("end_of_life", "data_flows")).delete()


def vendor_risk_scenario(org):
    """seed_demo's vendor risk scenario on fresh risk files: every vendor's file in the organization is removed first
    (through catalog/vendor_risk.remove_for, audited), so each call starts the specs from the same state."""
    from django.contrib.auth import get_user_model

    from catalog import vendor_risk
    from catalog.management.commands.seed_demo import Command
    from catalog.models import Vendor
    from notify.models import NotificationSnooze, Reminder, ReminderStatus

    editor = get_user_model().objects.get(username=VENDOR_RISK_USERS[1][0])
    for name in ("Workday Inc.", "Zoom", "Zylker Software"):
        Vendor.objects.get_or_create(organization=org, name=name)
    with transaction.atomic():
        for vendor in Vendor.objects.filter(organization=org):
            vendor_risk.remove_for(org, editor, vendor)
        Command().vendor_risk_scenario(org, editor)
    # After the commit, which regenerated the reminders: what a spec did to the queue and the reminders goes too (a
    # Needs attention snooze of a vendor_risk row, a reminder snoozed or acknowledged).
    NotificationSnooze.objects.filter(organization=org, subject_type="vendor_risk").delete()
    Reminder.objects.filter(organization=org, status__in=(ReminderStatus.SNOOZED, ReminderStatus.ACKNOWLEDGED)).update(
        status=ReminderStatus.OPEN, snoozed_until=None, acknowledged_at=None, acknowledged_by=None
    )


@api_view(["POST"])
@min_role(ADMIN)
def spec_org(request):
    """Make sure a synthetic organization of its own (`which`: a SPEC_ORGS key, such as quality, journey, contracts,
    notify, views, discovery, reconcile, webhooks, sso, vendor_risk, shadow_it, offboarding, catalog_code, sso_coverage or tickets) and its three
    accounts (demo password) exist, with every data-quality rule on and the installation's
    attachment limits (make_spec_org). With `simulate` instead, set a connector's simulator config (_simulate). Synthetic data in
    the throwaway e2e database only."""
    _, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    if "simulate" in data:
        return _simulate(request, data["simulate"])
    which = data.get("which", "quality")
    if which not in SPEC_ORGS:
        return Response(
            {"detail": "which: " + ", ".join(SPEC_ORGS) + "."},
            status=400,
        )
    org = make_spec_org(which)
    return Response({"organization": str(org.id), "accounts": {r: e for e, _, _, r in SPEC_ORGS[which][1]}})


def _simulator_key(key) -> bool:
    return str(key).startswith("simulated_") or key == "simulator_changes"


def _simulate(request, spec):
    """`simulate: {connector, config}`: set the synthetic tenant's test-only connector config keys
    (`simulated_*`, such as `simulated_permissions`, read by discovery/entra_simulator.py and the other simulators;
    never settable through the API) on one connector of the caller's organization, replacing any set before. Also
    `simulator_changes`: the simulators' edits in order ({"op", "id", "value"}, as the
    simulate_entra_change command appends them to every simulated connector), here on this one connector only. Only on
    a connector whose connection is the simulated tenant or the synthetic Halo, so nothing here can change how a real
    source is read. Synthetic data in the throwaway e2e database only."""
    from catalog.services import is_uuid
    from core import google_workspace, okta
    from core.halo import is_simulated
    from discovery.models import Connector

    org, _ = org_role(request)
    if not isinstance(spec, dict) or not isinstance(spec.get("config"), dict):
        return Response({"detail": "simulate: {connector: id, config: {simulated_...: value}}."}, status=400)
    if any(not _simulator_key(k) for k in spec["config"]):
        return Response({"detail": "Only simulated_* keys and simulator_changes can be set."}, status=400)
    wanted = spec.get("connector")
    if not is_uuid(wanted):
        return Response({"detail": "connector: a connector ID."}, status=400)
    connector = (
        Connector.objects.filter(organization=org, pk=str(wanted))
        .select_related("microsoft_connection", "halo_connection", "okta_connection", "google_workspace_connection")
        .first()
    )
    if connector is None:
        return Response({"detail": "No such connector in this organization."}, status=404)
    microsoft, halo_ = connector.microsoft_connection, connector.halo_connection
    okta_, google = connector.okta_connection, connector.google_workspace_connection
    if not (
        (microsoft and microsoft.tenant_id == "simulated")
        or (halo_ and is_simulated(halo_.base_url))
        or (okta_ and okta.is_simulated(okta_.base_url))
        or (google and google_workspace.is_simulated(google.admin_email))
    ):
        return Response(
            {"detail": "Only a connector on the simulated tenant or the synthetic Halo, Okta or Workspace."},
            status=400,
        )
    config = {k: v for k, v in (connector.config or {}).items() if not _simulator_key(k)}
    Connector.objects.filter(pk=connector.pk).update(config={**config, **spec["config"]})
    return Response({"connector": str(connector.id), "config": spec["config"]})


# ---- Licence observations -------------------------------------------------------------------------------

LICENCE_CONNECTOR = "Entra licenses (synthetic)"


@api_view(["POST"])
@min_role(ADMIN)
def license_observation(request):
    """Record a synthetic discovery observation of kind `license` in the caller's organization, as the Entra
    licences source will (one SKU's purchased and assigned seats, from an enabled connector named `connector`, default
    "Entra licenses (synthetic)"), so the seat review can be checked without a tenant. Body: `sku_id`, optional
    `sku_part_number`, `seats_purchased`, `seats_assigned`, `connector`. Updates the same SKU's observation from that
    connector on a rerun. Synthetic data in the throwaway e2e database only."""
    from django.utils import timezone

    from discovery.models import Connector, Observation

    org, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    sku_id = str(data.get("sku_id") or "").strip()
    if not sku_id:
        return Response({"detail": "sku_id is required."}, status=400)
    name = str(data.get("connector") or LICENCE_CONNECTOR)
    connector = Connector.objects.filter(organization=org, name=name).first() or Connector.objects.create(
        organization=org, source="entra", name=name, enabled=True
    )
    part = str(data.get("sku_part_number") or sku_id.upper())
    purchased, assigned = int(data.get("seats_purchased", 0)), int(data.get("seats_assigned", 0))
    now = timezone.now()
    observation, _ = Observation.objects.update_or_create(
        connector=connector,
        kind="license",
        external_id=sku_id.lower(),
        defaults={
            "organization": org,
            "display_name": part,
            "data": {
                "sku_id": sku_id,
                "sku_part_number": part,
                "seats_purchased": purchased,
                "seats_assigned": assigned,
            },
            "fingerprint": f"{purchased}:{assigned}",
            "first_seen_at": now,
            "last_seen_at": now,
            "last_changed_at": now,
            "gone_at": None,
        },
    )
    return Response({"observation": str(observation.id), "connector": str(connector.id)})


# ---- The assistant ------------------------------------------------------------------------------------

ASSISTANT_ORG = "Contoso Assistant (synthetic)"
ASSISTANT_USERS = (
    ("as-admin@example.test", "Ash", "Admin", "admin"),
    ("as-editor@example.test", "Avery", "Editor", "editor"),
    ("as-viewer@example.test", "Alex", "Viewer", "viewer"),
)
SPEC_ORGS["assistant"] = (ASSISTANT_ORG, ASSISTANT_USERS, "contoso-assistant")
# A community organization with a connector and a candidate, for the queue without the capability.
ASSISTANT_COMMUNITY_ORG = "Fabrikam Assistant Community (synthetic)"
ASSISTANT_COMMUNITY_USERS = (
    ("ac-admin@example.test", "Arlo", "Admin", "admin"),
    ("ac-editor@example.test", "Aria", "Editor", "editor"),
    ("ac-viewer@example.test", "Abe", "Viewer", "viewer"),
)
SPEC_ORGS["assistant_community"] = (ASSISTANT_COMMUNITY_ORG, ASSISTANT_COMMUNITY_USERS, "fabrikam-assistant")
ASSISTANT_CONNECTOR = "Assistant Entra (synthetic)"
# The spec organization's ID, stored by make_assistant_org in this (the e2e server's) process: the fake client and the
# fixture's observe and run act for that organization only, never one that merely has its name. None until setup.
_assistant_org_id: str | None = None
# The candidates the setup observes, and what the fake model says about each (_FakeMessages below): a candidate with an
# offered record is linked to it, one named "Test ..." dismissed, "Mystery ..." answered with a record that wasn't
# offered (so the suggestion is discarded as failed), anything else created. A call carrying a candidate named
# "Declined ..." is refused by the model as a whole (stop_reason refusal), as the real API refuses a request.
ASSISTANT_CANDIDATES = ("Zoom", "Expense Hub", "Test registration 7", "Mystery Widget", "Old Notes App")
ASSISTANT_RECORDS = ("Zoom",)
# The fake call's token counts (input, output): 0.0042 USD a call at the code price table, against a worst case of
# about 0.0236 USD reserved (count_tokens answers 1,000), so a cap of 0.01 USD stops the next call.
FAKE_USAGE = (1200, 180)


class _FakeMessages:
    """messages.count_tokens and messages.create shaped like the SDK's answers (stop_reason, content, usage); no
    network. A link reason carries markup and an address on purpose: the SPA must show it as plain text."""

    def count_tokens(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(input_tokens=1000)

    def create(self, **kwargs):
        import json
        from types import SimpleNamespace

        content = kwargs["messages"][0]["content"]
        candidates = json.loads(content.split("\n", 1)[1])["candidates"]
        inp, out = FAKE_USAGE
        usage = SimpleNamespace(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        if any(c["name"].startswith("Declined ") for c in candidates):
            details = SimpleNamespace(type="refusal", category="e2e")
            return SimpleNamespace(stop_reason="refusal", stop_details=details, content=[], usage=usage)
        rows = []
        for c in candidates:
            name, records = c["name"], c["records"]
            if name.startswith("Test "):
                row = {"action": "dismiss", "record": "", "confidence": 0.62, "reason": "A developer's test app."}
            elif name.startswith("Mystery "):
                row = {"action": "link", "record": "c9-r9", "confidence": 0.4, "reason": "Looks familiar."}
            elif records:
                row = {
                    "action": "link",
                    "record": records[0]["record"],
                    "confidence": 0.93,
                    "reason": f"<b>Same</b> name as the record {records[0]['name']}; see https://example.test/x.",
                }
            else:
                row = {"action": "create", "record": "", "confidence": 0.71, "reason": "Real software the team uses."}
            rows.append({"candidate": c["candidate"], **row})
        return SimpleNamespace(
            stop_reason="end_turn",
            stop_details=None,
            content=[SimpleNamespace(type="text", text=json.dumps({"suggestions": rows}))],
            usage=usage,
        )


class FakeAnthropic:
    def __init__(self):
        self.messages = _FakeMessages()

    def with_options(self, **options):
        return self


def _assistant_org_from_slug() -> str | None:
    """The assistant spec organization's ID read from its slug, when this process didn't make it (the e2e server
    restarted after make_assistant_org)."""
    from django.utils.text import slugify

    from core.models import Organization

    # make_spec_org creates it by name, so on the e2e database it holds the name's own slug (a later organization of
    # the same name would get `-2`, never this one).
    found = Organization.objects.filter(slug=slugify(ASSISTANT_ORG)).values_list("id", flat=True).first()
    return str(found) if found else None


def _e2e_make_client(key: str, http_client=None):
    """The e2e server's discovery.assistant.make_client: the fake client only for the key the assistant spec
    organization stored; any other key (another organization's, the installation's) is refused, so the e2e server
    never reaches the Anthropic API."""
    from core import credentials
    from discovery.models import AssistantSettings

    org_id = _assistant_org_id or _assistant_org_from_slug()
    row = AssistantSettings.objects.filter(organization_id=org_id).first() if org_id else None
    try:
        theirs = credentials.slot_value(row, "api_key") if row is not None and row.api_key_ciphertext else ""
    except credentials.CredentialError:
        theirs = ""
    if not key or key != theirs:
        raise RuntimeError("The e2e server never calls the Anthropic API (only the assistant spec org is faked).")
    return FakeAnthropic()


def _external_id(name: str) -> str:
    return "e2e-" + name.lower().replace(" ", "-")


def _observe(connector, name: str):
    import hashlib

    from django.utils import timezone

    from discovery.models import Observation

    now = timezone.now()
    found = Observation.objects.filter(connector=connector, external_id=_external_id(name)).first()
    if found is not None:
        return found
    return Observation.objects.create(
        organization_id=connector.organization_id,
        connector=connector,
        kind="application",
        external_id=_external_id(name),
        display_name=name,
        data={"description": f"{name}, reported by the synthetic tenant", "publisher": "Contoso"},
        fingerprint=hashlib.sha256(name.encode()).hexdigest(),
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )


def make_assistant_org():
    """The assistant spec organization (enterprise edition; every edition has the capability) with an Admin, an Editor and a
    Viewer, a tested connector on the simulated tenant, the record Zoom and four undecided candidates. Every assistant
    row (settings, batches, suggestions, usage, queued jobs) and every decision is removed, so a rerun starts again
    with the assistant off. Returns (organization, connector)."""
    from django.contrib.auth import get_user_model

    from catalog import services
    from catalog.models import Resource
    from core import capabilities, connections
    from core.models import Job, JobStatus
    from discovery import assistant
    from discovery import engine as discovery_engine
    from discovery.models import (
        AssistantBatch,
        AssistantSettings,
        AssistantSuggestion,
        AssistantUsage,
        Connector,
        Observation,
        ReconciliationDecision,
    )

    global _assistant_org_id
    org = make_spec_org("assistant")
    org, _ = capabilities.set_edition(org, "enterprise", via="e2e")
    _assistant_org_id = str(org.id)
    admin = get_user_model().objects.get(username=ASSISTANT_USERS[0][0])
    AssistantSuggestion.objects.filter(organization=org).delete()
    AssistantUsage.objects.filter(organization=org).delete()
    AssistantBatch.objects.filter(organization=org).delete()
    AssistantSettings.objects.filter(organization=org).delete()
    Job.objects.filter(organization=org, kind=assistant.JOB, status=JobStatus.QUEUED).delete()
    ReconciliationDecision.objects.filter(organization=org).delete()
    connector = Connector.objects.filter(organization=org, name=ASSISTANT_CONNECTOR).first()
    if connector is None:
        connector = discovery_engine.create_connector(
            org, admin, {"name": ASSISTANT_CONNECTOR, "tenant_id": "simulated", "client_id": "", "secret": "synthetic"}
        )
        connections.run_test(org, admin, connector.microsoft_connection)
    Observation.objects.filter(connector=connector).exclude(
        external_id__in=[_external_id(n) for n in ASSISTANT_CANDIDATES]
    ).delete()
    for name in ASSISTANT_RECORDS:
        if not Resource.objects.filter(organization=org, name=name, archived_at__isnull=True).exists():
            services.create_resource(org, admin, {"name": name, "kind": "application", "lifecycle": "active"})
    for name in ASSISTANT_CANDIDATES:
        _observe(connector, name)
    return org, connector


def make_assistant_community_org():
    """A community organization whose assistant was turned off (a revoke of `assistant`, as platform staff would set),
    with a tested connector on the simulated tenant and one undecided candidate. Returns the organization."""
    from django.contrib.auth import get_user_model

    from core import connections
    from discovery import engine as discovery_engine
    from discovery.models import Connector

    org = make_spec_org("assistant_community")
    admin = get_user_model().objects.get(username=ASSISTANT_COMMUNITY_USERS[0][0])
    connector = Connector.objects.filter(organization=org, name=ASSISTANT_CONNECTOR).first()
    if connector is None:
        connector = discovery_engine.create_connector(
            org, admin, {"name": ASSISTANT_CONNECTOR, "tenant_id": "simulated", "client_id": "", "secret": "synthetic"}
        )
        connections.run_test(org, admin, connector.microsoft_connection)
    _observe(connector, "Zoom")
    from core import capabilities
    from core.models import CapabilityOverride

    if not CapabilityOverride.objects.filter(organization=org, capability="assistant").exists():
        CapabilityOverride.objects.create(organization=org, capability="assistant", effect="revoke", reason="e2e spec")
        capabilities.forget_organization(org.pk)
    return org


@api_view(["POST"])
@min_role(ADMIN)
def assistant_org(request):
    """The assistant specs' fixture. `{"setup": true}` from any Admin: make the assistant spec
    organization (make_assistant_org) and answer its accounts. From that organization's Admin only:
    `{"observe": [names]}` adds undecided candidates to its connector, `{"change": [names]}` gives them a new
    fingerprint as a source change would, `{"run": true}` runs its queued assistant batches now, as the worker would,
    through the fake client. `{"community": true}` from any Admin makes the community organization
    (make_assistant_community_org). Synthetic data in the throwaway e2e database only."""
    from core import jobs
    from core.models import Job, JobStatus
    from discovery import assistant
    from discovery.models import Connector, Observation

    org, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    if data.get("setup"):
        made, connector = make_assistant_org()
        accounts = {r: e for e, _, _, r in ASSISTANT_USERS}
        return Response({"organization": str(made.id), "connector": str(connector.id), "accounts": accounts})
    if data.get("community"):
        made = make_assistant_community_org()
        accounts = {r: e for e, _, _, r in ASSISTANT_COMMUNITY_USERS}
        return Response({"organization": str(made.id), "accounts": accounts})
    if _assistant_org_id is None or str(org.id) != _assistant_org_id:
        return Response({"detail": "Only the assistant spec organization's Admin can do this."}, status=403)
    if isinstance(data.get("observe"), list):
        connector = Connector.objects.get(organization=org, name=ASSISTANT_CONNECTOR)
        return Response({"observed": [str(_observe(connector, str(n)[:100]).id) for n in data["observe"]]})
    if isinstance(data.get("change"), list):
        import hashlib

        changed = Observation.objects.filter(
            organization=org,
            connector__name=ASSISTANT_CONNECTOR,
            external_id__in=[_external_id(str(n)) for n in data["change"]],
        )
        for obs in changed:  # as if the source changed it: a new fingerprint, so its suggestion is out of date
            obs.fingerprint = hashlib.sha256(f"{obs.fingerprint}:changed".encode()).hexdigest()
            obs.save(update_fields=["fingerprint"])
        return Response({"changed": len(changed)})
    if data.get("run"):
        ran = []
        queued = Job.objects.filter(organization=org, kind=assistant.JOB, status=JobStatus.QUEUED)
        for job_id in list(queued.order_by("created_at").values_list("id", flat=True)):
            job = jobs.claim(ids=[job_id])
            if job is None:
                continue
            jobs.run(job)
            ran.append((job.payload or {}).get("batch_id"))
        return Response({"batches": ran})
    return Response({"detail": "setup, community, observe, change or run."}, status=400)


# ---- Mail, impersonation and exports (the cloud journey) -------------------------------------------------------------


def _addressed_to(message, wanted: str) -> bool:
    from email.utils import parseaddr

    return any(parseaddr(str(address))[1].lower() == wanted for address in message.to)


@api_view(["POST"])
@min_role(ADMIN)
def outbox(request):
    """The e2e server's in-memory outbox: every email sent since it started (or since it was last cleared), oldest
    first, as {to, subject, body}. Body `to` (an address) keeps the ones sent to it; `clear: true` takes the ones
    returned out of the outbox. Any Admin reads every organization's mail, signup confirmations to people with no
    account included: a signup spec reads its link signed in as a seed Admin. Synthetic data in the throwaway e2e
    database only."""
    _, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    to, clear = data.get("to"), data.get("clear", False)
    if to is not None and (not isinstance(to, str) or not to.strip()):
        return Response({"detail": "to: an email address."}, status=400)
    if not isinstance(clear, bool):
        return Response({"detail": "clear: true or false."}, status=400)
    box = _outbox()
    picked = [m for m in box if to is None or _addressed_to(m, to.strip().lower())]
    found = [_message(m) for m in picked]
    if clear:
        _take(picked)
    return Response(found)


@api_view(["POST"])
@min_role(VIEWER, VIEWER)
def expire_impersonation(request):
    """End this session's impersonation now, as if its time limit had passed: its `expires_at` is set to a second ago,
    so the next request notices (a read restores the staff session and marks it, so the write after is refused; a
    write is refused with /problems/impersonation-ended at once). Answers {"expires_at"}; 409 when the session isn't
    impersonating. Any role, since the member impersonated may be a Viewer; the impersonation route policy lets this
    one test-only route through (core/impersonation_routes.E2E_ALLOWED). Nothing is audited until the end is
    noticed, as with a real expiry."""
    from django.utils import timezone

    from core import impersonation

    django_request = request._request
    state = impersonation.state_of(django_request)
    if state is None or getattr(django_request, "softwarden_impersonator", None) is None:
        return Response({"detail": "This session isn't impersonating anyone."}, status=409)
    expired = (timezone.now() - timedelta(seconds=1)).isoformat()
    django_request.session[impersonation.SESSION_KEY] = {**state, "expires_at": expired}
    return Response({"expires_at": expired})


@api_view(["POST"])
@min_role(ADMIN)
def run_export_jobs(request):
    """Run the queued `org.export` jobs now, as the worker's long lane would: the caller's organization's, or with
    `all: true` every organization's (a deletion's final export, a platform-requested export, a nightly backup). A job
    queued to run later (a nightly backup's offset) runs now too; a suspended organization's stay queued, as in the
    worker. Answers each job run, oldest first: {"job", "organization", "job_status", "export", "purpose", "status"},
    with the export's state afterwards. Synthetic data in the throwaway e2e database only."""
    from django.utils import timezone

    from catalog import org_lifecycle
    from core import jobs
    from core.models import Job, JobStatus, OrgExport

    org, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    every = data.get("all", False)
    if not isinstance(every, bool):
        return Response({"detail": "all: true or false."}, status=400)
    queued = Job.objects.filter(kind=org_lifecycle.EXPORT_JOB, status=JobStatus.QUEUED)
    if not every:
        queued = queued.filter(organization=org)
    ids = list(queued.order_by("created_at").values_list("id", flat=True))
    now = timezone.now()
    Job.objects.filter(id__in=ids, run_after__gt=now).update(run_after=now)
    ran = []
    for job_id in ids:
        job = jobs.claim(ids=[job_id])
        if job is None:  # a suspended organization's, or taken meanwhile
            continue
        job_status = jobs.run(job)
        export = OrgExport.objects.filter(organization_id=job.organization_id, job_id=job.id).first()
        ran.append(
            {
                "job": str(job.id),
                "organization": str(job.organization_id),
                "job_status": job_status,
                "export": str(export.id) if export else None,
                "purpose": export.purpose if export else None,
                "status": export.status if export else None,
            }
        )
    return Response(ran)


@api_view(["POST"])
@min_role(ADMIN)
def run_password_reset_jobs(request):
    """Run the queued `password_reset.send` jobs now, as the worker's next tick would (asking for a reset link
    only queues the job, so every address takes the same time). Answers each job run, oldest first: {"job",
    "job_status"}. Any Admin runs every queued one (they carry an account id or null, nothing else). Synthetic data in
    the throwaway e2e database only."""
    from catalog import password_reset
    from core import jobs
    from core.models import Job, JobStatus

    _, role = org_role(request)
    require_admin(role)
    queued = Job.objects.filter(kind=password_reset.SEND_JOB, status=JobStatus.QUEUED)
    ran = []
    for job_id in list(queued.order_by("created_at").values_list("id", flat=True)):
        job = jobs.claim(ids=[job_id])
        if job is None:  # taken meanwhile (another spec's call)
            continue
        ran.append({"job": str(job.id), "job_status": jobs.run(job)})
    return Response(ran)


@api_view(["POST"])
@min_role(ADMIN)
def kept_ticket_connection(request):
    """A Halo ticket connection kept from when the installation's preview features were on:
    the e2e server runs with them off, as by default, so the API refuses to create one. `{"halo_connection": id,
    "name"}` writes it directly on one of the caller's organization's Halo connections and answers {"id"};
    `{"delete": id}` removes that organization's one again. Test-only synthetic rows, written without an audit event,
    in the throwaway e2e database only."""
    from core.models import HaloConnection
    from tickets.models import TicketConnection, TicketSystem

    org, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    if "delete" in data:
        TicketConnection.objects.filter(organization=org, pk=str(data["delete"]), system=TicketSystem.HALO).delete()
        return Response(status=204)
    halo = HaloConnection.objects.filter(organization=org, pk=str(data.get("halo_connection", ""))).first()
    name = data.get("name")
    if halo is None or not isinstance(name, str) or not name.strip():
        return Response({"detail": "halo_connection: one of this organization's Halo connections; name."}, status=400)
    conn = TicketConnection.objects.create(organization=org, system=TicketSystem.HALO, halo_connection=halo, name=name)
    return Response({"id": str(conn.id)}, status=201)


@api_view(["POST"])
@min_role(ADMIN)
def capability_override(request):
    """Turn one capability off, or back on, for the calling Admin's own organization, through the platform override
    service (catalog/platform_admin, audited as a platform revoke would be, with this Admin as the actor):
    `{"capability": name, "revoked": true|false}` (default true). Lets a spec show a feature as unavailable now that
    every edition has every feature. Synthetic data in the throwaway e2e database only."""
    from catalog import platform_admin
    from core import capabilities
    from core.models import CapabilityOverride

    org, role = org_role(request)
    require_admin(role)
    data = request.data if isinstance(request.data, dict) else {}
    revoked = data.get("revoked", True)
    if not isinstance(revoked, bool):
        return Response({"detail": "revoked: true or false."}, status=400)
    name = capabilities.check_capability_name(data.get("capability"))
    if revoked:
        platform_admin.set_override(request.user, org, {"capability": name, "effect": "revoke", "reason": "e2e spec"})
    else:
        for override in CapabilityOverride.objects.filter(organization=org, capability=name):
            platform_admin.remove_override(request.user, org, override, {"reason": "e2e spec"})
    return Response({"capability": name, "revoked": revoked})
