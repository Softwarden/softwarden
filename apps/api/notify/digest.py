"""The daily digest, the built-in "Daily digest" notification rule.

At the organization's digest time, while the Daily digest rule is on, each User with items routed to them gets one
email listing their Needs attention items, built from the same queues the page shows (notify/attention.attention_queues),
so the two can't disagree. Unrouted items go to the IT coordinator under UNASSIGNED. People without a
User account are never emailed. Renewal actions appear on the day a reminder threshold fires and every day once
overdue; the other queues appear while the item is open. Snoozed and acknowledged items are left out of that user's
email only. Nothing to send means no email.

A run is recorded once per organization and date (DigestRun), and a digest once per user and date (Digest). Since
Each digest is a Delivery (kind `digest`, key `digest:{user_id}:{date}`, notify/deliveries.py): it goes out
through the organization's email settings, shows in the notification log, and is retried by the delivery engine
(1, 4 and 16 minutes, then Failed for Admins to see and retry). Its text is saved when it is built, so a retry sends
the same email.
"""

import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import escape

from catalog.models import KIND_PATHS
from catalog.relationship_types import phrase_of
from money import renewal_rules
from money.models import RenewalCycle

from . import deliveries
from .models import (
    Channel,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    Digest,
    DigestRun,
    DigestStatus,
    NotificationPreference,
    NotificationSnooze,
    ReminderDispatch,
    SnoozeKind,
)

log = logging.getLogger("softwarden.digest")

SECTIONS = [
    ("renewal_actions", "Renewal actions"),
    ("renewal_gaps", "Renewal gaps"),
    ("reviews", "Reviews"),
    ("ownership_gaps", "Ownership gaps"),
    ("agent_gaps", "AI agent gaps"),
    ("links_to_verify", "Links to verify"),
    ("stale_assessments", "Stale assessments"),
    ("discovery_candidates", "Discovery candidates"),
    ("discovery_missing", "No longer seen in source"),
    ("data_quality", "Data quality"),
    ("seat_counts", "Seat counts"),
    ("vendor_risk", "Vendor risk"),
    ("data_flows", "Data flows"),
]
SECTION_NAMES = dict(SECTIONS)
# Queues the page doesn't show yet (attention.UNCOUNTED) aren't digest sections either. None waits now.
PENDING_SECTIONS: dict[str, str] = {}
# Counted queues that are deliberately not digest sections, because a notification rule emails them instead: End of
# life left the digest when its reminders and the built-in "End of life" rule arrived (notify 0012), so nothing is
# emailed twice. Like the Reminders queue, it stays on the Needs attention page and in its counts.
NOT_SECTIONS = {"end_of_life": "emailed by the built-in End of life notification rule (notify 0012)"}
REVIEW_STATES = {"overdue": "Overdue", "needs_confirmation": "Needs confirmation", "unreviewed": "Unreviewed"}


def subject_of(queue, row):
    """The (type, id) a snooze or acknowledgment names for a Needs attention row."""
    if queue == "links_to_verify":
        return queue, f"{row['link_kind']}:{row['id']}"
    return queue, row["id"]


def active_snoozes(org, today, user=None):
    """{user_id: {(type, id): snooze}} for snoozes and acknowledgments that may still hold today."""
    qs = NotificationSnooze.objects.filter(organization=org)
    if user is not None:
        qs = qs.filter(user=user)
    found: dict = {}
    for s in qs:
        if s.until is not None and s.until < today:
            continue
        found.setdefault(s.user_id, {})[(s.subject_type, s.subject_id)] = s
    return found


def is_quiet(snooze, row, org, today):
    """Whether a snooze or acknowledgment keeps this row out of the user's digest today."""
    if snooze is None:
        return False
    if snooze.kind == SnoozeKind.SNOOZE:
        return snooze.until is not None and snooze.until >= today
    # An acknowledgment holds until the next reminder threshold; an overdue one for the day it was made.
    if snooze.threshold == renewal_rules.OVERDUE:
        return snooze.until is not None and snooze.until >= today
    current = renewal_rules.bucket(_date(row.get("deadline")), today, org.reminder_thresholds)
    return current == snooze.threshold


def _date(value):
    return datetime.fromisoformat(value).date() if value else None


def _path(queue, row):
    if queue in ("renewal_actions", "renewal_gaps"):
        return f"/renewals/{row['id']}"
    if queue == "stale_assessments":
        return f"/assessments/{row['id']}"
    if queue == "discovery_candidates":
        return f"/discovery?connector={row['id']}"
    if queue == "discovery_missing":
        return f"/discovery?status=missing&connector={row['connector']['id']}"
    if queue == "data_quality":
        steward = row["it_steward"] if row["it_steward"] is not None else "none"
        return f"/data-quality?rule={row['rule']}&it_steward={steward}"
    if queue == "seat_counts":
        return f"/license-pools/{row['id']}?tab=review"
    if queue == "vendor_risk":
        return f"/vendors/{row['id']}?tab=risk"
    if queue == "data_flows":
        return f"/integrations/{row['id']}"
    if queue == "links_to_verify":
        if row["link_kind"] == "integration":
            return f"/integrations/{row['id']}"
        return f"/{KIND_PATHS[row['record']['kind']]}/{row['record']['id']}?tab=relationships"
    return f"/{KIND_PATHS[row['kind']]}/{row['id']}"


def _line(queue, row):
    """One item in words, as in the wireframe: the name, then what needs doing."""
    if queue == "renewal_actions":
        parts = [row["name"], f"action deadline {row['deadline']} ({row['label_text']})", row["timing"]["text"]]
        if row["decision"]:
            parts.append(f"decision: {row['decision'].capitalize()}, not completed")
        return " — ".join(parts)
    if queue == "renewal_gaps":
        return f"{row['name']} — {'; '.join(row['gaps'])}"
    if queue == "reviews":
        return f"{row['name']} — {REVIEW_STATES[row['state']]}: {row['why']}"
    if queue in ("ownership_gaps", "agent_gaps"):
        return f"{row['name']} — {', '.join(row['gaps'])}"
    if queue == "links_to_verify":
        if row["link_kind"] == "integration":
            name = row["record"]["name"]
        else:
            verb = phrase_of(row["type_name"], row["type"])  # "Depends on" reads "depends on"
            name = f"{row['source']['name']} {verb} {row['target']['name']}"
        return f"{name} — {row['why']}"
    if queue in ("discovery_candidates", "discovery_missing"):
        return f"{row['name']} — {row['why']}"
    if queue == "data_quality":
        return f"{row['label']} — {row['count']} record{'' if row['count'] == 1 else 's'}"
    if queue == "seat_counts":
        return f"{row['name']} ({row['application']['name']}) — {row['why']}"
    if queue == "vendor_risk":
        return f"{row['name']} — {', '.join(g['label'] for g in row['gaps'])}"
    if queue == "data_flows":
        # Each risk once: a two-way flow raises one per receiving end.
        return f"{row['name']} — {', '.join(dict.fromkeys(r['label'] for r in row['risks']))}"
    return f"{row['title']} — {row['why']}"


def plan(org, today):
    """{user_id: [(queue, row, threshold or None, unassigned)]} for every user with something to send, and the
    reminder thresholds that fire today ({cycle_id: threshold}). Built from one read of the queues."""
    from .attention import attention_queues

    queues, _ = attention_queues(org, SimpleNamespace(id=None), today)
    open_cycles = [r["cycle"] for r in queues["renewal_actions"]]
    cycles = {
        str(c.id): c
        for c in RenewalCycle.objects.filter(organization=org, id__in=open_cycles).only("id", "deadline_set_on")
    }
    dispatched: dict = {}
    for cycle_id, sent in ReminderDispatch.objects.filter(organization=org, cycle_id__in=open_cycles).values_list(
        "cycle_id", "threshold_days"
    ):
        dispatched.setdefault(str(cycle_id), set()).add(sent)
    snoozes = active_snoozes(org, today)
    items: dict = {}
    fires: dict = {}
    for queue, _label in SECTIONS:
        for row in queues[queue]:
            steward = row["steward"]
            user_id = steward.get("id")
            if user_id is None:  # no coordinator: nobody to tell (the page shows "Unassigned")
                continue
            threshold: int | None = None
            if queue == "renewal_actions":
                if not row["timing"]["overdue"]:
                    cycle = cycles.get(row["cycle"])
                    threshold = renewal_rules.due_threshold(
                        _date(row["deadline"]),
                        today,
                        org.reminder_thresholds,
                        cycle.deadline_set_on if cycle else None,
                        dispatched.get(row["cycle"], set()),
                    )
                    if threshold is None:
                        continue  # not a reminder day for this action
            if is_quiet(snoozes.get(user_id, {}).get(subject_of(queue, row)), row, org, today):
                continue
            items.setdefault(user_id, []).append((queue, row, threshold, bool(steward.get("coordinator"))))
            if threshold is not None:
                fires[row["cycle"]] = threshold
    return items, fires


def recipients(org):
    """Active Users of the organization who have digests on (the preference of their membership here) and an email
    address. Owners who are only People are never here."""
    off = set(
        NotificationPreference.objects.filter(organization=org, digest_enabled=False).values_list(
            "membership__user_id", flat=True
        )
    )
    users = get_user_model().objects.filter(membership__organization=org, is_active=True).exclude(email="")
    return {u.id: u for u in users if u.id not in off}


def greeting_name(user):
    return user.first_name or user.get_full_name() or user.email.split("@")[0]


def render(org, user, entries, today):
    """(subject, text, html) for one user's digest: plain text first, with an HTML part of the same sections."""
    base = settings.SOFTWARDEN_BASE_URL
    count = len(entries)
    day = today.strftime("%a ") + today.isoformat()
    subject = f"Softwarden: {count} item{'s' if count != 1 else ''} need{'s' if count == 1 else ''} attention ({day})"
    sections = []
    for queue, label in SECTIONS:
        rows = [
            (_line(queue, r), base + _path(queue, r))
            for q, r, _t, unassigned in entries
            if q == queue and not unassigned
        ]
        if rows:
            sections.append((label, rows))
    unrouted = [
        (f"{SECTION_NAMES[q]}: {_line(q, r)}", base + _path(q, r)) for q, r, _t, unassigned in entries if unassigned
    ]
    if unrouted:
        sections.append(("Unassigned", unrouted))
    intro = (
        "These items are assigned to you as IT steward or follow-up, or because you're the IT coordinator. "
        "Open Softwarden to act on them."
    )
    footer = [
        "Snoozed items are left out of this email but stay in Needs attention.",
        f"Open Needs attention: {base}/",
        "You get this email because digests are on for your account. Turn them off in your profile.",
    ]
    text = [f"Hi {greeting_name(user)},", "", intro, ""]
    for label, rows in sections:
        text.append(f"{label.upper()} ({len(rows)})")
        for line, url in rows:
            text += [f"  • {line}", f"    {url}"]
        text.append("")
    text += footer
    html = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#141a24">',
        f"<p>Hi {escape(greeting_name(user))},</p>",
        f"<p>{escape(intro)}</p>",
    ]
    for label, rows in sections:
        html.append(f'<h2 style="font-size:17px;margin:20px 0 6px">{escape(label)} ({len(rows)})</h2><ul>')
        for line, url in rows:
            name, _, rest = line.partition(" — ")
            tail = f" — {escape(rest)}" if rest else ""
            html.append(f'<li><a href="{escape(url)}">{escape(name)}</a>{tail}</li>')
        html.append("</ul>")
    html.append(f'<p style="color:#535e6e">{escape(footer[0])}<br>')
    html.append(f'<a href="{escape(base)}/">Open Needs attention</a><br>{escape(footer[2])}</p></div>')
    return subject, "\n".join(text) + "\n", "".join(html)


def local_now(org, now=None):
    return (now or timezone.now()).astimezone(ZoneInfo(org.timezone))


def run_for_org(org, now=None):
    """Build today's digests and queue their deliveries if the digest time has passed, the Daily digest rule is on
    and today's run hasn't happened. Returns the run, or None when nothing was due. Sending is the delivery engine's
    (a `notify.deliver` job per digest), so a slow mail server never blocks building."""
    from .rule_services import digest_rule

    now = now or timezone.now()
    local = local_now(org, now)
    if local.time() < org.digest_time:
        return None
    rule = digest_rule(org)
    if rule is None or not rule.enabled:
        return None
    today = local.date()
    if DigestRun.objects.filter(organization=org, for_date=today).exists():
        return None
    try:
        with transaction.atomic():
            run = DigestRun.objects.create(organization=org, for_date=today)
            items, fires = plan(org, today)
            people = recipients(org)
            for user_id, entries in items.items():
                user = people.get(user_id)
                if user is None:  # digests off, inactive or no email
                    continue
                subject, text, html = render(org, user, entries, today)
                delivery, _created = deliveries.enqueue(
                    org,
                    channel=Channel.EMAIL,
                    kind=DeliveryKind.DIGEST,
                    dedupe_key=delivery_key(user.id, today),
                    subject=subject,
                    content={"text": text, "html": html},
                    recipient=user.email,
                    rule=rule,
                    rule_name=rule.name,
                )
                digest = Digest.objects.create(
                    organization=org, run=run, user=user, for_date=today, item_count=len(entries), delivery=delivery
                )
                for queue, row, threshold, _unassigned in entries:
                    if queue == "renewal_actions" and threshold is not None:
                        ReminderDispatch.objects.get_or_create(
                            organization=org,
                            cycle_id=row["cycle"],
                            threshold_days=threshold,
                            defaults={"digest": digest},
                        )
            run.finished_at = timezone.now()
            run.save(update_fields=["finished_at"])
    except IntegrityError:  # another worker recorded today's run first
        return None
    log.info("digest run built", extra={"organization": str(org.id), "for_date": today.isoformat()})
    return run


def delivery_key(user_id, day) -> str:
    """One digest per user per local date, whatever reruns or workers do."""
    return f"digest:{user_id}:{day.isoformat()}"


def send_now(org, day, now=None, rounds=1) -> None:
    """Attempt the day's queued digest deliveries now, `rounds` times over (with `now` far enough ahead, each round is
    the next retry). For the e2e endpoint and the timing command; otherwise the worker's jobs send them."""
    ids = list(
        Digest.objects.filter(organization=org, for_date=day)
        .exclude(delivery=None)
        .values_list("delivery_id", flat=True)
    )
    base = now or timezone.now()
    for step in range(rounds):  # each round an hour later, past the previous round's backoff (at most 16 minutes)
        when = base + timedelta(hours=step)
        for delivery_id in ids:
            deliveries.attempt(delivery_id, when)


def discard_day(org, day) -> None:
    """Remove a day's digest run, digests and their deliveries, so the day can be built again. Test and timing tools
    only (the e2e endpoint, nfr_timing); nothing in the product deletes deliveries."""
    Delivery.objects.filter(
        organization=org,
        kind=DeliveryKind.DIGEST,
        dedupe_key__startswith="digest:",
        dedupe_key__endswith=f":{day.isoformat()}",
    ).delete()
    DigestRun.objects.filter(organization=org, for_date=day).delete()


# The digest log's status words for its delivery's status: queued and sending read pending (waiting to send or retry).
STATUS_OF: dict[str, str] = {
    DeliveryStatus.QUEUED: DigestStatus.PENDING,
    DeliveryStatus.SENDING: DigestStatus.PENDING,
    DeliveryStatus.SENT: DigestStatus.SENT,
    DeliveryStatus.FAILED: DigestStatus.FAILED,
    DeliveryStatus.SKIPPED: DigestStatus.SKIPPED,
}
DELIVERY_STATUSES = {status: [d for d, s in STATUS_OF.items() if s == status] for status in DigestStatus.values}


def status_word(delivery_status: str | None) -> str:
    """A digest's status in the log's words from its delivery's status; no delivery (only a deleted one leaves
    that) reads failed. The digest log, the Admin banner, the organization's email status and the renewal page's
    reminders all read it here."""
    return STATUS_OF.get(delivery_status, DigestStatus.FAILED) if delivery_status is not None else DigestStatus.FAILED


def status_of(digest) -> str:
    """A digest's status in the log, from its delivery."""
    return status_word(digest.delivery.status if digest.delivery is not None else None)


def retry(org, actor, digest):
    """An Admin's Retry now: send the digest at once. A queued one is attempted now; a failed or skipped one is
    sent again as a resend of its delivery (a fresh key, `resend_of`; the usual retries if this attempt can be
    retried), which becomes the digest's delivery. Sending happens outside any transaction (the claim commits first);
    the audit event records the outcome."""
    from catalog.services import audit
    from core.problems import Conflict

    from . import channels

    digest = Digest.objects.select_related("delivery", "user").get(id=digest.id, organization=org)
    current = digest.delivery
    if current is not None and current.status == DeliveryStatus.SENT:
        return digest
    if current is not None and current.status == DeliveryStatus.SENDING:
        raise Conflict("The digest is being sent now; wait for its outcome.")
    if not channels.email_configured(org):
        raise Conflict(
            "Email isn't configured, so the digest can't be sent. Choose a transport in Email settings or set "
            "SOFTWARDEN_SMTP_HOST on the server (see docs/ENV.md)."
        )
    with transaction.atomic():
        if current is not None and current.status == DeliveryStatus.QUEUED:
            Delivery.objects.filter(id=current.id, status=DeliveryStatus.QUEUED).update(next_attempt_at=timezone.now())
            target = current
        else:
            if current is None:
                raise Conflict("This digest's message is gone, so it can't be sent again.")
            target, _ = deliveries.enqueue(
                org,
                channel=Channel.EMAIL,
                kind=DeliveryKind.DIGEST,
                dedupe_key=f"resend:{current.id}:{uuid.uuid4().hex}",
                subject=current.subject,
                content=current.content,
                recipient=digest.user.email or current.recipient,
                rule=current.rule,
                rule_name=current.rule_name,
                requested_by=actor,
                resend_of=current,
                queue_job=False,
            )
            Digest.objects.filter(id=digest.id).update(delivery=target, updated_at=timezone.now())
    done = deliveries.attempt(target.id) or Delivery.objects.get(id=target.id)
    with transaction.atomic():
        audit(
            org,
            actor,
            "digest.retried",
            digest,
            {
                "user": digest.user_id,
                "for_date": digest.for_date.isoformat(),
                "sent": done.status == DeliveryStatus.SENT,
                "delivery": str(done.id),
            },
        )
    return Digest.objects.select_related("delivery", "user").get(id=digest.id)


def scheduled_build(now):
    """The `digest.build` schedule (notify/apps.py), every minute: each organization's digest run once its local
    digest time has come. One organization's failure doesn't stop the others."""
    from core.models import active_organizations

    runs = 0
    for org in active_organizations():  # a suspended organization is skipped
        try:
            if run_for_org(org, now) is not None:
                runs += 1
        except Exception:  # logged with the organization's id, never its data
            log.exception("digest run failed organization=%s", org.id)
    return {"runs": runs}


def digest_problem(org):
    """For the Admin banner: the latest run's failed deliveries, or None."""
    latest = Digest.objects.filter(organization=org).order_by("-for_date").values_list("for_date", flat=True).first()
    if latest is None:
        return None
    failed = Digest.objects.filter(organization=org, for_date=latest, delivery__status=DeliveryStatus.FAILED).count()
    return {"for_date": latest.isoformat(), "failed": failed} if failed else None
