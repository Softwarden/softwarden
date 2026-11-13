"""OpenAPI descriptions of the reports, dashboard and calendar feed responses. The bodies are built as plain
data by money/reporting.py and money/report_views.py; tests/test_reports.py checks each description has exactly the
keys the endpoint returns. Money is a decimal string, never converted between currencies."""

from rest_framework import serializers

from catalog.serializers import QualitySummarySerializer
from core.drf import ReadOnlySerializer, UserBriefSerializer
from notify.serializers import AttentionCountsSerializer

from .contract_serializers import PoolTotalsRowSerializer, PoolTotalsSumSerializer
from .serializers import TimingSerializer


class NamedRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class RecordRefSerializer(NamedRefSerializer):
    kind = serializers.CharField()


class CommitmentSerializer(ReadOnlySerializer):
    currency = serializers.CharField()
    annualized = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="run_rate + one_time: the total every spend dimension adds up to."
    )
    run_rate = serializers.DecimalField(max_digits=16, decimal_places=2, help_text="Recurring cost of live terms.")
    one_time = serializers.DecimalField(max_digits=16, decimal_places=2, help_text="One-time charges on live terms.")
    future = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="Recurring annualized cost of Signed terms not started yet."
    )
    future_one_time = serializers.DecimalField(max_digits=16, decimal_places=2)
    next_start = serializers.DateField(allow_null=True, help_text="The earliest start of a Signed term.")
    contracts = serializers.IntegerField(help_text="Live contracts (Active, Pending renewal) in this currency.")


GAP_HELP = (
    "Null for a real group; else why the cost has no group: no_vendor, unallocated (not on a record), no_department, "
    "no_category, not_application (components and AI agents have no category), no_cost_center, other (the dashboard's "
    "folded remainder)."
)


class SpendGroupSerializer(ReadOnlySerializer):
    key = serializers.CharField(
        allow_null=True, help_text="Vendor, department or category id, or the cost center; null for a gap."
    )
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    gap = serializers.CharField(allow_null=True, help_text=GAP_HELP)
    currency = serializers.CharField()
    amount = serializers.DecimalField(max_digits=16, decimal_places=2)
    contracts = serializers.IntegerField(allow_null=True)
    records = serializers.IntegerField(allow_null=True)


class DashboardSpendGroupSerializer(SpendGroupSerializer):
    groups = serializers.IntegerField(required=False, help_text="With gap other: how many groups it folds.")


class AllocationRowSerializer(ReadOnlySerializer):
    key = serializers.CharField(allow_null=True)
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    gap = serializers.CharField(allow_null=True, help_text=GAP_HELP)
    currency = serializers.CharField()
    amount = serializers.DecimalField(max_digits=16, decimal_places=2)
    percent = serializers.DecimalField(max_digits=6, decimal_places=2, help_text="Share of the contract's cost.")
    contract = NamedRefSerializer()
    vendor = NamedRefSerializer(allow_null=True)
    record = RecordRefSerializer(allow_null=True, help_text="Null: cost not on a record.")
    cost_center = serializers.CharField(help_text="The record's cost center, else the contract's; may be empty.")


class SpendReportSerializer(ReadOnlySerializer):
    by = serializers.ChoiceField(choices=["vendor", "department", "category", "cost_center"])
    totals = CommitmentSerializer(many=True)
    rows = SpendGroupSerializer(many=True)
    allocations = AllocationRowSerializer(many=True)


class DeadlineRowSerializer(ReadOnlySerializer):
    uid = serializers.CharField(help_text="Stable per deadline: the ICS event's UID.")
    source = serializers.CharField(help_text="renewal or contract.")  # type: ignore[assignment]  # the API field
    id = serializers.UUIDField(help_text="The renewal's or the contract's id.")
    cycle = serializers.UUIDField(
        allow_null=True,
        help_text="A renewal row's open cycle (the deadline's source; reminders name it); null for contracts.",
    )
    name = serializers.CharField()
    vendor = serializers.CharField(allow_null=True)
    date = serializers.DateField()
    action = serializers.ChoiceField(
        choices=["cancel_by", "renew_by"], help_text="cancel_by unless it doesn't auto-renew."
    )
    label = serializers.CharField(allow_null=True)  # type: ignore[assignment]  # the API field, not Field.label
    label_text = serializers.CharField(allow_null=True)
    explanation = serializers.CharField(
        allow_blank=True, help_text="Empty (and label null) when a contract's stored deadline isn't explained."
    )
    term_ends = serializers.DateField(allow_null=True, help_text="The renewal date or the contract's end date.")
    auto_renew = serializers.CharField()
    decision = serializers.CharField(allow_null=True)
    timing = TimingSerializer()
    contract = NamedRefSerializer(allow_null=True, help_text="The contract this deadline comes from, if any.")
    annualized_cost = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    currency = serializers.CharField(allow_null=True)
    owner = UserBriefSerializer(allow_null=True, help_text="The renewal's IT follow-up.")
    path = serializers.CharField(help_text="The SPA page of the renewal or contract.")


class MonthCountSerializer(ReadOnlySerializer):
    month = serializers.CharField(help_text="YYYY-MM")
    count = serializers.IntegerField()


class CalendarReportSerializer(ReadOnlySerializer):
    months = serializers.IntegerField()
    from_ = serializers.DateField(help_text="The first day of this month.")
    to = serializers.DateField(help_text="The last day of the window.")
    overdue = serializers.IntegerField()
    by_month = MonthCountSerializer(many=True)
    rows = DeadlineRowSerializer(many=True)

    def get_fields(self):
        fields = super().get_fields()
        fields["from"] = fields.pop("from_")
        return fields


class WindowSerializer(ReadOnlySerializer):
    days = serializers.IntegerField()
    count = serializers.IntegerField()


class DashboardDeadlinesSerializer(ReadOnlySerializer):
    overdue = serializers.IntegerField()
    windows = WindowSerializer(many=True, help_text="Due within 30, 60 and 90 days, today included.")
    total = serializers.IntegerField(help_text="Open deadlines up to 90 days ahead, overdue included.")
    rows = DeadlineRowSerializer(many=True, help_text="The first 10 of them.")


class CredentialApplicationSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(help_text="The discovery observation.")
    name = serializers.CharField()


class CredentialRecordSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()


class CredentialSerializer(ReadOnlySerializer):
    kind = serializers.ChoiceField(
        choices=["api_token", "discovered_credential"],
        help_text="api_token, or discovered_credential: a client secret or certificate discovery found.",
    )
    id = serializers.UUIDField()
    name = serializers.CharField(help_text="The token's name, or the credential's display name (else its key ID).")
    prefix = serializers.CharField(help_text="An API token's prefix; empty for a discovered credential.")
    owner = UserBriefSerializer(allow_null=True, help_text="An API token's owner; null for a discovered credential.")
    expires_on = serializers.DateField()
    days = serializers.IntegerField(help_text="Negative once expired.")
    expired = serializers.BooleanField()
    credential_type = serializers.ChoiceField(
        choices=["secret", "certificate"], allow_null=True, help_text="Discovered credentials only."
    )
    key_id = serializers.CharField(allow_null=True, help_text="Discovered credentials only.")
    application = CredentialApplicationSerializer(
        allow_null=True, help_text="Discovered credentials: the observed app it belongs to."
    )
    record = CredentialRecordSerializer(
        allow_null=True, help_text="Discovered credentials: the record the app is linked to, if any."
    )


class ExpiringCredentialsSerializer(ReadOnlySerializer):
    days = serializers.IntegerField()
    count = serializers.IntegerField()
    rows = CredentialSerializer(many=True)


class AttentionQueueCountSerializer(ReadOnlySerializer):
    queue = serializers.CharField()
    all = serializers.IntegerField()
    mine = serializers.IntegerField()
    unassigned = serializers.IntegerField()
    counted = serializers.BooleanField(help_text="False: listed on the page but not yet in the badge or digest.")


class DashboardAttentionSerializer(ReadOnlySerializer):
    counts = AttentionCountsSerializer()
    queues = AttentionQueueCountSerializer(many=True)


class DashboardSpendSerializer(ReadOnlySerializer):
    totals = CommitmentSerializer(many=True)
    by_vendor = DashboardSpendGroupSerializer(many=True)
    by_department = DashboardSpendGroupSerializer(many=True)
    by_category = DashboardSpendGroupSerializer(many=True)


class DashboardLicencesSerializer(ReadOnlySerializer):
    total = PoolTotalsSumSerializer(help_text="As GET /license-pools/totals `total`.")
    rows = PoolTotalsRowSerializer(many=True, help_text="Overused first, then the most utilized; at most 8.")


class DashboardSerializer(ReadOnlySerializer):
    as_of = serializers.DateField()
    needs_attention = DashboardAttentionSerializer()
    deadlines = DashboardDeadlinesSerializer(allow_null=True, help_text="Null without renewals and contracts.")
    expiring_credentials = ExpiringCredentialsSerializer()
    spend = DashboardSpendSerializer(allow_null=True, help_text="Null without the contracts capability.")
    license_utilization = DashboardLicencesSerializer(allow_null=True, help_text="Null without contracts.")
    data_quality = QualitySummarySerializer(allow_null=True, help_text="Null without data_quality.")


class CalendarFeedSerializer(ReadOnlySerializer):
    active = serializers.BooleanField()
    id = serializers.UUIDField(allow_null=True)
    prefix = serializers.CharField(allow_null=True, help_text="Tells feeds apart; never the secret.")
    created_at = serializers.DateTimeField(allow_null=True)
    last_used_at = serializers.DateTimeField(allow_null=True)


class CalendarFeedCreatedSerializer(CalendarFeedSerializer):
    url = serializers.URLField(help_text="Shown once: subscribe to it from Outlook, Google or Apple Calendar.")
