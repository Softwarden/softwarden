"""Contract, contract line and licence pool responses. Read-only: writes go through money/contracts.py.
Money is a string with two decimal places (DRF's decimal format) next to its three-letter currency; derived values
(the annualised cost, the deadline, utilisation) are worked out in money/contracts.py and only rendered here."""

from decimal import Decimal
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog.models import RECORD_KIND_CHOICES, Lifecycle
from catalog.serializers import CUSTOM_FIELDS_WRITE, CustomFieldValuesMixin, PersonBriefSerializer
from core.drf import BriefSerializer, ReadOnlySerializer, UserBriefSerializer, represent

from . import contracts, renewal_rules
from .models import AutoRenew, BillingFrequency, ContractStatus, Decision, PeriodUnit
from .serializers import (
    DeadlineSerializer,
    PeriodSerializer,
    RenewalCycleSerializer,
    RenewalListSerializer,
    deadline_json,
)

FOUR_PLACES = Decimal("0.0001")


def _period(value, unit):
    return {"value": value, "unit": unit} if value else None


class ContractRecordSerializer(BriefSerializer):
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()


class ContractSerializer(CustomFieldValuesMixin, ReadOnlySerializer):
    """A contract in a list (and the create response). Context: `today`, `lead_days`, `custom_fields`, `counts`
    ({contract id: (lines, pools)}) and `drivers` (IDs of the contracts their renewal takes its dates from)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    vendor = BriefSerializer(read_only=True, allow_null=True)
    contract_number = serializers.CharField(read_only=True)
    po_number = serializers.CharField(read_only=True)
    amount = serializers.DecimalField(
        read_only=True, allow_null=True, help_text="Per billing period.", max_digits=14, decimal_places=2
    )
    currency = serializers.CharField(read_only=True, help_text="Three-letter code; amounts are never converted.")
    billing_frequency = serializers.ChoiceField(BillingFrequency.choices, read_only=True)
    annualized_cost = serializers.DecimalField(
        read_only=True,
        allow_null=True,
        help_text="Derived from the amount and billing frequency.",
        max_digits=14,
        decimal_places=2,
    )
    annualization_note = serializers.SerializerMethodField(help_text="How the annualized cost was worked out.")
    start_date = serializers.DateField(read_only=True, allow_null=True)
    end_date = serializers.DateField(read_only=True, allow_null=True)
    term_months = serializers.SerializerMethodField(
        help_text="The term in whole months from the dates (multi-year amounts are spread over it); null without both."
    )
    auto_renew = serializers.ChoiceField(AutoRenew.choices, read_only=True)
    notice = serializers.SerializerMethodField()
    cancellation_deadline = serializers.DateField(
        read_only=True, allow_null=True, help_text="Derived: deadline.date, stored for filters and reports."
    )
    deadline = serializers.SerializerMethodField(
        help_text="The deadline as a renewal states it (the same computation as its linked renewal's): Calculated "
        "(end date minus notice period), Assumed (minus the organization's lead time, no notice period) or Renewal "
        "action (it doesn't auto-renew)."
    )
    renewal_owner = PersonBriefSerializer(read_only=True, allow_null=True)
    status = serializers.ChoiceField(ContractStatus.choices, read_only=True)
    cost_center = serializers.CharField(read_only=True)
    notes = serializers.CharField(read_only=True)
    predecessor = BriefSerializer(read_only=True, allow_null=True, help_text="The term this one renews.")
    successor = serializers.SerializerMethodField(help_text="The term that renews this one.")
    renewal_obligation = BriefSerializer(read_only=True, allow_null=True, help_text="The linked renewal.")
    drives_renewal = serializers.SerializerMethodField(
        help_text="True when the linked renewal's open cycle takes its renewal date, notice period and auto-renewal "
        "from this contract (it is the renewal's newest term)."
    )
    archived = serializers.BooleanField(read_only=True)
    line_count = serializers.SerializerMethodField()
    license_pool_count = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_annualization_note(self, c) -> str:
        return contracts.annualization_note(c.amount, c.billing_frequency, c.start_date, c.end_date)

    def get_term_months(self, c) -> int | None:
        return contracts.term_months(c.start_date, c.end_date)

    @extend_schema_field(PeriodSerializer(allow_null=True))
    def get_notice(self, c) -> dict[str, Any] | None:
        return _period(c.notice_value, c.notice_unit)

    @extend_schema_field(DeadlineSerializer)
    def get_deadline(self, c) -> dict[str, Any]:
        deadline = contracts.contract_deadline(c, self.context["lead_days"])
        return deadline_json(deadline, renewal_rules.timing(deadline.date, self.context["today"]))

    @extend_schema_field(BriefSerializer(allow_null=True))
    def get_successor(self, c) -> dict[str, Any] | None:
        return represent(BriefSerializer, getattr(c, "successor", None))

    def get_drives_renewal(self, c) -> bool:
        return c.id in self.context["drivers"]

    def get_line_count(self, c) -> int:
        return self.context["counts"].get(c.id, (0, 0))[0]

    def get_license_pool_count(self, c) -> int:
        return self.context["counts"].get(c.id, (0, 0))[1]


class ContractLineSerializer(ReadOnlySerializer):
    """A contract line with its share of the annualized cost. Context: `allocation` ({line id: Allocation})."""

    id = serializers.UUIDField(read_only=True)
    record = serializers.SerializerMethodField()
    allocation_percent = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        read_only=True,
        allow_null=True,
        help_text="A fixed share; null shares what the fixed ones leave, evenly.",
    )
    share_percent = serializers.SerializerMethodField(help_text="The share it gets, fixed or even (4 places).")
    annual_amount = serializers.SerializerMethodField(
        help_text="Its share of the annualized cost, to the cent; a contract's lines (and remainder) sum exactly."
    )
    created_at = serializers.DateTimeField(read_only=True)

    @extend_schema_field(ContractRecordSerializer)
    def get_record(self, item) -> dict[str, Any]:
        r = item.resource
        return {
            "id": str(r.id),
            "name": r.name,
            "kind": r.kind,
            "lifecycle": r.lifecycle,
            "archived": r.archived_at is not None,
        }

    def _row(self, item):
        return self.context.get("allocation", {}).get(item.id)

    @extend_schema_field(serializers.DecimalField(max_digits=9, decimal_places=4, allow_null=True))
    def get_share_percent(self, item) -> str | None:
        row = self._row(item)
        return None if row is None else str(row.share.quantize(FOUR_PLACES))

    @extend_schema_field(serializers.DecimalField(allow_null=True, max_digits=14, decimal_places=2))
    def get_annual_amount(self, item) -> str | None:
        row = self._row(item)
        return None if row is None or item.contract.annualized_cost is None else str(row.amount)


class UnallocatedSerializer(serializers.Serializer):
    share_percent = serializers.DecimalField(max_digits=9, decimal_places=4)
    annual_amount = serializers.DecimalField(allow_null=True, max_digits=14, decimal_places=2)


class ContractPoolSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku_name = serializers.CharField()
    sku_id = serializers.CharField()
    application = BriefSerializer()
    seats_purchased = serializers.IntegerField()
    seats_assigned = serializers.IntegerField()
    seats_available = serializers.IntegerField(help_text="Purchased minus assigned; negative when overused.")
    utilization = serializers.FloatField(allow_null=True)
    overused = serializers.BooleanField()
    seat_sync = serializers.BooleanField()
    last_synced = serializers.DateTimeField(allow_null=True)


class ContractRenewalSerializer(RenewalListSerializer):
    history = RenewalCycleSerializer(many=True, help_text="Earlier cycles, newest first; read-only.")


class ContractDetailSerializer(ContractSerializer):
    """GET, PATCH and the actions: the list's fields plus the lines with their share of the cost, the fixed total,
    the unallocated remainder and the license pools bought under it. Context also needs `lines`, `allocation`,
    `unallocated` and `pools`."""

    lines = serializers.SerializerMethodField()
    allocated_percent = serializers.SerializerMethodField(help_text="The sum of the fixed allocations (at most 100).")
    unallocated = serializers.SerializerMethodField(
        help_text="What no line takes (fixed shares under 100 and no line without one, or no lines at all); null "
        "when the lines take it all."
    )
    license_pools = serializers.SerializerMethodField()
    renewal = serializers.SerializerMethodField(
        help_text="The linked renewal as GET /renewals/{id} gives it (without your snooze): its open or latest cycle, "
        "with the deadline, reminders and terms_change, and every earlier cycle in history. Null when not linked."
    )

    @extend_schema_field(ContractRenewalSerializer(allow_null=True))
    def get_renewal(self, c) -> dict[str, Any] | None:
        return self.context.get("renewals", {}).get(c.renewal_obligation_id)

    @extend_schema_field(ContractLineSerializer(many=True))
    def get_lines(self, c) -> list[dict[str, Any]]:
        lines = self.context["lines"].get(c.id, [])
        return list(ContractLineSerializer(lines, many=True, context={"allocation": self.context["allocation"]}).data)

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2))
    def get_allocated_percent(self, c) -> str:
        lines = self.context["lines"].get(c.id, [])
        total = sum((i.allocation_percent for i in lines if i.allocation_percent is not None), contracts.ZERO)
        return str(total.quantize(contracts.CENT))

    @extend_schema_field(UnallocatedSerializer(allow_null=True))
    def get_unallocated(self, c) -> dict[str, Any] | None:
        row = self.context["unallocated"].get(c.id)
        if row is None:
            return None
        amount = None if c.annualized_cost is None else str(row.amount)
        return {"share_percent": str(row.share.quantize(FOUR_PLACES)), "annual_amount": amount}

    @extend_schema_field(ContractPoolSerializer(many=True))
    def get_license_pools(self, c) -> list[dict[str, Any]]:
        return [
            {
                "id": str(p.id),
                "sku_name": p.sku_name,
                "sku_id": p.sku_id,
                "application": {"id": str(p.application.id), "name": p.application.name},
                "seats_purchased": p.seats_purchased,
                "seats_assigned": p.seats_assigned,
                "seats_available": p.seats_purchased - p.seats_assigned,
                "utilization": contracts.utilization(p),
                "overused": p.seats_assigned > p.seats_purchased,
                "seat_sync": p.seat_sync,
                "last_synced": p.last_synced.isoformat() if p.last_synced else None,
            }
            for p in self.context["pools"].get(c.id, [])
        ]


class ContractInputSerializer(serializers.Serializer):
    """Create and PATCH (PATCH changes only the fields sent). The service validates; this documents the body."""

    name = serializers.CharField(max_length=200)
    vendor = serializers.UUIDField(required=False, allow_null=True)
    contract_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    po_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    amount = serializers.DecimalField(
        required=False,
        allow_null=True,
        help_text="What is billed per billing period (for a multi-year term, the whole term; usage-based, the annual "
        "estimate). A number or a string, at most two decimal places, not negative.",
        max_digits=14,
        decimal_places=2,
    )
    currency = serializers.CharField(required=False, max_length=3, help_text="Three letters; default USD.")
    billing_frequency = serializers.ChoiceField(BillingFrequency.choices, required=False, help_text="Default annual.")
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True, help_text="Not before the start date.")
    auto_renew = serializers.ChoiceField(AutoRenew.choices, required=False, help_text="Default unknown.")
    notice_value = serializers.IntegerField(required=False, allow_null=True, help_text="1-1000, with notice_unit.")
    notice_unit = serializers.ChoiceField(PeriodUnit.choices, required=False, allow_blank=True)
    renewal_owner = serializers.UUIDField(required=False, allow_null=True, help_text="An active Person.")
    status = serializers.ChoiceField(ContractStatus.choices, required=False, help_text="Default active.")
    cost_center = serializers.CharField(required=False, allow_blank=True, max_length=50)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    renewal_obligation = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Link an existing renewal (it needs an open cycle, and the contract an end date): its open cycle "
        "then takes this contract's end date as its renewal date, its notice period and auto-renewal, and its entered "
        "deadline is cleared. Null unlinks (the renewal keeps the dates it has).",
    )
    create_renewal_obligation = serializers.BooleanField(
        required=False,
        help_text="True creates a renewal for this contract (named after it, its vendor, renewal owner, dates and the "
        "applications on it) and links it. Needs an end date; not with renewal_obligation.",
    )
    custom_field_data = serializers.DictField(required=False, help_text=CUSTOM_FIELDS_WRITE)


class ContractLineInputSerializer(serializers.Serializer):
    record = serializers.UUIDField(help_text="An application, component or AI agent; create only.")
    allocation_percent = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
        help_text="0-100; a contract's fixed allocations sum to at most 100. Null shares the rest evenly.",
    )


class ContractPreviewSerializer(serializers.Serializer):
    annualized_cost = serializers.DecimalField(allow_null=True, max_digits=14, decimal_places=2)
    annualization_note = serializers.CharField()
    term_months = serializers.IntegerField(allow_null=True)
    deadline = DeadlineSerializer()


class LicensePoolSerializer(CustomFieldValuesMixin, ReadOnlySerializer):
    """A license pool. Seats assigned may exceed seats purchased (overuse is shown, not refused)."""

    id = serializers.UUIDField(read_only=True)
    application = BriefSerializer(read_only=True)
    contract = BriefSerializer(read_only=True, allow_null=True)
    sku_name = serializers.CharField(read_only=True)
    sku_id = serializers.CharField(
        read_only=True, help_text="Matches Microsoft Graph subscribedSkus skuId when synced."
    )
    seats_purchased = serializers.IntegerField(read_only=True)
    seats_assigned = serializers.IntegerField(read_only=True)
    seats_available = serializers.SerializerMethodField(help_text="Purchased minus assigned; negative when overused.")
    utilization = serializers.SerializerMethodField(
        help_text="Percent of purchased seats assigned (one decimal); null when none are purchased; over 100 is "
        "overuse."
    )
    overused = serializers.SerializerMethodField()
    seat_sync = serializers.BooleanField(read_only=True, help_text="Entra license discovery may overwrite the seats.")
    last_synced = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Set by the seat sync path only; never by a person."
    )
    seat_proposals = serializers.SerializerMethodField(
        help_text="Open proposed seat counts from sources that disagree with this pool (GET "
        "/license-pools/seat-review?pool=<id>)."
    )
    synced_from = serializers.SerializerMethodField(
        help_text="Seat sync on: the one enabled Entra licenses connector whose latest observations report this SKU, "
        "the source its counts sync from; null when none does, when two or more report it (sync_held), and for a pool "
        "that doesn't sync."
    )
    sync_held = serializers.SerializerMethodField(
        help_text="Seat sync on and two or more enabled connectors report this SKU: no run changes the counts; they "
        "are proposals, one per source, until one connector is turned off or its SKU excluded."
    )
    notes = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    @extend_schema_field(BriefSerializer(allow_null=True))
    def get_synced_from(self, pool) -> dict[str, Any] | None:
        return self.context.get("sync_sources", {}).get(pool.id, {}).get("synced_from")

    def get_sync_held(self, pool) -> bool:
        return bool(self.context.get("sync_sources", {}).get(pool.id, {}).get("sync_held", False))

    def get_seats_available(self, pool) -> int:
        return pool.seats_purchased - pool.seats_assigned

    def get_utilization(self, pool) -> float | None:
        return contracts.utilization(pool)

    def get_overused(self, pool) -> bool:
        return pool.seats_assigned > pool.seats_purchased

    def get_seat_proposals(self, pool) -> int:
        return self.context.get("seat_proposals", {}).get(pool.id, 0)


class LicensePoolInputSerializer(serializers.Serializer):
    application = serializers.UUIDField(help_text="An application.")
    contract = serializers.UUIDField(required=False, allow_null=True)
    sku_name = serializers.CharField(max_length=200)
    sku_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    seats_purchased = serializers.IntegerField(required=False, min_value=0)
    seats_assigned = serializers.IntegerField(required=False, min_value=0, help_text="May exceed seats_purchased.")
    seat_sync = serializers.BooleanField(
        required=False, help_text="Needs a SKU ID; at most one pool per SKU in the organization."
    )
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    custom_field_data = serializers.DictField(required=False, help_text=CUSTOM_FIELDS_WRITE)


# ---- the renew action (money/renewal_action.py) -----------------------------------------------------------


class RenewCycleBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sequence = serializers.IntegerField()
    renewal_date = serializers.DateField(allow_null=True)
    decision = serializers.ChoiceField(Decision.choices, allow_null=True)


class RenewDefaultsSerializer(serializers.Serializer):
    name = serializers.CharField()
    contract_number = serializers.CharField()
    po_number = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    currency = serializers.CharField()
    billing_frequency = serializers.ChoiceField(BillingFrequency.choices)
    start_date = serializers.DateField(allow_null=True, help_text="The day after this contract ends.")
    end_date = serializers.DateField(allow_null=True, help_text="As long as this term (a year without a start date).")
    auto_renew = serializers.ChoiceField(AutoRenew.choices)
    notice_value = serializers.IntegerField(allow_null=True)
    notice_unit = serializers.CharField()
    notes = serializers.CharField()
    lines = serializers.ListField(child=serializers.UUIDField(), help_text="Every line, carried with its allocation.")
    pools = serializers.ListField(child=serializers.UUIDField(), help_text="Every license pool bought under it.")
    pool_action = serializers.ChoiceField(["move", "copy"])
    overlap_confirmed = serializers.BooleanField()


class RenewTermsSerializer(serializers.Serializer):
    can_renew = serializers.BooleanField(help_text="False when POST would be refused (409); `refusal` says why.")
    refusal = serializers.CharField(allow_null=True)
    renewal = BriefSerializer(allow_null=True, help_text="The linked renewal whose cycle records the decision.")
    open_cycle = RenewCycleBriefSerializer(
        allow_null=True, help_text="The cycle the decision is recorded on; null when not linked or none is open."
    )
    defaults = RenewDefaultsSerializer(help_text="The next term POST creates when a field isn't sent.")


class RenewInputSerializer(serializers.Serializer):
    """The decision, and for renew or renegotiate the next term (anything not sent takes GET's defaults)."""

    decision = serializers.ChoiceField(Decision.choices)
    decision_notes = serializers.CharField(required=False, help_text="Required to cancel.")
    decided_on = serializers.DateField(required=False, help_text="Default today; not in the future.")
    completion_evidence = serializers.CharField(
        required=False, help_text="Completes the renewal's cycle; default names the next term or the cancellation."
    )
    name = serializers.CharField(required=False, max_length=200)
    contract_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    po_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    amount = serializers.DecimalField(required=False, allow_null=True, max_digits=14, decimal_places=2)
    currency = serializers.CharField(required=False, max_length=3)
    billing_frequency = serializers.ChoiceField(BillingFrequency.choices, required=False)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, help_text="After this contract's end date.")
    auto_renew = serializers.ChoiceField(AutoRenew.choices, required=False)
    notice_value = serializers.IntegerField(required=False, allow_null=True)
    notice_unit = serializers.ChoiceField(PeriodUnit.choices, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    custom_field_data = serializers.DictField(
        required=False,
        help_text="Values over the ones copied from this contract; validated as on a new contract (required fields).",
    )
    lines = serializers.ListField(
        child=serializers.UUIDField(), required=False, help_text="This contract's line IDs to carry; default all."
    )
    pools = serializers.ListField(
        child=serializers.UUIDField(), required=False, help_text="This contract's license pool IDs; default all."
    )
    pool_action = serializers.ChoiceField(
        ["move", "copy"], required=False, help_text="Move (default: seats counted once) or copy (not syncing)."
    )
    overlap_confirmed = serializers.BooleanField(
        required=False, help_text="Needed when the next term starts on or before this one's end date."
    )


# ---- licence pool utilisation and the seat review (money/licences.py) --------------------------------------


class TotalsApplicationSerializer(BriefSerializer):
    archived = serializers.BooleanField()


class UtilisationSerializer(serializers.Serializer):
    seats_purchased = serializers.IntegerField()
    seats_assigned = serializers.IntegerField()
    seats_available = serializers.IntegerField()
    utilization = serializers.FloatField(allow_null=True)
    overused = serializers.BooleanField()


class PoolTotalsRowSerializer(UtilisationSerializer):
    application = TotalsApplicationSerializer()
    sku_name = serializers.CharField()
    sku_id = serializers.CharField()
    pools = serializers.IntegerField()


class PoolTotalsSumSerializer(UtilisationSerializer):
    pools = serializers.IntegerField()
    overused_rows = serializers.IntegerField(help_text="Application and SKU rows with more assigned than purchased.")


class PoolTotalsSerializer(serializers.Serializer):
    rows = PoolTotalsRowSerializer(many=True, help_text="Per application and SKU (SKU ID, else name).")
    total = PoolTotalsSumSerializer()


class SeatDifferenceSerializer(serializers.Serializer):
    field = serializers.ChoiceField(["seats_purchased", "seats_assigned"])
    current = serializers.IntegerField()
    proposed = serializers.IntegerField()


class SeatSourceSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The observation.")
    display_name = serializers.CharField()
    connector = BriefSerializer()
    last_seen_at = serializers.DateTimeField()


class ObservedSeatsSerializer(serializers.Serializer):
    seats_purchased = serializers.IntegerField(allow_null=True)
    seats_assigned = serializers.IntegerField(allow_null=True)


class SeatPoolSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sku_name = serializers.CharField()
    sku_id = serializers.CharField()
    application = BriefSerializer()
    contract = BriefSerializer(allow_null=True)
    seats_purchased = serializers.IntegerField()
    seats_assigned = serializers.IntegerField()
    seat_sync = serializers.BooleanField()
    last_synced = serializers.DateTimeField(allow_null=True)


class SeatDismissalSerializer(serializers.Serializer):
    reason = serializers.CharField()
    by = UserBriefSerializer(allow_null=True)
    at = serializers.DateTimeField()


class SeatProposalSerializer(serializers.Serializer):
    pool = SeatPoolSerializer()
    observation = SeatSourceSerializer(help_text="The license observation: what the source said, and when.")
    observed = ObservedSeatsSerializer()
    differences = SeatDifferenceSerializer(many=True)
    status = serializers.ChoiceField(["open", "dismissed"])
    dismissal = SeatDismissalSerializer(allow_null=True)


class UnboundSkuSerializer(serializers.Serializer):
    observation = SeatSourceSerializer(help_text="The license observation: what the source said, and when.")
    sku_id = serializers.CharField()
    sku_part_number = serializers.CharField()
    observed = ObservedSeatsSerializer()


class SeatReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(["open", "dismissed", "all"])
    results = SeatProposalSerializer(many=True)
    unbound = UnboundSkuSerializer(
        many=True, help_text="SKUs a source reports that no pool has as its SKU ID (add a pool to track them)."
    )


class SeatApplyInputSerializer(serializers.Serializer):
    counts = serializers.ListField(
        child=serializers.ChoiceField(["seats_purchased", "seats_assigned"]),
        required=False,
        help_text="Which differing counts to take; default all that differ.",
    )


class SeatDismissInputSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500)


class UnboundCreateInputSerializer(serializers.Serializer):
    application = serializers.UUIDField(help_text="The application the seats are for.")
    contract = serializers.UUIDField(required=False, allow_null=True)
    sku_name = serializers.CharField(required=False, help_text="Default: the SKU's part number.")
    seat_sync = serializers.BooleanField(
        required=False,
        help_text="Let every run of the source overwrite the counts (the one exception to discovery never overwriting curated data). Default off.",
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    custom_field_data = serializers.DictField(required=False)


class BindInputSerializer(serializers.Serializer):
    observation = serializers.UUIDField(help_text="The unbound SKU's license observation (seat-review `unbound`).")
    seat_sync = serializers.BooleanField(
        required=False, help_text="Also turn seat sync on: the next run then overwrites the counts. Default off."
    )
