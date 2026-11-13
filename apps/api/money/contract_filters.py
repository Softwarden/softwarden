"""The contract and licence pool lists' filters. Unknown parameters and bad values are one 400 problem
(core/drf.FilterBackend); custom field filters `cf_<name>` come from catalog/filters.CustomFieldFilters."""

from datetime import date

from django import forms
from django.db.models import Exists, F, OuterRef, Q

from catalog.filters import CustomFieldFilters
from core.drf import ParamFilter, choice, choices_csv, stripped, uuid_value

from .models import AutoRenew, BillingFrequency, Contract, ContractItem, ContractStatus, LicensePool


def day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


def uuid_or_none(value: str) -> str:
    return value if value == "none" else uuid_value(value)


def none_only(value: str) -> str:
    if value != "none":
        raise forms.ValidationError("Use none (pools without a SKU ID).")
    return value


def currency(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or not code.isascii():
        raise forms.ValidationError("Enter a three-letter currency code.")
    return code


TRUE_FALSE = choice(("true", "false"))


class ContractFilterSet(CustomFieldFilters):
    custom_kind = "contract"

    q = ParamFilter(parse=stripped, method="filter_q", help_text="Name, contract or PO number, or vendor name.")
    vendor = ParamFilter(parse=uuid_value, field_name="vendor_id", help_text="Vendor ID.")
    status = ParamFilter(
        parse=choices_csv(ContractStatus.values),
        field_name="status",
        lookup_expr="in",
        help_text="Comma-separated statuses, e.g. active,pending_renewal.",
        choices=ContractStatus.choices,
    )
    billing_frequency = ParamFilter(
        parse=choices_csv(BillingFrequency.values),
        field_name="billing_frequency",
        lookup_expr="in",
        help_text="Comma-separated billing frequencies.",
        choices=BillingFrequency.choices,
    )
    auto_renew = ParamFilter(
        parse=choice(AutoRenew.values),
        field_name="auto_renew",
        choices=AutoRenew.choices,
        help_text="yes, no, unknown.",
    )
    cost_center = ParamFilter(
        parse=stripped, field_name="cost_center", lookup_expr="iexact", help_text="Exact, ignoring case."
    )
    currency = ParamFilter(parse=currency, field_name="currency", help_text="Three-letter code.")
    end_after = ParamFilter(
        parse=day, field_name="end_date", lookup_expr="gte", help_text="Ends on or after (YYYY-MM-DD)."
    )
    end_before = ParamFilter(
        parse=day, field_name="end_date", lookup_expr="lte", help_text="Ends on or before (YYYY-MM-DD)."
    )
    deadline_after = ParamFilter(
        parse=day,
        field_name="cancellation_deadline",
        lookup_expr="gte",
        help_text="Cancellation deadline on or after (YYYY-MM-DD).",
    )
    deadline_before = ParamFilter(
        parse=day,
        field_name="cancellation_deadline",
        lookup_expr="lte",
        help_text="Cancellation deadline on or before (YYYY-MM-DD).",
    )
    renewal_obligation = ParamFilter(
        parse=uuid_or_none, method="filter_obligation", help_text="Linked renewal ID, or none for unlinked contracts."
    )
    record = ParamFilter(
        parse=uuid_value,
        method="filter_record",
        help_text="Contracts with a line for this application, component or AI agent.",
    )
    archived = ParamFilter(
        parse=choice(("true", "false", "all")),
        method="filter_archived",
        help_text="Default false (unarchived only); true for archived only; all for both.",
    )

    class Meta:
        model = Contract
        fields: list[str] = []

    def filter_q(self, qs, name, value):
        return qs.filter(
            Q(name__icontains=value)
            | Q(contract_number__icontains=value)
            | Q(po_number__icontains=value)
            | Q(vendor__name__icontains=value)
        )

    def filter_obligation(self, qs, name, value):
        return qs.filter(renewal_obligation__isnull=True) if value == "none" else qs.filter(renewal_obligation_id=value)

    def filter_record(self, qs, name, value):
        return qs.filter(Exists(ContractItem.objects.filter(contract_id=OuterRef("pk"), resource_id=value)))

    def filter_archived(self, qs, name, value):
        return qs  # applied in filter_queryset, so the default (false) holds when the parameter is absent

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        archived = self.form.cleaned_data.get("archived") or "false"
        if archived != "all":
            qs = qs.filter(archived_at__isnull=archived == "false")
        return qs


class LicensePoolFilterSet(CustomFieldFilters):
    custom_kind = "license_pool"

    q = ParamFilter(parse=stripped, method="filter_q", help_text="SKU name or ID, or the application's name.")
    application = ParamFilter(parse=uuid_value, field_name="application_id", help_text="Application ID.")
    contract = ParamFilter(parse=uuid_or_none, method="filter_contract", help_text="Contract ID, or none.")
    seat_sync = ParamFilter(parse=TRUE_FALSE, method="filter_sync", help_text="true or false.")
    sku_id = ParamFilter(
        parse=none_only,
        method="filter_sku_id",
        help_text="none: pools without a SKU ID (the ones an unbound SKU can be bound to).",
    )
    overused = ParamFilter(
        parse=TRUE_FALSE, method="filter_overused", help_text="true: more seats assigned than purchased."
    )

    class Meta:
        model = LicensePool
        fields: list[str] = []

    def filter_q(self, qs, name, value):
        return qs.filter(
            Q(sku_name__icontains=value) | Q(sku_id__icontains=value) | Q(application__name__icontains=value)
        )

    def filter_contract(self, qs, name, value):
        return qs.filter(contract__isnull=True) if value == "none" else qs.filter(contract_id=value)

    def filter_sku_id(self, qs, name, value):
        return qs.filter(sku_id__regex=r"^\s*$")

    def filter_sync(self, qs, name, value):
        return qs.filter(seat_sync=value == "true")

    def filter_overused(self, qs, name, value):
        over = Q(seats_assigned__gt=F("seats_purchased"))
        return qs.filter(over) if value == "true" else qs.exclude(over)
