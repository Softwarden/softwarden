"""Renewal obligations and cycles: every write, with organization scope, validation and audit.

An obligation is created together with its first cycle. Only the open cycle can change; a decision never
completes it; completion needs a date and evidence; the next cycle is established explicitly and starts from the
previous renewal date plus the term. Earlier cycles are history and are never rewritten. Audit events target
the obligation, so its Audit activity shows every cycle's events; the cycle ID is in the payload.
"""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from catalog import snapshots
from catalog.models import Kind, Person, Resource, Vendor
from catalog.services import (
    EVIDENCE_MAX,
    NAME_MAX,
    TEXT_MAX,
    WRITE_ROLES,
    _in_org,
    audit,
    is_uuid,
    org_today,
    refuse_while_referenced,
)
from core import dates, limits
from core.input import text_value, whole_number
from core.problems import Conflict, FieldErrors
from notify.models import ReminderDispatch

from . import renewal_rules
from .models import AutoRenew, Decision, PeriodUnit, RenewalCycle, RenewalCycleApplication, RenewalObligation

DEADLINE_INPUTS = {"renewal_date", "notice_value", "notice_unit", "explicit_deadline"}
# A cycle's inputs a linked contract owns (money/contracts.py, "One source of truth"), with the obligation's auto-renewal.
CONTRACT_INPUTS = ("renewal_date", "notice_value", "notice_unit", "explicit_deadline")
PERIOD_MAX = 1000  # no notice period or term is longer than this many units

OBLIGATION_FIELDS = ("name", "vendor", "responsible", "followup", "auto_renew")
CYCLE_FIELDS = (
    "renewal_date",
    "notice_value",
    "notice_unit",
    "explicit_deadline",
    "term_value",
    "term_unit",
    "applications",
)


def _date(data, field, errors, values, allow_future=True, today=None):
    raw = data.get(field)
    if raw in (None, ""):
        values[field] = None
        return
    try:
        values[field] = dates.parse_stored(raw)
    except ValueError as exc:
        values[field] = None
        errors[field] = [str(exc)]
        return
    if not allow_future and values[field] > today:
        errors[field] = ["The date can't be in the future."]


def _obligation_fields(org, data, creating):
    errors: dict[str, list[str]] = {}
    values: dict = {}
    if "name" in data or creating:
        name = text_value(data.get("name"), "name", errors)
        if not name:
            errors.setdefault("name", ["Enter a name."])
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        values["name"] = name
    if "vendor" in data:
        values["vendor"] = _in_org(Vendor, org, data.get("vendor"), "vendor", errors)
    if "responsible" in data:
        values["responsible"] = _in_org(Person, org, data.get("responsible"), "responsible", errors, active_only=True)
        if "responsible" in errors and errors["responsible"][0].startswith("Inactive"):
            errors["responsible"] = ["Choose an active person."]
    if "followup" in data:
        raw = data.get("followup")
        user = None
        if raw not in (None, ""):
            followup_id = whole_number(raw, 1)
            user = (
                get_user_model()
                .objects.filter(
                    id=followup_id, is_active=True, membership__organization=org, membership__role__in=WRITE_ROLES
                )
                .first()
                if followup_id is not None
                else None
            )
            if user is None:
                errors["followup"] = ["Choose an Admin or Editor in this organization."]
        values["followup"] = user
    if "auto_renew" in data or creating:
        choice = data.get("auto_renew", AutoRenew.UNKNOWN)
        if choice not in AutoRenew.values:
            errors["auto_renew"] = ["Choose yes, no or unknown."]
        values["auto_renew"] = choice
    return values, errors


def _period(data, prefix, label, errors, values):
    """A number with a unit, both or neither."""
    value_key, unit_key = f"{prefix}_value", f"{prefix}_unit"
    if value_key not in data and unit_key not in data:
        return
    raw, unit = data.get(value_key), data.get(unit_key)
    unit = "" if unit is None else unit
    if not isinstance(unit, str):  # 5, false or [] is refused, not read as "no unit"
        errors[unit_key] = ["Choose days, months or years."]
        return
    if raw in (None, ""):
        values[value_key], values[unit_key] = None, ""
        return
    number = whole_number(raw)
    if number is None:
        errors[value_key] = [f"Enter the {label} as a whole number."]
        return
    if not 1 <= number <= PERIOD_MAX:
        errors[value_key] = [f"Enter a {label} between 1 and {PERIOD_MAX}."]
    if unit not in PeriodUnit.values:
        errors[unit_key] = ["Choose days, months or years."]
    values[value_key], values[unit_key] = number, unit


def _cycle_fields(org, data):
    errors: dict[str, list[str]] = {}
    values: dict = {}
    for field in ("renewal_date", "explicit_deadline"):
        if field in data:
            _date(data, field, errors, values)
    _period(data, "notice", "notice period", errors, values)
    _period(data, "term", "term", errors, values)
    if "applications" in data:
        raw = data.get("applications")
        if raw is None:
            raw = []
        if not isinstance(raw, list) or not all(is_uuid(v) for v in raw):
            errors["applications"] = ["Choose applications from the list."]
        else:
            ids = list(dict.fromkeys(str(v) for v in raw))
            found = {str(r.id): r for r in Resource.objects.filter(organization=org, kind=Kind.APPLICATION, id__in=ids)}
            if len(found) != len(ids):
                errors["applications"] = ["Choose existing applications. A renewal covers applications only."]
            else:
                values["applications"] = [found[i] for i in ids]
    return values, errors


def _set_applications(org, cycle, applications):
    snapshots.touch(cycle)  # the bulk insert below sends no signal; the cycle's snapshot lists its applications
    RenewalCycleApplication.objects.filter(cycle=cycle).exclude(application__in=applications).delete()
    have = set(RenewalCycleApplication.objects.filter(cycle=cycle).values_list("application_id", flat=True))
    RenewalCycleApplication.objects.bulk_create(
        [
            RenewalCycleApplication(organization=org, cycle=cycle, application=a)
            for a in applications
            if a.id not in have
        ]
    )


def _id(value):
    return str(value) if value else None


def obligation_audit_values(o):
    return {
        "name": o.name,
        "vendor": _id(o.vendor_id),
        "responsible": _id(o.responsible_id),
        "followup": o.followup_id,
        "auto_renew": o.auto_renew,
    }


def cycle_audit_values(c):
    return {
        "renewal_date": c.renewal_date.isoformat() if c.renewal_date else None,
        "notice": [c.notice_value, c.notice_unit] if c.notice_value else None,
        "explicit_deadline": c.explicit_deadline.isoformat() if c.explicit_deadline else None,
        "term": [c.term_value, c.term_unit] if c.term_value else None,
        "applications": sorted(str(a) for a in c.coverage.values_list("application_id", flat=True)),
    }


def _diff(before, after):
    return {k: [before[k], after[k]] for k in after if before[k] != after[k]}


def latest_cycle(obligation):
    return RenewalCycle.objects.filter(obligation=obligation).order_by("-sequence").first()


def open_cycle(obligation, cycle_id=None):
    """The open cycle (by ID when given); a completed one is history and can't change."""
    qs = RenewalCycle.objects.filter(obligation=obligation)
    cycle = qs.filter(id=cycle_id).first() if cycle_id else qs.filter(completed_on__isnull=True).first()
    if cycle is None:
        raise Conflict("This renewal has no open cycle. Establish the next cycle first.")
    if cycle.completed_on is not None:
        raise Conflict("This cycle is complete; its history can't be changed.")
    return cycle


@transaction.atomic
def create_renewal(org, actor, data, note=None):
    """An obligation and its first cycle. Missing dates or applications are allowed and show as gaps. `note` adds
    context to the event (a contract import names its run)."""
    values, errors = _obligation_fields(org, data, creating=True)
    cycle_values, cycle_errors = _cycle_fields(org, data)
    errors.update(cycle_errors)
    if errors:
        raise FieldErrors(errors)
    applications = cycle_values.pop("applications", [])
    limits.check_room(org, limits.OTHER_RECORDS)
    obligation = RenewalObligation.objects.create(organization=org, created_by=actor, **values)
    cycle = RenewalCycle.objects.create(
        organization=org, obligation=obligation, sequence=1, deadline_set_on=org_today(org), **cycle_values
    )
    _set_applications(org, cycle, applications)
    audit(
        org,
        actor,
        "renewal.created",
        obligation,
        {**obligation_audit_values(obligation), "cycle": str(cycle.id), **cycle_audit_values(cycle), **(note or {})},
    )
    return obligation


def _from_contract(name):
    return f"This renewal takes its dates and auto-renewal from the contract {name}. Change them on the contract."


def contract_owned(obligation, values, current) -> dict[str, list[str]]:
    """Field errors for `values` that would change what the obligation's driving contract owns (its deadline inputs,
    money/contracts.py): sending the value it already has is fine, so a form can send everything back."""
    from .contracts import driver

    changed = [k for k, v in values.items() if k in current and current[k] != v]
    if not changed:
        return {}
    contract = driver(obligation)
    if contract is None:
        return {}
    return {k: [_from_contract(contract.name)] for k in changed}


def lock_obligation(obligation) -> None:
    """Lock the obligation row until the transaction ends and re-read it. Every write that reads which contract drives
    the renewal (contract_owned, driver) takes this lock first, as money/contracts.sync_obligation does, so the order
    is always obligation then cycle: a contract write and a renewal write serialise instead of losing one write or
    deadlocking. Call it inside the caller's transaction (each caller is @transaction.atomic)."""
    RenewalObligation.objects.select_for_update().filter(id=obligation.id).values_list("id", flat=True).first()
    obligation.refresh_from_db()


@transaction.atomic
def update_renewal(org, actor, obligation, data):
    """The obligation's own fields. Cycle fields go through update_cycle. Auto-renewal comes from a linked contract
    while there is one."""
    lock_obligation(obligation)
    stray = [f for f in CYCLE_FIELDS if f in data]
    if stray:
        raise FieldErrors({f: ["Change cycle terms on the cycle."] for f in stray})
    values, errors = _obligation_fields(org, data, creating=False)
    if "auto_renew" in values and not errors:
        errors.update(
            contract_owned(obligation, {"auto_renew": values["auto_renew"]}, {"auto_renew": obligation.auto_renew})
        )
    if errors:
        raise FieldErrors(errors)
    before = obligation_audit_values(obligation)
    if "auto_renew" in values and values["auto_renew"] != obligation.auto_renew:
        # The deadline basis changes with auto-renewal; thresholds already passed today don't fire.
        RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).update(
            deadline_set_on=org_today(org)
        )
    for field, value in values.items():
        setattr(obligation, field, value)
    obligation.save()
    changes = _diff(before, obligation_audit_values(obligation))
    if changes:
        audit(org, actor, "renewal.updated", obligation, changes)
    return obligation


@transaction.atomic
def update_cycle(org, actor, obligation, cycle_id, data):
    """Terms and covered applications of the open cycle; earlier cycles never change."""
    lock_obligation(obligation)
    cycle = open_cycle(obligation, cycle_id)
    stray = [f for f in OBLIGATION_FIELDS if f in data]
    if stray:
        raise FieldErrors({f: ["Change this on the renewal, not the cycle."] for f in stray})
    values, errors = _cycle_fields(org, data)
    if not errors:
        errors.update(contract_owned(obligation, values, {k: getattr(cycle, k) for k in CONTRACT_INPUTS}))
    if errors:
        raise FieldErrors(errors)
    before = cycle_audit_values(cycle)
    applications = values.pop("applications", None)
    if any(getattr(cycle, f) != v for f, v in values.items() if f in DEADLINE_INPUTS):
        cycle.deadline_set_on = org_today(org)
    for field, value in values.items():
        setattr(cycle, field, value)
    cycle.save()
    if applications is not None:
        _set_applications(org, cycle, applications)
    changes = _diff(before, cycle_audit_values(cycle))
    if changes:
        audit(org, actor, "renewal_cycle.updated", cycle, {"cycle": str(cycle.id), **changes})
    return cycle


@transaction.atomic
def record_decision(org, actor, obligation, cycle_id, data, terms=None):
    """Renew, Cancel or Renegotiate, with notes; or clear it. Never completes the cycle. `terms` ({column:
    value} for TERM_COLUMNS) is what the decision changed on a contract's term, written by the contract renew action
    (money/renewal_action.py): every column is set (missing ones cleared) and the event
    carries them as `terms_change`."""
    cycle = open_cycle(obligation, cycle_id)
    errors: dict[str, list[str]] = {}
    values: dict = {}
    decision = data.get("decision") or ""
    if decision and decision not in Decision.values:
        errors["decision"] = ["Choose renew, cancel or renegotiate."]
    notes = text_value(data.get("decision_notes"), "decision_notes", errors)
    if len(notes) > TEXT_MAX:
        errors["decision_notes"] = [f"Use {TEXT_MAX} characters or fewer."]
    today = org_today(org)
    if decision:
        data = {"decided_on": today.isoformat(), **{k: v for k, v in data.items() if v not in (None, "")}}
        _date(data, "decided_on", errors, values, allow_future=False, today=today)
    if errors:
        raise FieldErrors(errors)
    before = [cycle.decision, cycle.decision_notes, cycle.decided_on.isoformat() if cycle.decided_on else None]
    cycle.decision = decision
    cycle.decision_notes = notes if decision else ""
    cycle.decided_on = values.get("decided_on") if decision else None
    if terms is not None:
        for column in TERM_COLUMNS:
            empty = "" if column.endswith(("_currency", "_billing_frequency")) else None
            setattr(cycle, column, terms.get(column, empty))
    cycle.save()
    after = [cycle.decision, cycle.decision_notes, cycle.decided_on.isoformat() if cycle.decided_on else None]
    if before != after or terms is not None:
        payload = {"cycle": str(cycle.id), "decision": [before[0], after[0]], "decided_on": after[2]}
        if terms is not None:
            from .serializers import terms_change_json

            payload["terms_change"] = terms_change_json(cycle)
        audit(org, actor, "renewal_cycle.decided", cycle, payload)
    return cycle


@transaction.atomic
def record_completion(org, actor, obligation, cycle_id, data):
    """What actually happened: a completion date and evidence. Needs a decision first; closes the
    cycle, which then stays as history. Time passing never does this."""
    cycle = open_cycle(obligation, cycle_id)
    errors: dict[str, list[str]] = {}
    values: dict = {}
    today = org_today(org)
    if not cycle.decision:
        errors["decision"] = ["Record the decision before the completion."]
    if data.get("completed_on") in (None, ""):
        errors["completed_on"] = ["Enter the completion date."]
    else:
        _date(data, "completed_on", errors, values, allow_future=False, today=today)
    evidence = text_value(data.get("completion_evidence"), "completion_evidence", errors)
    if not evidence:
        errors.setdefault("completion_evidence", ["Enter evidence or a reference, such as a PO number or an email."])
    elif len(evidence) > EVIDENCE_MAX:
        errors["completion_evidence"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    if errors:
        raise FieldErrors(errors)
    cycle.completed_on = values["completed_on"]
    cycle.completion_evidence = evidence
    cycle.save()
    audit(
        org,
        actor,
        "renewal_cycle.completed",
        cycle,
        {"cycle": str(cycle.id), "decision": cycle.decision, "completed_on": cycle.completed_on.isoformat()},
    )
    return cycle


@transaction.atomic
def establish_next_cycle(org, actor, obligation, data):
    """The next cycle, only once the latest is complete. Defaults: renewal date plus the term (clamped), and the
    same notice period, term and covered applications; anything in data overrides them."""
    lock_obligation(obligation)
    previous = latest_cycle(obligation)
    if previous is None or previous.completed_on is None:
        raise Conflict("Complete the current cycle before establishing the next one.")
    from .contracts import driver

    values, errors = _cycle_fields(org, data)
    defaults = {
        "renewal_date": renewal_rules.next_renewal_date(previous),
        "notice_value": previous.notice_value,
        "notice_unit": previous.notice_unit,
        "explicit_deadline": None,
        "term_value": previous.term_value,
        "term_unit": previous.term_unit,
    }
    contract = driver(obligation)
    if contract is not None:  # the dates come from the linked contract, so the two can't disagree
        if contract.end_date is None or (
            previous.renewal_date is not None and contract.end_date <= previous.renewal_date
        ):
            raise Conflict(
                f"Update the contract's end date first: the next cycle takes its renewal date from {contract.name}, "
                "which still ends on or before this cycle's renewal date."
            )
        owned = {
            "renewal_date": contract.end_date,
            "notice_value": contract.notice_value,
            "notice_unit": contract.notice_unit,
            "explicit_deadline": None,
        }
        errors.update(
            {k: [_from_contract(contract.name)] for k in CONTRACT_INPUTS if k in values and values[k] != owned[k]}
        )
        defaults.update(owned)
        values = {k: v for k, v in values.items() if k not in owned}
    if errors:
        raise FieldErrors(errors)
    applications = values.pop("applications", None)
    if applications is None:
        applications = list(Resource.objects.filter(renewal_coverage__cycle=previous))
    cycle = RenewalCycle.objects.create(
        organization=org,
        obligation=obligation,
        sequence=previous.sequence + 1,
        deadline_set_on=org_today(org),
        **{**defaults, **values},
    )
    _set_applications(org, cycle, applications)
    obligation.save()  # touches updated_at
    audit(
        org,
        actor,
        "renewal_cycle.established",
        cycle,
        {"cycle": str(cycle.id), "sequence": cycle.sequence, "previous": str(previous.id), **cycle_audit_values(cycle)},
    )
    return cycle


@transaction.atomic
def archive_renewal(org, actor, obligation):
    if obligation.archived_at is None:
        obligation.archived_at = timezone.now()
        obligation.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "renewal.archived", obligation, {"name": obligation.name})
    return obligation


@transaction.atomic
def restore_renewal(org, actor, obligation):
    if obligation.archived_at is not None:
        obligation.archived_at = None
        obligation.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "renewal.restored", obligation, {"name": obligation.name})
    return obligation


@transaction.atomic
def delete_renewal(org, actor, obligation):
    """Hard delete (Admin): only while no cycle has a decision or completion, since those are history worth
    keeping, and no contract takes it as its renewal; archive instead."""
    from .models import Contract

    recorded = RenewalCycle.objects.filter(obligation=obligation).exclude(decision="").count()
    refs = {"recorded decision": recorded} if recorded else {}
    contracts = Contract.objects.filter(renewal_obligation=obligation).count()
    if contracts:
        refs["linked contract"] = contracts
    refuse_while_referenced(obligation, refs, "Archive it instead.")
    audit(org, actor, "renewal.deleted", obligation, {"name": obligation.name})
    obligation.delete()


# ---- derived state, for the API, Needs attention and the digest ---------------------------------


def obligations_for(org):
    return RenewalObligation.objects.filter(organization=org).select_related(
        "vendor", "responsible__department", "followup"
    )


# What a decision on a contract's term changed (money 0005): written by the renew action.
TERM_COLUMNS = (
    "old_amount",
    "old_currency",
    "old_billing_frequency",
    "old_annualized_cost",
    "old_start_date",
    "old_end_date",
    "new_amount",
    "new_currency",
    "new_billing_frequency",
    "new_annualized_cost",
    "new_start_date",
    "new_end_date",
)
CYCLE_COLUMNS = (
    "id",
    "obligation_id",
    "sequence",
    "renewal_date",
    "notice_value",
    "notice_unit",
    "explicit_deadline",
    "term_value",
    "term_unit",
    "decision",
    "decision_notes",
    "decided_on",
    "completed_on",
    "completion_evidence",
    *TERM_COLUMNS,
)


def _load_cycles(org, obligation_ids, history):
    """{obligation id: [cycle, ...]} newest first, as plain namespaces with `.coverage` (covered applications) and
    `.reminders` (thresholds fired). Plain rows on purpose: Needs attention and the badge read every renewal on every
    page, and building model instances through prefetch_related cost about 100 ms at 300 renewals."""
    qs = RenewalCycle.objects.filter(organization=org, obligation_id__in=obligation_ids)
    if history:
        qs = qs.order_by("obligation_id", "-sequence")
    else:  # only the latest cycle of each obligation
        qs = qs.order_by("obligation_id", "-sequence").distinct("obligation_id")
    cycles = [SimpleNamespace(**dict(zip(CYCLE_COLUMNS, row, strict=True))) for row in qs.values_list(*CYCLE_COLUMNS)]
    by_id = {c.id: c for c in cycles}
    for c in cycles:
        c.coverage, c.reminders = [], []
    covered = (
        RenewalCycleApplication.objects.filter(organization=org, cycle_id__in=list(by_id))
        .order_by("application__name")
        .values_list(
            "cycle_id", "application_id", "application__name", "application__lifecycle", "application__archived_at"
        )
    )
    for cycle_id, app_id, name, lifecycle, archived_at in covered:
        by_id[cycle_id].coverage.append(
            SimpleNamespace(id=app_id, name=name, lifecycle=lifecycle, archived=archived_at is not None)
        )
    fired = ReminderDispatch.objects.filter(organization=org, cycle_id__in=list(by_id)).values_list(
        "cycle_id", "threshold_days", "created_at", "digest_id", "digest__delivery__status", "digest__for_date"
    )
    from notify.digest import status_word

    for cycle_id, days, created_at, digest_id, delivery_status, for_date in fired:
        # The digest's status as the digest log words it (notify/digest.status_word). No digest (its run was
        # removed): None, which reads "sent" on the day it fired.
        status = status_word(delivery_status) if digest_id is not None else None
        by_id[cycle_id].reminders.append(
            SimpleNamespace(threshold_days=days, created_at=created_at, status=status, for_date=for_date)
        )
    return _group(cycles)


def _group(cycles):
    grouped: dict = {}
    for c in cycles:
        grouped.setdefault(c.obligation_id, []).append(c)
    return grouped


class RenewalState:
    """An obligation with its latest cycle and everything derived from it, judged on `today` (organization-local)."""

    def __init__(self, org, obligation, cycles, today, contract=None):
        self.obligation = obligation
        self.contract = contract  # the linked contract the open cycle takes its dates from, if any
        self.cycles = cycles  # newest first; only the latest unless history was asked for
        self.cycle = cycles[0]
        self.applications = self.cycle.coverage
        c = self.cycle
        self.deadline = renewal_rules.action_deadline(
            obligation.auto_renew,
            c.renewal_date,
            c.notice_value,
            c.notice_unit,
            c.explicit_deadline,
            org.renewal_lead_days,
        )
        self.open = c.completed_on is None
        self.timing = renewal_rules.timing(self.deadline.date, today) if self.open else None
        self.gaps = renewal_rules.cycle_gaps(
            auto_renew=obligation.auto_renew,
            cycle=c,
            deadline=self.deadline,
            applications=self.applications,
            responsible=obligation.responsible,
            followup_id=obligation.followup_id,
            today=today,
        )


_QUEUE_SQL = """
SELECT o.id, o.name, o.auto_renew, o.followup_id, v.name, o.responsible_id, p.active,
       c.id, c.renewal_date, c.notice_value, c.notice_unit, c.explicit_deadline, c.decision, c.completed_on,
       (SELECT count(*) FROM {coverage} AS cov WHERE cov.cycle_id = c.id),
       (SELECT count(*) FROM {coverage} AS cov JOIN {resource} AS r ON r.id = cov.application_id
         WHERE cov.cycle_id = c.id AND r.lifecycle = 'retired')
FROM {obligation} AS o
LEFT JOIN {vendor} AS v ON v.id = o.vendor_id
LEFT JOIN {person} AS p ON p.id = o.responsible_id
JOIN LATERAL (
    SELECT * FROM {cycle} WHERE obligation_id = o.id ORDER BY sequence DESC LIMIT 1
) AS c ON TRUE
WHERE o.organization_id = %(org)s AND o.archived_at IS NULL
"""


def queue_states(org, today):
    """The states Needs attention, the badge and the digest read, for every renewal that isn't archived: each
    obligation with its latest cycle and how many applications it covers (and how many are retired), in one query.
    Every page asks for these (the badge), so they skip models and prefetching altogether."""
    from django.db import connection

    from catalog.models import Person, Resource, Vendor

    sql = _QUEUE_SQL.format(
        obligation=RenewalObligation._meta.db_table,
        cycle=RenewalCycle._meta.db_table,
        coverage=RenewalCycleApplication._meta.db_table,
        resource=Resource._meta.db_table,
        vendor=Vendor._meta.db_table,
        person=Person._meta.db_table,
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, {"org": org.id})
        rows = cursor.fetchall()
    states = []
    for (
        oid,
        name,
        auto,
        followup,
        vendor,
        responsible,
        active,
        cid,
        rdate,
        nval,
        nunit,
        explicit,
        decision,
        done,
        n,
        retired,
    ) in rows:
        o = SimpleNamespace(
            id=oid,
            name=name,
            auto_renew=auto,
            followup_id=followup,
            vendor_name=vendor,
            responsible=SimpleNamespace(active=active) if responsible else None,
        )
        apps = [SimpleNamespace(lifecycle="retired")] * retired + [SimpleNamespace(lifecycle="active")] * (n - retired)
        cycle = SimpleNamespace(
            id=cid,
            renewal_date=rdate,
            notice_value=nval,
            notice_unit=nunit,
            explicit_deadline=explicit,
            decision=decision,
            completed_on=done,
            coverage=apps,
        )
        states.append(RenewalState(org, o, [cycle], today))
    return states


def renewal_states(org, qs, today=None, history=False):
    """States for every obligation in qs, from five queries however many there are. `history` loads every cycle
    (the renewal page); otherwise only the latest (the list). Needs attention and the digest use queue_states."""
    from .contracts import drivers

    today = today or org_today(org)
    obligations = list(qs.annotate(cycle_count=Count("cycles")))
    ids = [o.id for o in obligations]
    cycles = _load_cycles(org, ids, history)
    linked = drivers(org, ids)
    return [RenewalState(org, o, cycles[o.id], today, linked.get(o.id)) for o in obligations if o.id in cycles]
