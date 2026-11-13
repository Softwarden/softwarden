"""Serializers for Needs attention, your digest preference, snoozes and the digest delivery log.
Snoozes and digests render model rows; Needs attention and the preference are built as plain data
(notify/attention.py, notify/viewsets.py) and described here; tests/test_drf_resources.py checks each description has
exactly the keys the endpoint returns."""

from rest_framework import serializers

from catalog.models import RECORD_KIND_CHOICES, DataClassification, VendorRiskTier
from catalog.serializers import RecordBriefSerializer
from core.drf import BriefSerializer, ReadOnlySerializer, UserBriefSerializer, iso, represent
from money.models import Decision
from money.serializers import TimingSerializer

from .attention import FOLDED_REMINDERS, QUEUES
from .models import LIVE_REMINDER_STATUSES, DigestStatus, ReminderSource, ReminderStatus, ResolutionKind, SnoozeKind

# ---- Needs attention ------------------------------------------------------------------------------------------------


class StewardSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    name = serializers.CharField(help_text="'Unassigned' when no steward or owner and no IT coordinator is set.")
    email = serializers.CharField(required=False)
    coordinator = serializers.BooleanField(required=False, help_text="True when the IT coordinator catches the item.")


class AttentionSnoozeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(SnoozeKind.choices)
    until = serializers.DateField(allow_null=True)


class AttentionReminderBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The reminder (POST /reminders/{id}/snooze, /acknowledge, /reopen).")
    status = serializers.ChoiceField(
        [s for s in ReminderStatus.values if s in LIVE_REMINDER_STATUSES], help_text="As of today."
    )
    snoozed_until = serializers.DateField(allow_null=True)
    due = serializers.DateField()
    due_text = serializers.CharField(
        required=False,
        help_text='End of life rows only: what `due` is, "End of life on DATE", or for a cycle flagged end of life '
        'with no date "End of life (no date published), tracked since DATE" (the day tracking began).',
    )


class AttentionRecordRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    name = serializers.CharField()
    why = serializers.CharField()
    steward = StewardSerializer()
    criticality = serializers.CharField(allow_null=True, required=False, help_text="Ownership and AI agent gaps only.")
    gaps = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Ownership and AI agent gaps only."
    )
    state = serializers.CharField(required=False, help_text="Reviews only.")
    due = serializers.DateField(allow_null=True, required=False, help_text="Reviews only.")
    reminder = AttentionReminderBriefSerializer(
        allow_null=True,
        required=False,
        help_text="Reviews only: the record review reminder for this record's next review date, if live.",
    )
    snooze = AttentionSnoozeSerializer(allow_null=True, help_text="Your snooze, while it holds.")


class AttentionLinkEndSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    name = serializers.CharField()
    instance = serializers.CharField(allow_null=True)


class AttentionLinkRecordSerializer(BriefSerializer):
    kind = serializers.ChoiceField([*RECORD_KIND_CHOICES, "integration"])


class AttentionLinkRowSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The relationship's ID, or the integration record's ID.")
    link_kind = serializers.ChoiceField(["relationship", "integration"])
    type = serializers.CharField(help_text="The relationship type's slug, or 'integration'.")
    type_name = serializers.CharField(help_text="The relationship type's name (\"Depends on\"), or 'Integration'.")
    record = AttentionLinkRecordSerializer(
        help_text="The record the link belongs to: the source of a relationship, or the integration."
    )
    source = AttentionLinkEndSerializer()  # type: ignore[assignment]  # the field, not Field.source
    target = AttentionLinkEndSerializer()
    why = serializers.CharField()
    last_confirmed_on = serializers.DateField()
    steward = StewardSerializer(help_text="The source record's IT steward, or the integration's own.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionAssessmentTargetSerializer(BriefSerializer):
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)


class AttentionAssessmentRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField()
    status = serializers.CharField()
    change_type = serializers.CharField()
    target = AttentionAssessmentTargetSerializer()
    target_date = serializers.DateField(allow_null=True)
    stale_since = serializers.DateField()
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The assessment owner; the coordinator when it has none.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionRenewalRowSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The renewal's ID.")
    kind = serializers.ChoiceField(["renewal_action", "renewal_gap"])
    name = serializers.CharField()
    vendor = serializers.CharField(allow_null=True)
    cycle = serializers.UUIDField(help_text="The latest cycle's ID.")
    deadline = serializers.DateField(allow_null=True)
    label = serializers.ChoiceField(["entered", "calculated", "assumed", "renewal_action"], allow_null=True)  # type: ignore[assignment]  # the API field is called label
    label_text = serializers.CharField(allow_null=True)
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The IT follow-up assignee; the coordinator when there is none.")
    snooze = AttentionSnoozeSerializer(allow_null=True, help_text="Your snooze or acknowledgment, while it holds.")


class AttentionRenewalActionSerializer(AttentionRenewalRowSerializer):
    timing = TimingSerializer()
    decision = serializers.ChoiceField(Decision.choices, allow_null=True)
    reminder = AttentionReminderBriefSerializer(
        allow_null=True,
        help_text="The reminder for this cycle's action deadline, if live: one row per deadline.",
    )


class AttentionRenewalGapSerializer(AttentionRenewalRowSerializer):
    gaps = serializers.ListField(child=serializers.CharField())


class AttentionCountsSerializer(serializers.Serializer):
    mine = serializers.IntegerField()
    all = serializers.IntegerField()
    unassigned = serializers.IntegerField()


class AttentionDiscoverySerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The connector.")
    name = serializers.CharField()
    new = serializers.IntegerField()
    changed = serializers.IntegerField(help_text="Dismissed candidates the source has changed since.")
    why = serializers.CharField()
    kind = serializers.ChoiceField(["discovery"])
    steward = StewardSerializer(help_text="The connector's reviewer, else the coordinator.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionMissingSourceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    source = serializers.CharField(help_text="entra, azure, powerplatform, halo, ...")  # type: ignore[assignment]


class AttentionMissingObservationSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The observation (GET /discovery/observations/{id}).")
    name = serializers.CharField(help_text="The name the source last gave it.")
    external_id = serializers.CharField()


class AttentionDiscoveryMissingSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The observation; what a snooze names.")
    kind = serializers.ChoiceField(["discovery_missing"])
    name = serializers.CharField(help_text="The record's name.")
    record = RecordBriefSerializer()
    observation = AttentionMissingObservationSerializer()
    connector = AttentionMissingSourceSerializer()
    since = serializers.DateField(help_text="When a run first no longer saw it (organization date).")
    why = serializers.CharField()
    steward = StewardSerializer(
        help_text="The record's IT steward, else the connector's reviewer, else the coordinator."
    )
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionDataQualitySerializer(serializers.Serializer):
    id = serializers.CharField(help_text="<rule id>:<IT steward's user ID, or none>; what a snooze names.")
    kind = serializers.ChoiceField(["data_quality"])
    rule = serializers.CharField(help_text="The data-quality rule (GET /data-quality/rules).")
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    severity = serializers.ChoiceField(["high", "medium", "low"])
    count = serializers.IntegerField(help_text="Live active or deprecated records of this steward failing the rule.")
    it_steward = serializers.IntegerField(
        allow_null=True, help_text="The records' IT steward (GET /data-quality/failures?it_steward=), or null."
    )
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The records' IT steward, else the coordinator.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionSeatCountSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The license pool; what a snooze names.")
    kind = serializers.ChoiceField(["seat_count"])
    name = serializers.CharField(help_text="The pool's SKU name.")
    application = BriefSerializer()
    proposals = serializers.IntegerField(help_text="Sources whose counts differ (GET /license-pools/seat-review).")
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The application's IT steward, else the coordinator.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionReminderSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The reminder; act on it at /reminders/{id}.")
    kind = serializers.ChoiceField(["reminder"])
    source_type = serializers.ChoiceField(
        [s for s in ReminderSource.values if s not in FOLDED_REMINDERS],
        help_text="Renewal cycle, record review and end-of-life reminders are folded into their Renewal actions, "
        "Reviews and End of life rows.",
    )
    source_id = serializers.UUIDField()
    name = serializers.CharField(help_text="The source's name.")
    label = serializers.CharField(
        help_text="What is due: 'Cancellation deadline', 'Periodic review due', a field label."
    )  # type: ignore[assignment]  # the API field
    due = serializers.DateField()
    timing = TimingSerializer()
    status = serializers.ChoiceField([s for s in ReminderStatus.values if s in LIVE_REMINDER_STATUSES])
    snoozed_until = serializers.DateField(allow_null=True)
    path = serializers.CharField(allow_null=True, help_text="The SPA page of the source.")
    why = serializers.CharField()
    steward = StewardSerializer(
        help_text="Routed like the source (IT steward, IT follow-up, renewal owner), else the coordinator."
    )
    snooze = AttentionSnoozeSerializer(allow_null=True, help_text="Always null: a reminder is snoozed on the reminder.")


class VendorRiskGapSerializer(serializers.Serializer):
    code = serializers.ChoiceField(["no_current_assurance", "dpa_missing"])
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label


class AttentionVendorRiskSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The vendor (GET /vendors/{id}); what a snooze names.")
    kind = serializers.ChoiceField(["vendor_risk"])
    name = serializers.CharField(help_text="The vendor's name.")
    risk_tier = serializers.ChoiceField(VendorRiskTier.choices, allow_null=True)
    gaps = VendorRiskGapSerializer(many=True)
    why = serializers.CharField()
    steward = StewardSerializer(help_text="A vendor has no IT steward: the coordinator.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionEndOfLifeRecordSerializer(BriefSerializer):
    kind = serializers.ChoiceField(["application", "component"])


class AttentionEndOfLifeSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The tracked version (GET /end-of-life); what a snooze names.")
    kind = serializers.ChoiceField(["end_of_life"])
    record = AttentionEndOfLifeRecordSerializer()
    instance = BriefSerializer(allow_null=True, help_text="The instance with a version of its own, or null.")
    product = serializers.CharField(help_text="The endoflife.date product name.")
    product_label = serializers.CharField()
    cycle = serializers.CharField(help_text="The release cycle, such as 16.")
    status = serializers.ChoiceField(
        ["supported", "security_only", "end_of_life"],
        help_text="end_of_life, or not yet: its end-of-life date is within 180 days.",
    )
    eol_date = serializers.DateField(allow_null=True)
    days_remaining = serializers.IntegerField(allow_null=True, help_text="Negative once past.")
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The record's IT steward, else the coordinator.")
    reminder = AttentionReminderBriefSerializer(
        allow_null=True,
        help_text="The end-of-life reminder for this version's date, if live (notify 0012; it is emailed by the "
        "built-in End of life rule, to the IT steward, else the technical owner). Null with no end-of-life date.",
    )
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionDataFlowEndSerializer(BriefSerializer):
    kind = serializers.ChoiceField(["application", "component"])


class AttentionDataFlowRiskSerializer(serializers.Serializer):
    code = serializers.ChoiceField(["no_dpa", "no_auth_method", "down_classification"])
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label


class AttentionDataFlowSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The integration (GET /integrations/{id}); what a snooze names.")
    kind = serializers.ChoiceField(["data_flow"])
    name = serializers.CharField(help_text="The integration's name.")
    classification = serializers.ChoiceField(DataClassification.choices, allow_null=True)
    source = AttentionDataFlowEndSerializer()  # type: ignore[assignment]  # the field, not Field.source
    target = AttentionDataFlowEndSerializer()
    risks = AttentionDataFlowRiskSerializer(many=True)
    why = serializers.CharField()
    steward = StewardSerializer(help_text="The integration's IT steward, else the coordinator.")
    snooze = AttentionSnoozeSerializer(allow_null=True)


class AttentionSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(["mine", "all", "unassigned"])
    records = serializers.IntegerField(help_text="Active catalog records; 0 means a new install.")
    counts = AttentionCountsSerializer(help_text="Items in every queue, for each scope.")
    ownership_gaps = AttentionRecordRowSerializer(many=True)
    agent_gaps = AttentionRecordRowSerializer(
        many=True,
        help_text="Live AI agents with no owner, no data access scope or no human-in-loop decision.",
    )
    reviews = AttentionRecordRowSerializer(many=True)
    links_to_verify = AttentionLinkRowSerializer(many=True)
    stale_assessments = AttentionAssessmentRowSerializer(many=True)
    renewal_actions = AttentionRenewalActionSerializer(
        many=True, help_text="Action deadline within the largest reminder threshold, or overdue."
    )
    renewal_gaps = AttentionRenewalGapSerializer(many=True)
    discovery_candidates = AttentionDiscoverySerializer(many=True, help_text="One row per connector.")
    discovery_missing = AttentionDiscoveryMissingSerializer(
        many=True,
        help_text="Linked records whose source no longer lists them: keep (POST "
        "/discovery/observations/{id}/missing/acknowledge) or archive (.../missing/archive). Not counted in `counts` "
        "and not a digest section until the page shows it.",
    )
    data_quality = AttentionDataQualitySerializer(
        many=True,
        help_text="One row per data-quality rule and IT steward with failing records; rules that are "
        "ownership, AI agent or review gaps are in those queues instead.",
    )
    seat_counts = AttentionSeatCountSerializer(
        many=True,
        help_text="One row per license pool whose seat counts a source disagrees with; counted and in the digest.",
    )
    reminders = AttentionReminderSerializer(
        many=True,
        help_text="Live reminders (contract deadlines, periodic reviews, custom dates, credential expiry) due within the "
        "largest reminder threshold or overdue. Not counted in `counts` or the digest until the page shows it.",
    )
    vendor_risk = AttentionVendorRiskSerializer(
        many=True,
        help_text="Vendors whose risk file has a gap: high or critical risk with no current SOC 2 or "
        "equivalent, or personal data with no signed DPA. Counted and a digest section since the vendor risk file's "
        "SPA package.",
    )
    end_of_life = AttentionEndOfLifeSerializer(
        many=True,
        help_text="Tracked versions of live records that are end of life or reach it within 180 days (capability "
        "eol), soonest first. Not a digest section: the "
        "built-in End of life notification rule emails each version's reminder instead (notify 0012).",
    )
    data_flows = AttentionDataFlowSerializer(
        many=True,
        help_text="Live data flows with a risk (capability data_flows; GET /reports/data-flows): "
        "confidential data to a vendor with no DPA, no authentication to an external vendor, or classified above the "
        "receiving record. The most sensitive first. Counted and a digest section since the data-flow map's SPA "
        "package.",
    )


# ---- preferences, snoozes, digests ------------------------------------------------------------------------------


class PreferenceSerializer(serializers.Serializer):
    digest_enabled = serializers.BooleanField()
    reminders_enabled = serializers.BooleanField(
        help_text="Reminder and overdue emails from notification rules (Teams and webhooks aren't personal)."
    )
    subscriptions_enabled = serializers.BooleanField(
        help_text="The weekly emails of saved views you are subscribed to (saved-view-subscriptions)."
    )
    digest_time = serializers.CharField(help_text="HH:MM in the organization's timezone.")
    timezone = serializers.CharField()
    next_digest_at = serializers.DateTimeField(help_text="When the next digest run starts.")


class PreferenceInputSerializer(serializers.Serializer):
    digest_enabled = serializers.BooleanField(required=False)
    reminders_enabled = serializers.BooleanField(required=False)
    subscriptions_enabled = serializers.BooleanField(required=False)


class SnoozeSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    subject_type = serializers.CharField(
        read_only=True, help_text="The Needs attention queue, such as renewal_actions."
    )
    subject_id = serializers.CharField(read_only=True, help_text="The row's id; for links_to_verify, link_kind:id.")
    kind = serializers.ChoiceField(SnoozeKind.choices, read_only=True)
    until = serializers.DateField(read_only=True, allow_null=True)
    threshold = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="Acknowledge: the reminder threshold it holds for; -1 overdue."
    )


class SnoozeInputSerializer(serializers.Serializer):
    subject_type = serializers.ChoiceField(list(QUEUES))
    subject_id = serializers.CharField()
    kind = serializers.ChoiceField(SnoozeKind.choices, help_text="Acknowledge is for renewal actions only.")
    until = serializers.DateField(required=False, help_text="Snooze: a date after today, at most 90 days ahead.")


class DigestSerializer(ReadOnlySerializer):
    """One user's digest for one date, read from its delivery (digests are deliveries, kind digest)."""

    id = serializers.UUIDField(read_only=True)
    for_date = serializers.DateField(read_only=True)
    user = UserBriefSerializer(read_only=True)
    status = serializers.ChoiceField(
        DigestStatus.choices, read_only=True, help_text="From the delivery: queued or sending read pending."
    )
    item_count = serializers.IntegerField(read_only=True)
    attempts = serializers.IntegerField(read_only=True)
    last_error = serializers.CharField(read_only=True, help_text="Never contains a secret; empty once sent.")
    next_attempt_at = serializers.DateTimeField(read_only=True, allow_null=True, help_text="While pending.")
    sent_at = serializers.DateTimeField(read_only=True, allow_null=True)
    delivery = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="The delivery in the notification log (the latest resend)."
    )

    def to_representation(self, d):
        from .digest import status_of

        delivery = d.delivery
        status = status_of(d)
        return {
            "id": str(d.id),
            "for_date": d.for_date.isoformat(),
            "user": represent(UserBriefSerializer, d.user),
            "status": status,
            "item_count": d.item_count,
            "attempts": delivery.attempts if delivery else 0,
            "last_error": delivery.last_error if delivery else "",
            "next_attempt_at": iso(delivery.next_attempt_at) if delivery and status == DigestStatus.PENDING else None,
            "sent_at": iso(delivery.sent_at) if delivery else None,
            "delivery": str(delivery.id) if delivery else None,
        }


# ---- reminders ----------------------------------------------------------------------------------------------


class ReminderSerializer(ReadOnlySerializer):
    """A reminder as of today in the organization's timezone. Rows are annotated by the viewset (`effective`, `routed`,
    `resource_kind`, `obligation_id`); the page context carries `today`, the routed `users`, the organization's
    `coordinator` and the `vendors` that still exist. Built as plain data per row (a list reads hundreds)."""

    id = serializers.UUIDField(read_only=True)
    source_type = serializers.ChoiceField(ReminderSource.choices, read_only=True)
    source_id = serializers.UUIDField(
        read_only=True, help_text="The renewal cycle, contract, record, vendor, license pool or credential."
    )
    source_field = serializers.CharField(
        read_only=True, help_text="A custom date's field name; a credential's key ID; else empty."
    )
    title = serializers.CharField(read_only=True, help_text="The source's name when the reminder was last generated.")
    label = serializers.CharField(
        read_only=True,
        help_text="What is due: 'Cancellation deadline', 'Renewal action due', 'Periodic review due', 'Record review due', a custom field's label.",
    )  # type: ignore[assignment]  # the API field
    deadline_label = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Renewals and contracts: entered, calculated, assumed or renewal_action.",
    )
    deadline_label_text = serializers.CharField(read_only=True, allow_null=True)
    due_date = serializers.DateField(read_only=True)
    timing = TimingSerializer(read_only=True)
    status = serializers.ChoiceField(
        ReminderStatus.choices, read_only=True, help_text="As of today: a snooze whose date has passed reads open."
    )
    snoozed_until = serializers.DateField(
        read_only=True, allow_null=True, help_text="Quiet through this date; null unless snoozed."
    )
    path = serializers.CharField(
        read_only=True, allow_null=True, help_text="The SPA page of the source; null once it is gone."
    )
    steward = StewardSerializer(
        read_only=True, help_text="Routed like the source in Needs attention, else the coordinator."
    )
    unassigned = serializers.BooleanField(
        read_only=True, help_text="No steward or owner of its own (scope=unassigned)."
    )
    acknowledged_at = serializers.DateTimeField(read_only=True, allow_null=True)
    acknowledged_by = UserBriefSerializer(read_only=True, allow_null=True)
    resolved_at = serializers.DateTimeField(read_only=True, allow_null=True, help_text="Done or obsolete since.")
    resolved_by = UserBriefSerializer(read_only=True, allow_null=True)
    resolution_kind = serializers.ChoiceField(
        ResolutionKind.choices,
        read_only=True,
        allow_null=True,
        help_text="source_changed when the generator closed it (status obsolete).",
    )
    resolution_note = serializers.CharField(read_only=True)
    resolution_evidence = serializers.CharField(read_only=True)
    notify_cycle = serializers.IntegerField(
        read_only=True, help_text="Bumped each time it is reopened after being closed."
    )
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def to_representation(self, instance):
        from money import renewal_rules

        from .reminders import path_of

        context = self.context
        today = context["today"]
        status = instance.effective
        routed = context["users"].get(instance.routed)
        if routed is not None:
            steward = represent(UserBriefSerializer, routed)
        elif context["coordinator"] is not None:
            steward = dict(represent(UserBriefSerializer, context["coordinator"]), coordinator=True)
        else:
            steward = {"name": "Unassigned"}
        return {
            "id": str(instance.id),
            "source_type": instance.source_type,
            "source_id": str(instance.source_id),
            "source_field": instance.source_field,
            "title": instance.title,
            "label": instance.label,
            "deadline_label": instance.deadline_label or None,
            "deadline_label_text": renewal_rules.LABELS.get(instance.deadline_label),
            "due_date": instance.due_date.isoformat(),
            "timing": renewal_rules.timing(instance.due_date, today),
            "status": status,
            "snoozed_until": iso(instance.snoozed_until) if status == ReminderStatus.SNOOZED else None,
            "path": path_of(
                instance.source_type,
                instance.source_id,
                resource_id=instance.resource_id,
                resource_kind=instance.resource_kind,
                obligation_id=instance.obligation_id,
                contract_id=instance.contract_id,
                license_pool_id=instance.license_pool_id,
                vendor_exists=instance.source_id in context["vendors"],
            ),
            "steward": steward,
            "unassigned": routed is None,
            "acknowledged_at": iso(instance.acknowledged_at),
            "acknowledged_by": represent(UserBriefSerializer, instance.acknowledged_by),
            "resolved_at": iso(instance.resolved_at),
            "resolved_by": represent(UserBriefSerializer, instance.resolved_by),
            "resolution_kind": instance.resolution_kind or None,
            "resolution_note": instance.resolution_note,
            "resolution_evidence": instance.resolution_evidence,
            "notify_cycle": instance.notify_cycle,
            "created_at": iso(instance.created_at),
            "updated_at": iso(instance.updated_at),
        }


class ReminderSnoozeInputSerializer(serializers.Serializer):
    until = serializers.DateField(
        help_text="Quiet through this date: after today, at most 90 days ahead (organization-local)."
    )


class ReminderResolveInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        [ResolutionKind.DONE, ResolutionKind.RISK_ACCEPTED, ResolutionKind.NO_LONGER_APPLIES]
    )
    note = serializers.CharField(
        required=False,
        help_text="Why; required for risk_accepted and for renewal, contract, periodic review and credential reminders.",
    )
    evidence = serializers.CharField(required=False, help_text="A reference: a ticket, a document (500 characters).")
