"""Hand a leaving Person's responsibilities to someone else (offboarding).

`POST /people/{id}/reassign {to, roles, dry_run}` moves the selected roles from the Person to `to`, each row through
its own service function (catalog.services.update_resource, admin_services.update_named, money.renewals
.update_renewal, money.contracts.update_contract, catalog.quality.update_exception, notify.rule_services.update_rule),
so every row gets its single-edit validation and its own audit event. Every event written meanwhile carries
`via: offboarding` and `offboarding_person` (core.request_context.acting_via), and one `person.reassigned` event on the
leaving Person sums it up.

Like bulk edit (catalog/bulk.py): all or nothing in one transaction. A row whose service refuses the change refuses the
whole request (ReassignRefused, naming each row and why); nothing is saved. There is no If-Match (a bulk action has no
one revision to compare); instead the rows are locked before they are read, so a concurrent single edit waits rather
than being overwritten from a stale read. Locks, in this order, each table's rows in ID order:

1. both People, FOR NO KEY UPDATE: it conflicts with update_person and anonymize_person (FOR UPDATE), so the target
   can't be deactivated halfway, but not with the FOR KEY SHARE a concurrent write takes to check a foreign key to
   them (a PATCH setting someone as owner), which plain FOR UPDATE would block into a deadlock;
2. records, vendors, contracts, then renewals, then exceptions and rules, each FOR NO KEY UPDATE for the same reason
   (rows that reference them can still be inserted). Contracts come before renewals because update_contract and a
   contract's If-Match lock the contract and then its renewal (money/contracts.py): the same order, so no cycle.
   The renewals the contracts are linked to are locked with the matched ones, in the same statement, so
   update_contract never takes a renewal lock later, out of order.

A dry run runs the same service calls in a savepoint that is rolled back, so it reports the rows the real run would
refuse (status `refused`, with the errors) and writes nothing.

Never deletes: team memberships aren't moved (a membership is removed by editing the team); archived records,
renewals and contracts keep their owners (skipped, and listed with the reason); someone is only ever replaced on a row.
The target must be an active Person of the same organisation, not the leaving one; another organisation's ID is simply
unknown. Roles held through a user account (IT steward, renewal follow-up) need the target's account to be an
active Admin or Editor here. Notification rules are Admin-only, as their own edits are."""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from django.db import transaction
from django.db.models import Q
from rest_framework import exceptions

from catalog import admin_services, quality, services
from catalog.models import Kind, Person, QualityException, Resource, Vendor
from core.capabilities import AI_AGENTS, CONTRACTS, DATA_QUALITY, RENEWALS, has_capability, require_capability
from core.models import Membership, Role
from core.problems import Conflict, FieldErrors
from core.request_context import acting_via
from money import contracts as money_contracts
from money import renewals as money_renewals
from money.models import Contract, RenewalObligation
from notify import rule_services
from notify.models import NotificationRule

from .footprint import record_path

VIA = "offboarding"


@dataclass(frozen=True)
class RoleSpec:
    label: str
    capability: str | None = None
    account: bool = False  # held through the Person's user account
    admin: bool = False


ROLES: dict[str, RoleSpec] = {
    "business_owner": RoleSpec("Business owner of applications, components and AI agents"),
    "technical_owner": RoleSpec("Technical owner of applications, components, AI agents and integrations"),
    "it_steward": RoleSpec("IT steward of records", account=True),
    "vendor_account_manager": RoleSpec("Account manager of vendors"),
    "renewal_responsible": RoleSpec("Responsible for renewals", capability=RENEWALS),
    "renewal_followup": RoleSpec("Follow-up on renewals", capability=RENEWALS, account=True),
    "contract_renewal_owner": RoleSpec("Renewal owner of contracts", capability=CONTRACTS),
    "quality_exception_owner": RoleSpec("Accountable for data-quality exceptions", capability=DATA_QUALITY),
    "notification_rules": RoleSpec("Named on notification rules (audience, escalation, fallback)", admin=True),
}
ROLE_NAMES = tuple(ROLES)  # the API's enum (settings ENUM_NAME_OVERRIDES ReassignRoleEnum)
ROW_STATUSES = ("changed", "planned", "skipped", "refused")  # ReassignRowStatusEnum
ARCHIVED = "Archived, so it keeps its owners. Restore it to change them."
STALE = "/problems/reassign-stale"


class ReassignRefused(FieldErrors):
    """A row's own service refused the change. Nothing was saved. `rows` says which and why."""

    def __init__(self, rows):
        super().__init__({"roles": ["Some rows can't take this change. Nothing was saved."]})
        self.extra = {"rows": rows}


@dataclass
class Row:
    type: str
    id: Any
    name: str
    path: str | None
    roles: list[str] = field(default_factory=list)
    apply: Callable[[], Any] | None = None
    reason: str = ""  # skipped: why
    refusal: dict[str, Any] | None = None  # refused: {errors, field_errors}

    def json(self, status: str) -> dict[str, Any]:
        out = {
            "type": self.type,
            "id": str(self.id),
            "name": self.name,
            "path": self.path,
            "roles": self.roles,
            "status": status,
            "reason": self.reason,
        }
        return {**out, **self.refusal} if self.refusal is not None else out


# ---- the request ----------------------------------------------------------------------------------------------------


def parse(org, actor_role: str, data: dict[str, Any]) -> tuple[str, list[str], bool, str | None]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(data) - {"to", "roles", "dry_run", "plan_hash"})
    for key in unknown:
        errors[key] = ["This field isn't accepted here."]
    to = data.get("to")
    if not isinstance(to, str) or not services.is_uuid(to):
        errors["to"] = ["Choose the person who takes over."]
    else:
        to = str(uuid.UUID(to))  # canonical: upper case and braces name the same person
    raw = data.get("roles")
    roles: list[str] = []
    if not isinstance(raw, list) or not raw or not all(isinstance(r, str) for r in raw):
        errors["roles"] = [f"Choose at least one of: {', '.join(ROLES)}."]
    else:
        roles = list(dict.fromkeys(raw))
        bad = [r for r in roles if r not in ROLES]
        if bad:
            errors["roles"] = [f"Unknown role {', '.join(bad)}. Choose from: {', '.join(ROLES)}."]
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        errors["dry_run"] = ["Send true or false."]
    expected = data.get("plan_hash")
    if expected is not None and (not isinstance(expected, str) or dry_run is True):
        errors["plan_hash"] = ["Send the plan_hash of the dry run you reviewed, with a real run only."]
    if errors:
        raise FieldErrors(errors)
    for role in roles:
        spec = ROLES[role]
        if spec.admin and actor_role != Role.ADMIN:
            raise exceptions.PermissionDenied(f"Only Admins can change notification rules ({role}).")
        if spec.capability:
            require_capability(org, spec.capability)
    return str(to), roles, dry_run, (expected.strip() or None) if isinstance(expected, str) else None


def _lock_people(org, source: Person, target_id: str) -> tuple[Person, Person]:
    """Both People locked in ID order, FOR NO KEY UPDATE (see the module docstring); the target as it is now."""
    locked = {
        str(p.id): p
        for p in Person.objects.select_for_update(no_key=True, of=("self",))
        .select_related("user")
        .filter(organization=org, id__in=[source.id, target_id])
        .order_by("id")
    }
    target = locked.get(target_id)
    if target is None:
        raise FieldErrors({"to": ["Choose an existing person."]})
    if target.id == source.id:
        raise FieldErrors({"to": ["Choose someone other than the person leaving."]})
    if not target.active or target.anonymized_at is not None:
        raise FieldErrors({"to": ["Choose an active person; inactive people can't be owners."]})
    return locked[str(source.id)], target


def _writer_account(org, person: Person):
    """The Person's user account if it is an active Admin or Editor of org, else None."""
    if person.user is None or not person.user.is_active:
        return None
    member = Membership.objects.filter(organization=org, user=person.user, role__in=services.WRITE_ROLES)
    return person.user if member.exists() else None


# ---- the plan -------------------------------------------------------------------------------------------------------


def _resources(org, actor, source, target, target_user, roles) -> list[Row]:
    match = Q()
    if "business_owner" in roles:
        match |= Q(business_owner=source) & ~Q(kind=Kind.INTEGRATION)
    if "technical_owner" in roles:
        match |= Q(technical_owner=source)
    if "it_steward" in roles and source.user_id is not None:
        match |= Q(it_steward_id=source.user_id)
    if not match:
        return []
    qs = (
        Resource.objects.filter(organization=org)
        .filter(match)
        .select_for_update(no_key=True, of=("self",))
        .order_by("id")
    )
    if not has_capability(org, AI_AGENTS):
        qs = qs.exclude(kind=Kind.AI_AGENT)  # without the capability its agents aren't shown or changed
    rows = []
    for r in sorted(qs, key=lambda r: (r.name.lower(), str(r.id))):
        row = Row(r.kind, r.id, r.name, record_path(r.id, r.kind))
        values: dict[str, Any] = {}
        if "business_owner" in roles and r.business_owner_id == source.id and r.kind != Kind.INTEGRATION:
            values["business_owner"] = str(target.id)
            row.roles.append("business_owner")
        if "technical_owner" in roles and r.technical_owner_id == source.id:
            values["technical_owner"] = str(target.id)
            row.roles.append("technical_owner")
        if "it_steward" in roles and source.user_id is not None and r.it_steward_id == source.user_id:
            values["it_steward"] = target_user.id if target_user is not None else None
            row.roles.append("it_steward")
        if r.archived_at is not None:
            row.reason = ARCHIVED
        else:
            row.apply = partial(services.update_resource, org, actor, r, values)
        rows.append(row)
    return rows


def _vendors(org, actor, source, target, roles) -> list[Row]:
    if "vendor_account_manager" not in roles:
        return []
    qs = Vendor.objects.select_for_update(no_key=True).filter(organization=org, account_manager=source).order_by("id")
    return [
        Row(
            "vendor",
            v.id,
            v.name,
            f"/vendors/{v.id}",
            ["vendor_account_manager"],
            partial(admin_services.update_named, Vendor, org, actor, v, {"account_manager": str(target.id)}),
        )
        for v in sorted(qs, key=lambda v: v.name.lower())
    ]


def _renewals(org, actor, source, target, target_user, roles, linked: set[Any]) -> list[Row]:
    """The matched renewals as rows. `linked`: the renewals the locked contracts point at, which update_contract locks
    too; they are locked here with the matched ones, in one statement in ID order, so no renewal is locked later, out of
    order (two offboardings whose contracts link to each other's renewals would otherwise deadlock). Only matched
    renewals become rows; the others are only locked."""
    match = Q()
    if "renewal_responsible" in roles:
        match |= Q(responsible=source)
    if "renewal_followup" in roles and source.user_id is not None:
        match |= Q(followup_id=source.user_id)
    linked = linked - {None}
    if not match and not linked:
        return []
    wanted = (match | Q(id__in=linked)) if match else Q(id__in=linked)
    locked = list(
        RenewalObligation.objects.select_for_update(no_key=True).filter(organization=org).filter(wanted).order_by("id")
    )
    if not match:
        return []
    rows = []
    for o in sorted(locked, key=lambda o: o.name.lower()):
        row = Row("renewal", o.id, o.name, f"/renewals/{o.id}")
        values: dict[str, Any] = {}
        if "renewal_responsible" in roles and o.responsible_id == source.id:
            values["responsible"] = str(target.id)
            row.roles.append("renewal_responsible")
        if "renewal_followup" in roles and source.user_id is not None and o.followup_id == source.user_id:
            values["followup"] = target_user.id if target_user is not None else None
            row.roles.append("renewal_followup")
        if not row.roles:
            continue  # only linked to a contract being reassigned: locked, not changed
        if o.archived_at is not None:
            row.reason = ARCHIVED
        else:
            row.apply = partial(money_renewals.update_renewal, org, actor, o, values)
        rows.append(row)
    return rows


def _contracts(org, actor, source, target, roles) -> tuple[list[Row], set[Any]]:
    """The contracts as rows, and the renewals the live ones are linked to (update_contract locks those)."""
    if "contract_renewal_owner" not in roles:
        return [], set()
    rows = []
    qs = list(
        Contract.objects.select_for_update(no_key=True).filter(organization=org, renewal_owner=source).order_by("id")
    )
    linked = {c.renewal_obligation_id for c in qs if c.archived_at is None}
    for c in sorted(qs, key=lambda c: c.name.lower()):
        row = Row("contract", c.id, c.name, f"/contracts/{c.id}", ["contract_renewal_owner"])
        if c.archived_at is not None:
            row.reason = ARCHIVED
        else:
            row.apply = partial(money_contracts.update_contract, org, actor, c, {"renewal_owner": str(target.id)})
        rows.append(row)
    return rows, linked


def _exceptions(org, actor, source, target, roles) -> list[Row]:
    if "quality_exception_owner" not in roles:
        return []
    qs = (
        QualityException.objects.select_for_update(no_key=True, of=("self",))
        .filter(organization=org, owner=source)
        .exclude(resource__kind__in=[] if has_capability(org, AI_AGENTS) else [Kind.AI_AGENT])
        .select_related("resource")
        .order_by("id")
    )
    return [
        Row(
            "quality_exception",
            e.id,
            f"{e.resource.name}: {quality.BY_ID[e.rule].label if e.rule in quality.BY_ID else e.rule}",
            record_path(e.resource_id, e.resource.kind),
            ["quality_exception_owner"],
            partial(quality.update_exception, org, actor, e, {"owner": str(target.id)}),
        )
        for e in sorted(qs, key=lambda e: (e.resource.name.lower(), e.rule))
    ]


def _swap(ids: list[Any], source, target) -> list[str]:
    """The list with the source replaced by the target in place (once, if the target was already in it)."""
    out: list[str] = []
    for pk in ids:
        value = str(target.id) if pk == source.id else str(pk)
        if value not in out:
            out.append(value)
    return out


def _rules(org, actor, source, target, roles) -> list[Row]:
    if "notification_rules" not in roles:
        return []
    qs = (
        NotificationRule.objects.select_for_update(no_key=True)
        .filter(organization=org, id__in=_rule_ids(org, source))
        .order_by("id")
    )
    rows = []
    for rule in sorted(qs, key=lambda r: r.name.lower()):
        data: dict[str, Any] = {}
        for name in ("people", "escalation_people"):
            ids = list(getattr(rule, name).order_by("name", "id").values_list("id", flat=True))
            if source.id in ids:
                data[name] = _swap(ids, source, target)
        if rule.fallback_person_id == source.id:
            data["fallback_person"] = str(target.id)
        rows.append(
            Row(
                "notification_rule",
                rule.id,
                rule.name,
                f"/admin/notification-rules/{rule.id}",
                ["notification_rules"],
                partial(rule_services.update_rule, org, actor, rule, data),
            )
        )
    return rows


def _rule_ids(org, source) -> list[Any]:
    match = Q(fallback_person=source) | Q(people=source) | Q(escalation_people=source)
    return list(NotificationRule.objects.filter(organization=org).filter(match).values_list("id", flat=True).distinct())


def plan(org, actor, source, target, roles) -> list[Row]:
    target_user = None
    if any(ROLES[r].account for r in roles) and source.user_id is not None:
        target_user = _writer_account(org, target)
    resources = _resources(org, actor, source, target, target_user, roles)
    vendors = _vendors(org, actor, source, target, roles)
    contracts, linked = _contracts(org, actor, source, target, roles)  # before renewals: update_contract's lock order
    rows = [
        *resources,
        *vendors,
        *contracts,
        *_renewals(org, actor, source, target, target_user, roles, linked),
        *_exceptions(org, actor, source, target, roles),
        *_rules(org, actor, source, target, roles),
    ]
    account_rows = [r for r in rows if r.apply and {"it_steward", "renewal_followup"} & set(r.roles)]
    if account_rows and target_user is None:
        raise FieldErrors(
            {
                "to": [
                    f"{target.name} has no active Admin or Editor account here, so they can't be IT steward or follow "
                    "up renewals. Leave those roles out, or link them to an account first."
                ]
            }
        )
    return rows


# ---- apply ----------------------------------------------------------------------------------------------------------


def _summary(source, target, roles, rows, dry_run) -> dict[str, Any]:
    def status(r: Row) -> str:
        if r.apply is None:
            return "skipped"
        if r.refusal is not None:
            return "refused"
        return "planned" if dry_run else "changed"

    return {
        "person": {"id": str(source.id), "name": source.name},
        "to": {"id": str(target.id), "name": target.name},
        "roles": roles,
        "dry_run": dry_run,
        "changed": sum(1 for r in rows if status(r) in ("planned", "changed")),
        "skipped": sum(1 for r in rows if r.apply is None),
        "refused": sum(1 for r in rows if r.refusal is not None),
        "rows": [r.json(status(r)) for r in rows],
    }


def plan_hash(org, source, target, roles, rows) -> str:
    """SHA-256 of what a run would change: the organisation, both people, the roles asked for, and every row that would
    change as (type, id, role), sorted. A real run sent with a dry run's hash refuses (409 reassign-stale) when the plan
    it computes under its locks differs, so it never changes rows the preview didn't show."""
    changing = sorted((r.type, str(r.id), role) for r in rows if r.apply is not None for role in r.roles)
    payload = {
        "org": str(org.id),
        "person": str(source.id),
        "to": str(target.id),
        "roles": sorted(roles),
        "rows": changing,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ReassignStale(Conflict):
    def __init__(self):
        super().__init__(
            "The records changed since the preview, so nothing was saved. Preview again.", extra={"type": STALE}
        )


class _DryRun(Exception):  # not an error: raised to roll the dry run's savepoint back
    pass


def _apply(rows: list[Row]) -> list[Row]:
    """Run each row's service, recording a refusal on the row; returns the refused rows."""
    for row in rows:
        if row.apply is None:
            continue
        try:
            # Each service is atomic itself: a refused row rolls back to its savepoint and the loop goes on collecting
            # refusals; the caller then rolls the whole transaction (or the dry run's savepoint) back.
            row.apply()
        except FieldErrors as e:
            row.refusal = {"errors": [], "field_errors": e.errors}
        except Conflict as e:
            row.refusal = {"errors": [str(e.detail)], "field_errors": {}}
    return [r for r in rows if r.refusal is not None]


def reassign(org, actor, actor_role: str, source: Person, data: dict[str, Any]) -> dict[str, Any]:
    """Reassign; with `dry_run`, run the same service calls in a savepoint that is rolled back and say what would
    change and what would be refused (nothing is written)."""
    target_id, roles, dry_run, expected = parse(org, actor_role, data)
    with transaction.atomic():
        source, target = _lock_people(org, source, target_id)
        rows = plan(org, actor, source, target, roles)
        digest = plan_hash(org, source, target, roles, rows)
        if expected is not None and expected != digest:
            raise ReassignStale
        with acting_via(via=VIA, offboarding_person=str(source.id)):
            if dry_run:
                try:
                    with transaction.atomic():
                        _apply(rows)
                        raise _DryRun
                except _DryRun:
                    pass
                return {**_summary(source, target, roles, rows, dry_run), "plan_hash": digest}
            refused = _apply(rows)
            if refused:
                raise ReassignRefused([r.json("refused") for r in refused])
            changed = sum(1 for r in rows if r.apply)
            if changed:  # nothing to change (every row skipped, or none found): no event
                services.audit(
                    org,
                    actor,
                    "person.reassigned",
                    source,
                    {"reassigned_to": str(target.id), "roles": roles, "changed": changed},
                )
        return {**_summary(source, target, roles, rows, dry_run), "plan_hash": digest}
