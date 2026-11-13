"""Notifications. Moved from the catalog app in the split; tables and constraint names
keep their `catalog_*` names (notify/migrations/0001_initial.py)."""

import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.functions import Lower

from core.models import CATALOG_ORG_KEY, Membership, Organization, OrgScoped, Role
from money.models import Contract, LicensePool, RenewalCycle


class NotificationPreference(models.Model):
    """One membership's digest preference: a user in two organizations chooses for each. A membership with no row
    gets the digest (default on). Removing the membership removes its preference."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    # Composite FK (organization_id, membership_id) -> catalog_membership (notify 0002).
    membership = models.OneToOneField(Membership, on_delete=models.CASCADE, related_name="notification_preference")
    digest_enabled = models.BooleanField(default=True)
    # Reminder and overdue emails from notification rules (notify/rules.py). Teams and webhooks aren't
    # personal, so they ignore it.
    reminders_enabled = models.BooleanField(default=True)
    # The weekly emails of saved views this member is subscribed to (notify/subscriptions.py).
    subscriptions_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "catalog_notificationpreference"
        constraints = [CATALOG_ORG_KEY]


class DigestRun(OrgScoped):
    """One organization's daily digest run. The unique date makes a second worker, or a second tick, do nothing."""

    for_date = models.DateField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_digestrun"
        ordering = ["-for_date"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["organization", "for_date"], name="catalog_digestrun_unique_date"),
        ]


class DigestStatus(models.TextChoices):
    """The digest log's words for its delivery's status (notify/digest.py `status_of`): queued or sending read
    pending."""

    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Not sent: email isn't configured"


class Digest(OrgScoped):
    """One user's digest for one date: who it was for, how many items, and the Delivery that carries it.
    The message, its status, attempts and errors live on the delivery (kind `digest`, key
    `digest:{user_id}:{date}`, so it uses the organization's email settings and shows in the notification log); an
    Admin's Retry now points `delivery` at the resend. Unique per user and date; exactly-once delivery is not
    promised."""

    run = models.ForeignKey(DigestRun, on_delete=models.CASCADE, related_name="digests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    for_date = models.DateField()
    item_count = models.PositiveIntegerField(default=0)
    # Composite FK (organization_id, delivery_id) (notify 0006). SET_NULL, hidden reverse name.
    delivery = models.ForeignKey("Delivery", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta(OrgScoped.Meta):
        db_table = "catalog_digest"
        ordering = ["-for_date", "user_id"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["organization", "user", "for_date"], name="catalog_digest_unique_day"),
        ]


class ReminderDispatch(OrgScoped):
    """A reminder threshold that fired for a cycle, in the digest that carried it. Unique on (cycle, threshold),
    so one obligation covering three applications reminds once per threshold, never once per application."""

    cycle = models.ForeignKey(RenewalCycle, on_delete=models.CASCADE, related_name="reminders")
    threshold_days = models.PositiveIntegerField()
    digest = models.ForeignKey(Digest, null=True, blank=True, on_delete=models.SET_NULL, related_name="reminders")

    class Meta(OrgScoped.Meta):
        db_table = "catalog_reminderdispatch"
        ordering = ["-threshold_days"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["cycle", "threshold_days"], name="catalog_reminderdispatch_unique"),
        ]


class SnoozeKind(models.TextChoices):
    SNOOZE = "snooze", "Snooze"
    ACKNOWLEDGE = "acknowledge", "Acknowledge"


class NotificationSnooze(OrgScoped):
    """One user's snooze or acknowledgment of one Needs attention item. Notifications only: it never
    changes the item. A snooze ends after `until`; an acknowledgment holds while the item's reminder threshold is
    still `threshold` (−1 for an overdue action, which holds for that day only, through `until`)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    subject_type = models.CharField(max_length=30)
    subject_id = models.CharField(max_length=80)
    kind = models.CharField(max_length=12, choices=SnoozeKind.choices)
    until = models.DateField(null=True, blank=True)
    threshold = models.IntegerField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_notificationsnooze"
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(
                fields=["organization", "user", "subject_type", "subject_id"], name="catalog_snooze_unique"
            ),
        ]


# ---- reminders ---------------------------------------------------------------------------------------------


class ReminderSource(models.TextChoices):
    """Where a reminder's date comes from. The value is stable API vocabulary."""

    RENEWAL_CYCLE = "renewal_cycle", "Renewal action deadline"
    CONTRACT = "contract", "Contract cancellation deadline"
    PERIODIC_REVIEW = "periodic_review", "Periodic review"
    RECORD_REVIEW = "record_review", "Record review"
    CUSTOM_DATE = "custom_date", "Custom date"
    CREDENTIAL_EXPIRY = "credential_expiry", "Credential expiry"
    VENDOR_RISK = "vendor_risk", "Vendor risk document expiry"  # (catalog/vendor_risk.py)
    END_OF_LIFE = "end_of_life", "Version end of life"  # (eol/reminders.py, notify 0012)


class ReminderStatus(models.TextChoices):
    OPEN = "open", "Open"
    SNOOZED = "snoozed", "Snoozed"
    ACKNOWLEDGED = "acknowledged", "Acknowledged"
    DONE = "done", "Done"
    OBSOLETE = "obsolete", "No longer applies"


# A reminder that is still in play: notifications may go out for it (the rules package decides when) and Needs
# attention lists it. Done and obsolete reminders are closed.
LIVE_REMINDER_STATUSES = (ReminderStatus.OPEN, ReminderStatus.SNOOZED, ReminderStatus.ACKNOWLEDGED)


class ResolutionKind(models.TextChoices):
    """Why a reminder was closed. Acknowledging is not one of these: it stops notifications and is no evidence the
    work was done. `source_changed` is the generator's alone (status obsolete)."""

    DONE = "done", "Done"
    RISK_ACCEPTED = "risk_accepted", "Risk accepted"
    NO_LONGER_APPLIES = "no_longer_applies", "No longer applies"
    SOURCE_CHANGED = "source_changed", "Source changed"


class Reminder(OrgScoped):
    """A dated obligation computed from a source (notify/reminders.py), never entered by hand. One row per
    occurrence: (source type, source id, source field, due date) is unique, so regenerating is idempotent. When the
    source's date moves, an open or snoozed reminder moves with it; an acknowledged one is closed as obsolete and a new
    one opens for the new date; a done one stays done. A source that stops producing it (deleted, archived, completed,
    capability or profile off) closes it as obsolete, and it opens again (a new `notify_cycle`) if the source produces
    the same date again. Person actions (snooze, acknowledge, reopen, resolve) are audited (notify/services.py); the
    generator's own writes are derived state and are not.

    The source is `source_type` + `source_id` (always set, so a deleted source still names what it was) and, where a
    table exists, one nullable foreign key to it with a composite key: `cycle`, `contract`, `resource` (a
    record's review dates and custom dates; a credential's owning record) or `license_pool`. Vendors'
    custom dates and vendor risk documents (`source_id` the vendor) have no key column. They are SET_NULL so a reminder outlives its source as history; hidden reverse
    names, so they never block a delete."""

    source_type = models.CharField(max_length=30, choices=ReminderSource.choices)
    source_id = models.UUIDField()
    source_field = models.CharField(max_length=100, blank=True)  # a custom field's name; a credential's key ID
    due_date = models.DateField()
    deadline_label = models.CharField(max_length=30, blank=True)  # renewal_rules label: entered, calculated, ...
    title = models.CharField(max_length=300)  # the source's name at the last generation
    label = models.CharField(max_length=300)  # what is due: "Cancellation deadline", "Periodic review", a field label
    # Composite FKs (organization_id, x_id) (notify 0003).
    cycle = models.ForeignKey(RenewalCycle, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resource = models.ForeignKey("catalog.Resource", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    license_pool = models.ForeignKey(LicensePool, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=20, choices=ReminderStatus.choices, default=ReminderStatus.OPEN)
    snoozed_until = models.DateField(null=True, blank=True)  # quiet through this date (organization-local)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_kind = models.CharField(max_length=20, choices=ResolutionKind.choices, blank=True)
    resolution_note = models.TextField(blank=True)
    resolution_evidence = models.CharField(max_length=500, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    notify_cycle = models.PositiveIntegerField(default=0)  # bumped on each reopen; part of delivery dedupe keys

    class Meta(OrgScoped.Meta):
        ordering = ["due_date", "id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                fields=["organization", "source_type", "source_id", "source_field", "due_date"],
                name="notify_reminder_unique_occurrence",
            ),
            models.CheckConstraint(
                condition=models.Q(status=ReminderStatus.SNOOZED, snoozed_until__isnull=False)
                | (~models.Q(status=ReminderStatus.SNOOZED) & models.Q(snoozed_until__isnull=True)),
                name="notify_reminder_snooze_date",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_date"], name="notify_reminder_status_due"),
            models.Index(fields=["organization", "source_type", "source_id"], name="notify_reminder_source"),
        ]


# ---- channels and deliveries -------------------------------------------------------------------------------
# Secret slots (core/credentials.SLOTS): `x_ciphertext`, `x_key_id`, `x_set_at`. Never in a response, an ETag, an
# audit snapshot, a log line, an export or a job payload.


class EmailTransport(models.TextChoices):
    INSTALLATION_DEFAULT = "installation_default", "Installation default (the server's SMTP settings)"
    GRAPH = "graph", "Microsoft Graph (sendMail)"
    SMTP = "smtp", "SMTP server"


class SmtpSecurity(models.TextChoices):
    STARTTLS = "starttls", "STARTTLS"
    TLS = "tls", "TLS (implicit, usually port 465)"
    NONE = "none", "None (local relays and Mailpit only)"


class ChannelTest(models.Model):
    """The outcome of the last connection test or test send of a channel's settings (secret-free); cleared when the
    settings change."""

    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_detail = models.CharField(max_length=500, blank=True)

    class Meta:
        abstract = True


class EmailSettings(OrgScoped, ChannelTest):
    """How one organization sends email (notify/channels.py). No row, or `installation_default`, means the server's
    SOFTWARDEN_SMTP_* settings. Graph sends as the application from `sender_mailbox` (Mail.Send granted only through
    Exchange RBAC for Applications, docs/PERMISSIONS.md); SMTP is the fallback for any other server."""

    transport = models.CharField(
        max_length=30, choices=EmailTransport.choices, default=EmailTransport.INSTALLATION_DEFAULT
    )
    graph_tenant_id = models.CharField(max_length=253, blank=True)  # a GUID or the tenant's domain; `simulated`
    graph_client_id = models.CharField(max_length=36, blank=True)
    graph_secret_ciphertext = models.TextField(blank=True, default="")
    graph_secret_key_id = models.CharField(max_length=20, blank=True, default="")
    graph_secret_set_at = models.DateTimeField(null=True, blank=True)
    sender_mailbox = models.CharField(max_length=254, blank=True)
    smtp_host = models.CharField(max_length=253, blank=True)
    smtp_port = models.PositiveIntegerField(null=True, blank=True)
    smtp_security = models.CharField(max_length=10, choices=SmtpSecurity.choices, default=SmtpSecurity.STARTTLS)
    smtp_username = models.CharField(max_length=254, blank=True)
    smtp_password_ciphertext = models.TextField(blank=True, default="")
    smtp_password_key_id = models.CharField(max_length=20, blank=True, default="")
    smtp_password_set_at = models.DateTimeField(null=True, blank=True)
    from_address = models.CharField(max_length=254, blank=True)

    class Meta(OrgScoped.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization"], name="notify_emailsettings_one_per_org"),
        ]


class TeamsDestination(OrgScoped, ChannelTest):
    """A Teams channel's Workflows webhook ("Send webhook alerts to a channel"). The URL is a bearer secret: only its
    host is kept in clear (`url_host`), to tell destinations apart."""

    name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    url_ciphertext = models.TextField(blank=True, default="")
    url_key_id = models.CharField(max_length=20, blank=True, default="")
    url_set_at = models.DateTimeField(null=True, blank=True)
    url_host = models.CharField(max_length=253, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(Lower("name"), "organization", name="notify_teamsdestination_unique_name"),
        ]


class WebhookDestination(OrgScoped, ChannelTest):
    """A generic HTTPS webhook: the plan's Webhook (notify/webhooks.py). Notification rules name it as a
    destination, and it subscribes to `events` (record.*, relationship.*, renewal.*,
    discovery.candidate, reminder.*; a family wildcard such as `record.*` is stored as given). Bodies are signed with
    HMAC SHA-256 under `signing_secret` (notify/channels.py, header X-Softwarden-Signature); after a rotation the
    previous secret signs too until `previous_secret_expires_at`.

    The worker keeps the delivery summary (`last_delivery_*`, `consecutive_failures`) with plain updates: they are not
    settings, so they don't move the ETag (notify/channel_viewsets.DESTINATION_SKIP). `consecutive_failures` counts
    event deliveries that ended failed in a row; at SOFTWARDEN_WEBHOOK_DISABLE_AFTER, or on a 410 Gone, the endpoint
    is turned off with `disabled_at` and `disabled_reason` (audited `webhook_destination.disabled`)."""

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    enabled = models.BooleanField(default=True)
    url_ciphertext = models.TextField(blank=True, default="")
    url_key_id = models.CharField(max_length=20, blank=True, default="")
    url_set_at = models.DateTimeField(null=True, blank=True)
    url_host = models.CharField(max_length=253, blank=True)
    signing_secret_ciphertext = models.TextField(blank=True, default="")
    signing_secret_key_id = models.CharField(max_length=20, blank=True, default="")
    signing_secret_set_at = models.DateTimeField(null=True, blank=True)
    # Rotation keeps the replaced secret signing beside the new one for a grace period.
    previous_signing_secret_ciphertext = models.TextField(blank=True, default="")
    previous_signing_secret_key_id = models.CharField(max_length=20, blank=True, default="")
    previous_signing_secret_set_at = models.DateTimeField(null=True, blank=True)
    previous_secret_expires_at = models.DateTimeField(null=True, blank=True)
    events = ArrayField(models.CharField(max_length=60), default=list, blank=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=300, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_delivery_outcome = models.CharField(max_length=10, blank=True)  # sent, retrying, failed
    last_delivery_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_delivery_id = models.UUIDField(null=True, blank=True)  # a Delivery's id; not a key, the log outlives it

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(Lower("name"), "organization", name="notify_webhookdestination_unique_name"),
        ]


class Channel(models.TextChoices):
    EMAIL = "email", "Email"
    TEAMS = "teams", "Teams"
    WEBHOOK = "webhook", "Webhook"


class DeliveryKind(models.TextChoices):
    REMINDER = "reminder", "Reminder"
    ESCALATION = "escalation", "Overdue escalation"
    DIGEST = "digest", "Digest"
    SUBSCRIPTION = "subscription", "Saved view subscription"
    TEST = "test", "Test"
    EVENT = "event", "Webhook event"


class DeliveryStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENDING = "sending", "Sending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class Delivery(OrgScoped):
    """One message on one channel to one audience (notify/deliveries.py), and the notification log's row. The
    `dedupe_key` is unique per organization and is written before anything is sent, so enqueueing the same key twice,
    a job that runs again, or two workers never send it twice (Graph has no idempotency header). Keys name the audience
    by id or hash, never by address or URL. `content` is the rendered message (email text and HTML, a Teams card, a
    webhook's event data), saved when queued so a retry or a resend sends the same thing; it never holds a secret."""

    channel = models.CharField(max_length=10, choices=Channel.choices)
    kind = models.CharField(max_length=20, choices=DeliveryKind.choices, default=DeliveryKind.REMINDER)
    status = models.CharField(max_length=10, choices=DeliveryStatus.choices, default=DeliveryStatus.QUEUED)
    dedupe_key = models.CharField(max_length=200)
    recipient = models.CharField(max_length=254, blank=True)  # email only; shown to Admins only
    # Composite FKs (organization_id, x_id) (notify 0004). SET_NULL: the log outlives a deleted destination.
    teams_destination = models.ForeignKey(
        TeamsDestination, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    webhook_destination = models.ForeignKey(
        WebhookDestination, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    destination_name = models.CharField(max_length=100, blank=True)  # the destination's name when queued
    subject = models.CharField(max_length=300)
    content = models.JSONField(default=dict)
    reminder = models.ForeignKey(Reminder, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # The rule that sent it (notify 0005, composite); SET_NULL, so the log outlives a deleted rule.
    rule = models.ForeignKey("NotificationRule", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Snapshots, so the log stays readable after a rule is renamed or deleted.
    rule_name = models.CharField(max_length=200, blank=True)
    lead_time_days = models.IntegerField(null=True, blank=True)
    overdue_step = models.PositiveIntegerField(null=True, blank=True)
    # Rules (notify 0005): the reminder's due date the notice was for (notify/rules.Ledger: a stage covers a moved date
    # only while it moved less than the stage's lead), and whether the row is a baseline (recorded, never sent).
    due_date = models.DateField(null=True, blank=True)
    baseline = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=4)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claimed_until = models.DateTimeField(null=True, blank=True)
    client_request_id = models.UUIDField(null=True, blank=True)  # this attempt's id (Graph `client-request-id`)
    last_error = models.CharField(max_length=500, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    resend_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="resends")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Webhooks (notify 0009): the event type posted (`record.updated`, `reminder.due`, `ping`, ...) and, for a
    # subscribed event (kind `event`), the event's id, shared by every endpoint's delivery of it.
    event_type = models.CharField(max_length=60, blank=True)
    event_id = models.UUIDField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["-created_at", "-id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization", "dedupe_key"], name="notify_delivery_dedupe"),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="notify_delivery_due"),
            models.Index(fields=["organization", "created_at"], name="notify_delivery_log"),
            models.Index(fields=["organization", "reminder"], name="notify_delivery_reminder"),
            models.Index(fields=["organization", "webhook_destination", "created_at"], name="notify_delivery_webhook"),
        ]


class AttemptOutcome(models.TextChoices):
    SENT = "sent", "Sent"
    RETRYING = "retrying", "Failed, will retry"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class DeliveryAttempt(OrgScoped):
    """One attempt at a delivery (notify/deliveries.py): the webhook delivery log's rows, written for every
    channel. `status_code` and `response_excerpt` are the server's answer (HTTP channels only); the excerpt and `error`
    are scrubbed of the channel's secrets before they are cut to 500 characters, so a server that echoes its URL or the
    signature leaves no secret here. `next_attempt_at` is when the retry it scheduled is due. Removed with its
    delivery (composite key, notify 0009)."""

    delivery = models.ForeignKey(Delivery, on_delete=models.CASCADE, related_name="attempt_log")
    number = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=10, choices=AttemptOutcome.choices)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_excerpt = models.CharField(max_length=500, blank=True)
    error = models.CharField(max_length=500, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["started_at", "number"]
        indexes = [models.Index(fields=["organization", "delivery"], name="notify_deliveryattempt_of")]


WEBHOOK_EVENT_SEQUENCE = "notify_webhookevent_sequence"  # created by notify 0010


class WebhookEvent(OrgScoped):
    """The webhook outbox (notify/webhooks.py): one row per subscribed event, written in the same transaction
    as the write it describes, so a write that rolls back leaves none and a committed write always has its event. The
    worker's `webhooks.outbox` schedule fans the rows out into deliveries (one per subscribed, enabled endpoint) and
    deletes them in one transaction, oldest `sequence` first, so deliveries are queued in the order the events were
    recorded. `id` is the event's id (the envelope's `id`, shared by every endpoint's delivery of it); `content` is
    what a delivery's `content` will be, built from the audit event's already redacted payload: never a secret."""

    sequence = models.BigIntegerField(
        db_default=models.Func(models.Value(WEBHOOK_EVENT_SEQUENCE), function="nextval"),
        editable=False,
        unique=True,
    )
    event_type = models.CharField(max_length=60)
    content = models.JSONField(default=dict)

    class Meta(OrgScoped.Meta):
        ordering = ["sequence"]


# ---- notification rules ----------------------------------------------------------------------------------


class RuleKind(models.TextChoices):
    REMINDERS = "reminders", "Reminder notices"
    DIGEST = "digest", "Daily digest"


class NotificationRule(OrgScoped):
    """When and to whom live reminders are announced (notify/rules.py). A
    `reminders` rule covers `source_types`; it sends at each of `lead_times_days` before the due date and at each of
    `escalation_days` after it (and, with `repeat_every_days`, at every multiple of it after it), by email to its
    audience (the routed owner, people, a team, every member with a role; escalations also reach the escalation
    audience) and to its Teams and webhook destinations. When routing finds
    nobody, the fallback team or person is told, else the organization's IT coordinator. Filters narrow the reminders
    it covers. The `digest` rule is the daily digest (notify/digest.py): only its name, `enabled` and the time
    (Organization.digest_time) apply.

    Built-ins (`builtin_key` set) are seeded with every organization (notify/rule_services.BUILTINS; migration 0005
    for the organizations that existed before); they are editable and never deleted. Every reference has a composite
    key (notify 0005). Teams, people, vendors and destinations a rule names are PROTECT: deleting one that a rule uses
    is refused with the rule's name (people are never deleted)."""

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=RuleKind.choices, default=RuleKind.REMINDERS)
    builtin_key = models.CharField(max_length=40, blank=True)
    enabled = models.BooleanField(default=True)
    # When the stages its reminders were already inside were recorded as done (notify/rules.baseline): set when the
    # rule is created or turned on; null until the planner's first run for the built-ins migration 0005 seeded.
    baselined_at = models.DateTimeField(null=True, blank=True)
    source_types = ArrayField(models.CharField(max_length=30, choices=ReminderSource.choices), default=list, blank=True)
    lead_times_days = ArrayField(models.PositiveIntegerField(), default=list, blank=True)  # largest first
    escalation_days = ArrayField(models.PositiveIntegerField(), default=list, blank=True)  # smallest first
    # Repeat every this many days after the due date while the reminder is still live (null: no repeat; notify 0013).
    # Each repeat is an escalation step at a multiple of it (30, 60, 90, ...), so it follows the same rules.
    repeat_every_days = models.PositiveIntegerField(null=True, blank=True)
    escalate_when_acknowledged = models.BooleanField(default=True)
    # Channels
    send_email = models.BooleanField(default=True)
    teams_destinations = models.ManyToManyField(
        TeamsDestination, through="NotificationRuleTeamsDestination", related_name="+", blank=True
    )
    webhook_destinations = models.ManyToManyField(
        WebhookDestination, through="NotificationRuleWebhookDestination", related_name="+", blank=True
    )
    # Audience
    notify_routed = models.BooleanField(default=True)
    people = models.ManyToManyField("catalog.Person", through="NotificationRulePerson", related_name="+", blank=True)
    team = models.ForeignKey("catalog.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    roles = ArrayField(models.CharField(max_length=10, choices=Role.choices), default=list, blank=True)
    escalation_people = models.ManyToManyField(
        "catalog.Person", through="NotificationRuleEscalationPerson", related_name="+", blank=True
    )
    escalation_team = models.ForeignKey(
        "catalog.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    escalation_roles = ArrayField(models.CharField(max_length=10, choices=Role.choices), default=list, blank=True)
    fallback_team = models.ForeignKey("catalog.Team", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    fallback_person = models.ForeignKey(
        "catalog.Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Filters: each one set must match; a reminder a filter can't judge doesn't match it.
    criticalities = ArrayField(models.CharField(max_length=20), default=list, blank=True)
    record_kinds = ArrayField(models.CharField(max_length=20), default=list, blank=True)
    vendor = models.ForeignKey("catalog.Vendor", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    min_annual_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(Lower("name"), "organization", name="notify_notificationrule_unique_name"),
            models.UniqueConstraint(
                fields=["organization", "builtin_key"],
                condition=~models.Q(builtin_key=""),
                name="notify_notificationrule_unique_builtin",
            ),
        ]

    @property
    def is_builtin(self) -> bool:
        return bool(self.builtin_key)


class NotificationRulePerson(OrgScoped):
    """A person a rule notifies (composite keys to the rule and the person)."""

    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name="+")
    person = models.ForeignKey("catalog.Person", on_delete=models.PROTECT, related_name="+")

    class Meta(OrgScoped.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["rule", "person"], name="notify_notificationruleperson_unique"),
        ]


class NotificationRuleEscalationPerson(OrgScoped):
    """A person a rule's overdue notices also reach."""

    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name="+")
    person = models.ForeignKey("catalog.Person", on_delete=models.PROTECT, related_name="+")

    class Meta(OrgScoped.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["rule", "person"], name="notify_notificationruleescperson_unique"),
        ]


class NotificationRuleTeamsDestination(OrgScoped):
    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name="+")
    destination = models.ForeignKey(TeamsDestination, on_delete=models.PROTECT, related_name="+")

    class Meta(OrgScoped.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["rule", "destination"], name="notify_notificationruleteams_unique"),
        ]


class NotificationRuleWebhookDestination(OrgScoped):
    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name="+")
    destination = models.ForeignKey(WebhookDestination, on_delete=models.PROTECT, related_name="+")

    class Meta(OrgScoped.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["rule", "destination"], name="notify_notificationrulewebhook_unique"),
        ]


# ---- saved view subscriptions ------------------------------------------------------------------------------


class SavedViewSubscription(OrgScoped):
    """A weekly email of a saved view's rows to one member (notify/subscriptions.py).
    Sent on `weekday` (0 Monday to 6 Sunday) of the organization's local date, at the notification send time, as a
    Delivery of kind `subscription` keyed per subscription and local date. The rows are the list's own, read with the
    member's current role and visibility at send time. A member subscribes themselves; an Admin can subscribe another
    member, to a view that member can see.

    `saved_view` is SET_NULL, so a deleted view leaves the subscription behind and each send day records a skipped
    delivery saying so; removing the membership removes its subscriptions. `last_sent` is the local date of the last
    weekly email queued. Composite keys to the view and the membership (notify 0008)."""

    saved_view = models.ForeignKey(
        "catalog.SavedFilter", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="+")
    weekday = models.PositiveSmallIntegerField()
    active = models.BooleanField(default=True)
    last_sent = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["weekday", "created_at", "id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["saved_view", "membership"], name="notify_savedviewsubscription_unique"),
            models.CheckConstraint(condition=models.Q(weekday__lte=6), name="notify_savedviewsubscription_weekday"),
        ]
        indexes = [
            models.Index(fields=["organization", "weekday", "active"], name="notify_savedviewsub_day"),
        ]
