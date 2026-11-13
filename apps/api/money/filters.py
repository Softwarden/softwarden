"""The renewals list's filters (apps/web/src/renewals.tsx, detail.tsx). The deadline, timing and gaps are derived,
not columns, so the FilterSet validates every parameter (with the messages the list has always used) and the
viewset applies them to the derived states (`filter_states`); only `archived` is SQL."""

from django import forms

from catalog import services
from core.drf import ParamFilter, TenantFilterSet
from core.input import whole_number

from .models import AutoRenew, Decision, RenewalObligation


def worded(check, message):
    """A parser that returns check(value), or refuses with `message`; an empty value is no filter."""

    def parse(value: str):
        if value == "":
            return None
        result = check(value)
        if result is None:
            raise forms.ValidationError(message)
        return result

    return parse


def _bool(value: str):
    return {"true": True, "false": False}.get(value)


def _in(values):
    return lambda value: value if value in values else None


def _followup(value: str):
    return value if value == "none" or whole_number(value, 1) is not None else None


def _days(value: str):
    return whole_number(value)


def _application(value: str):
    return value if services.is_uuid(value) else None


TRUE_FALSE = "Use true or false."


class RenewalFilterSet(TenantFilterSet):
    archived = ParamFilter(
        parse=worded(_bool, TRUE_FALSE),
        method="derived",
        help_text="True: only archived renewals. Default: not archived.",
    )
    auto_renew = ParamFilter(
        parse=worded(_in(AutoRenew.values), "Choose yes, no or unknown."), method="derived", choices=AutoRenew.choices
    )
    followup = ParamFilter(
        parse=worded(_followup, "Enter a user ID, or none."), method="derived", help_text="User ID, or none."
    )
    decision = ParamFilter(
        parse=worded(_in(["none", *Decision.values]), "Choose none, renew, cancel or renegotiate."),
        method="derived",
        choices=[("none", "none"), *Decision.choices],
    )
    due_within = ParamFilter(
        parse=worded(_days, "Enter a number of days."),
        method="derived",
        help_text="Action deadline within this many days, overdue included.",
    )
    overdue = ParamFilter(parse=worded(_bool, TRUE_FALSE), method="derived")
    has_gaps = ParamFilter(parse=worded(_bool, TRUE_FALSE), method="derived")
    mine = ParamFilter(
        parse=worded(_bool, TRUE_FALSE),
        method="derived",
        help_text="Assigned to me, or unassigned when I am the coordinator.",
    )
    application = ParamFilter(
        parse=worded(_application, "Enter an application ID."),
        method="derived",
        help_text="Renewals whose current cycle covers this application.",
    )
    q = ParamFilter(
        parse=lambda v: v or None,
        method="derived",
        help_text="Search obligation, vendor and covered application names.",
    )

    class Meta:
        model = RenewalObligation
        fields: list[str] = []

    def derived(self, qs, name, value):
        return qs  # applied to the derived states by filter_states

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        return qs.filter(archived_at__isnull=not self.form.cleaned_data.get("archived"))


def filter_states(states, f, user, org):
    """The list filters (cleaned by RenewalFilterSet), applied to derived state."""
    if f.get("auto_renew"):
        states = [s for s in states if s.obligation.auto_renew == f["auto_renew"]]
    if f.get("followup") == "none":
        states = [s for s in states if s.obligation.followup_id is None]
    elif f.get("followup"):
        states = [s for s in states if s.obligation.followup_id == int(f["followup"])]
    if f.get("decision") == "none":
        states = [s for s in states if s.open and not s.cycle.decision]
    elif f.get("decision"):
        states = [s for s in states if s.cycle.decision == f["decision"]]
    if f.get("due_within") is not None:
        states = [s for s in states if s.timing and s.timing["days"] <= f["due_within"]]
    if f.get("overdue"):
        states = [s for s in states if s.timing and s.timing["overdue"]]
    if f.get("has_gaps") is not None:
        states = [s for s in states if bool(s.gaps) == f["has_gaps"]]
    if f.get("mine"):
        coordinator = org.coordinator_id == user.id
        states = [
            s
            for s in states
            if s.obligation.followup_id == user.id or (coordinator and s.obligation.followup_id is None)
        ]
    if f.get("application"):
        states = [s for s in states if any(str(a.id) == f["application"] for a in s.applications)]
    if f.get("q"):
        q = f["q"].casefold()
        states = [
            s
            for s in states
            if q in s.obligation.name.casefold()
            or (s.obligation.vendor and q in s.obligation.vendor.name.casefold())
            or any(q in a.name.casefold() for a in s.applications)
        ]
    return states
