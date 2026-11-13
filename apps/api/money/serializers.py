"""Renewal responses. A renewal's state is derived (money/renewals.RenewalState and renewal_rules),
so the response is built here as plain data from that state, and the serializers below describe each shape for
OpenAPI; tests/test_drf_resources.py checks that each description has exactly the keys the endpoint returns."""

from zoneinfo import ZoneInfo

from rest_framework import serializers

from catalog.serializers import PersonBriefSerializer
from core import dates
from core.drf import BriefSerializer, UserBriefSerializer, represent, timestamp
from notify.models import SnoozeKind
from tickets.serializers import TicketLinkSerializer

from . import renewal_rules
from .models import AutoRenew, Decision, PeriodUnit

# ---- building the responses -------------------------------------------------------------------------------------


def _period(value, unit):
    return {"value": value, "unit": unit} if value else None


def deadline_json(deadline, timing):
    return {
        "date": deadline.date.isoformat() if deadline.date else None,
        "label": deadline.label,
        "label_text": renewal_rules.LABELS.get(deadline.label),
        "explanation": deadline.explanation,
        "timing": timing,
    }


def cycle_json(org, obligation, c, lead_days, today):
    deadline = renewal_rules.action_deadline(
        obligation.auto_renew, c.renewal_date, c.notice_value, c.notice_unit, c.explicit_deadline, lead_days
    )
    open_ = c.completed_on is None
    return {
        "id": str(c.id),
        "sequence": c.sequence,
        "open": open_,
        "renewal_date": c.renewal_date and c.renewal_date.isoformat(),
        "notice": _period(c.notice_value, c.notice_unit),
        "explicit_deadline": c.explicit_deadline and c.explicit_deadline.isoformat(),
        "term": _period(c.term_value, c.term_unit),
        "applications": [
            {"id": str(a.id), "name": a.name, "lifecycle": a.lifecycle, "archived": a.archived} for a in c.coverage
        ],
        "deadline": deadline_json(deadline, renewal_rules.timing(deadline.date, today) if open_ else None),
        "decision": c.decision or None,
        "decision_notes": c.decision_notes,
        "decided_on": c.decided_on and c.decided_on.isoformat(),
        "completed_on": c.completed_on and c.completed_on.isoformat(),
        "completion_evidence": c.completion_evidence,
        "next_renewal_date": (n := renewal_rules.next_renewal_date(c)) and n.isoformat(),
        "reminders": reminders_json(org, c, deadline.date, today),
        "terms_change": terms_change_json(c),
    }


def _plain(value):
    return None if value in (None, "") else (value.isoformat() if hasattr(value, "isoformat") else str(value))


def terms_change_json(c):
    """What a decision on a contract's term changed (money 0005), or None when none was recorded."""
    from .renewals import TERM_COLUMNS

    if all(getattr(c, k, None) in (None, "") for k in TERM_COLUMNS):
        return None
    return {k: _plain(getattr(c, k)) for k in TERM_COLUMNS}


def reminders_json(org, cycle, deadline, today):
    """Each reminder threshold of the organization with the day it falls on and, once it has fired, the digest that
    carried it ("90 days, sent 2026-09-01")."""
    fired = {r.threshold_days: r for r in cycle.reminders}
    zone = ZoneInfo(org.timezone)
    rows = []
    for t in org.reminder_thresholds:
        r = fired.get(t)
        rows.append(
            {
                "days": t,
                "on": (on := dates.shift(deadline, -t, "days")) and on.isoformat() if deadline else None,
                "status": "not_sent" if r is None else (r.status or "sent"),
                # The digest's date; if that digest row is gone, the day the reminder was recorded.
                "sent_on": (r.for_date or r.created_at.astimezone(zone).date()).isoformat() if r else None,
            }
        )
    return rows


def renewal_json(org, state, today, history=False):
    o = state.obligation
    body = {
        "id": str(o.id),
        "name": o.name,
        "vendor": represent(BriefSerializer, o.vendor),
        "responsible": represent(PersonBriefSerializer, o.responsible),
        "followup": represent(UserBriefSerializer, o.followup),
        "auto_renew": o.auto_renew,
        "archived": o.archived,
        "cycle": cycle_json(org, o, state.cycle, org.renewal_lead_days, today),
        "deadline": deadline_json(state.deadline, state.timing),
        "gaps": state.gaps,
        "cycle_count": o.cycle_count,
        "updated_at": timestamp(o.updated_at),
        "contract": represent(BriefSerializer, state.contract),
    }
    if history:
        body["history"] = [cycle_json(org, o, c, org.renewal_lead_days, today) for c in state.cycles[1:]]
    return body


# ---- descriptions -----------------------------------------------------------------------------------------------------


class TimingSerializer(serializers.Serializer):
    days = serializers.IntegerField(help_text="Days until the action deadline; negative when overdue.")
    overdue = serializers.BooleanField(help_text="True from the day after the deadline, organization-local.")
    text = serializers.CharField(help_text="For example 'in 30 days' or 'Overdue by 3 days'.")


class PeriodSerializer(serializers.Serializer):
    value = serializers.IntegerField()
    unit = serializers.ChoiceField(PeriodUnit.choices)


LABELS = ["entered", "calculated", "assumed", "renewal_action"]


class DeadlineSerializer(serializers.Serializer):
    date = serializers.DateField(allow_null=True, help_text="The last on-time date to act; derived, never stored.")
    label = serializers.ChoiceField(LABELS, allow_null=True)  # type: ignore[assignment]  # the API field is called label
    label_text = serializers.CharField(allow_null=True, help_text="Entered, Calculated, Assumed or Renewal action.")
    explanation = serializers.CharField(help_text="How the deadline was worked out, in words.")
    timing = TimingSerializer(allow_null=True, help_text="Null for a completed cycle or when there is no deadline.")


class CoveredApplicationSerializer(BriefSerializer):
    lifecycle = serializers.CharField()
    archived = serializers.BooleanField()


class ReminderThresholdSerializer(serializers.Serializer):
    days = serializers.IntegerField()
    on = serializers.DateField(allow_null=True, help_text="The day the threshold falls on.")
    status = serializers.ChoiceField(["not_sent", "pending", "sent", "failed", "skipped"])
    sent_on = serializers.DateField(allow_null=True)


class TermsChangeSerializer(serializers.Serializer):
    old_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True, coerce_to_string=True)
    old_currency = serializers.CharField(allow_null=True)
    old_billing_frequency = serializers.CharField(allow_null=True)
    old_annualized_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, allow_null=True, coerce_to_string=True
    )
    old_start_date = serializers.DateField(allow_null=True)
    old_end_date = serializers.DateField(allow_null=True)
    new_amount = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True, coerce_to_string=True)
    new_currency = serializers.CharField(allow_null=True)
    new_billing_frequency = serializers.CharField(allow_null=True)
    new_annualized_cost = serializers.DecimalField(
        max_digits=14, decimal_places=2, allow_null=True, coerce_to_string=True
    )
    new_start_date = serializers.DateField(allow_null=True)
    new_end_date = serializers.DateField(allow_null=True)


class RenewalCycleSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sequence = serializers.IntegerField()
    open = serializers.BooleanField(help_text="False once completed; a completed cycle is history and read-only.")
    renewal_date = serializers.DateField(allow_null=True)
    notice = PeriodSerializer(allow_null=True)
    explicit_deadline = serializers.DateField(allow_null=True, help_text="Entered cancellation or action deadline.")
    term = PeriodSerializer(allow_null=True)
    applications = CoveredApplicationSerializer(many=True)
    deadline = DeadlineSerializer()
    decision = serializers.ChoiceField(Decision.choices, allow_null=True)
    decision_notes = serializers.CharField()
    decided_on = serializers.DateField(allow_null=True)
    completed_on = serializers.DateField(allow_null=True)
    completion_evidence = serializers.CharField()
    next_renewal_date = serializers.DateField(
        allow_null=True, help_text="Renewal date plus the term: the default for the next cycle."
    )
    reminders = ReminderThresholdSerializer(
        many=True, help_text="Each reminder threshold, the day it falls on and when it fired."
    )
    terms_change = TermsChangeSerializer(
        allow_null=True,
        help_text="What a decision on a contract's term changed: the money and dates before "
        "and after, as recorded. Null unless the contract renew action recorded them.",
    )


class RenewalListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    vendor = BriefSerializer(allow_null=True)
    responsible = PersonBriefSerializer(allow_null=True)
    followup = UserBriefSerializer(allow_null=True, help_text="The IT follow-up assignee, an Admin or Editor.")
    auto_renew = serializers.ChoiceField(AutoRenew.choices)
    archived = serializers.BooleanField()
    cycle = RenewalCycleSerializer(help_text="The latest cycle.")
    deadline = DeadlineSerializer(help_text="The latest cycle's action deadline; timing is null once it is complete.")
    gaps = serializers.ListField(child=serializers.CharField(), help_text="For example 'Assumed deadline'.")
    cycle_count = serializers.IntegerField()
    updated_at = serializers.DateTimeField()
    contract = BriefSerializer(
        allow_null=True,
        help_text="The linked contract the open cycle takes its renewal date, notice period and auto-renewal from "
        "; those can't be changed on the renewal while it is set.",
    )


class RenewalSnoozeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(SnoozeKind.choices)
    until = serializers.DateField(allow_null=True)
    queue = serializers.ChoiceField(["renewal_actions", "renewal_gaps"])


class RenewalSerializer(RenewalListSerializer):
    history = RenewalCycleSerializer(many=True, help_text="Earlier cycles, newest first; read-only.")
    ticket_links = TicketLinkSerializer(
        many=True,
        help_text="Linked Jira, ServiceNow, Halo and other tickets (GET /renewals/{id}/ticket-links). "
        "Empty without the ticket_links capability.",
    )
    snooze = RenewalSnoozeSerializer(
        allow_null=True,
        required=False,
        help_text="GET and PATCH only: your snooze or acknowledgment of it, while it holds.",
    )


class RenewalInputSerializer(serializers.Serializer):
    """Create takes the obligation and its first cycle together; PATCH takes the obligation fields only."""

    name = serializers.CharField()
    vendor = serializers.UUIDField(required=False, allow_null=True)
    responsible = serializers.UUIDField(required=False, allow_null=True, help_text="An active Person.")
    followup = serializers.IntegerField(required=False, allow_null=True, help_text="An Admin or Editor user.")
    auto_renew = serializers.ChoiceField(AutoRenew.choices, required=False, help_text="Default unknown.")
    renewal_date = serializers.DateField(required=False, allow_null=True, help_text="Create only.")
    notice_value = serializers.IntegerField(required=False, allow_null=True, help_text="Create only. 1-1000.")
    notice_unit = serializers.ChoiceField(PeriodUnit.choices, required=False, help_text="Create only.")
    explicit_deadline = serializers.DateField(required=False, allow_null=True, help_text="Create only.")
    term_value = serializers.IntegerField(required=False, allow_null=True, help_text="Create only.")
    term_unit = serializers.ChoiceField(PeriodUnit.choices, required=False, help_text="Create only.")
    applications = serializers.ListField(
        child=serializers.UUIDField(), required=False, help_text="Create only. Application IDs."
    )


class RenewalCycleInputSerializer(serializers.Serializer):
    renewal_date = serializers.DateField(required=False, allow_null=True)
    notice_value = serializers.IntegerField(required=False, allow_null=True)
    notice_unit = serializers.ChoiceField(PeriodUnit.choices, required=False)
    explicit_deadline = serializers.DateField(required=False, allow_null=True)
    term_value = serializers.IntegerField(required=False, allow_null=True)
    term_unit = serializers.ChoiceField(PeriodUnit.choices, required=False)
    applications = serializers.ListField(child=serializers.UUIDField(), required=False)


class RenewalDecisionInputSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(
        [*Decision.choices, ("", "")], help_text="Empty clears it. A decision never completes the cycle."
    )
    decision_notes = serializers.CharField(required=False)
    decided_on = serializers.DateField(required=False, help_text="Default today; not in the future.")


class RenewalCompletionInputSerializer(serializers.Serializer):
    completed_on = serializers.DateField(help_text="Not in the future.")
    completion_evidence = serializers.CharField(help_text="A reference such as a PO number or an email.")
