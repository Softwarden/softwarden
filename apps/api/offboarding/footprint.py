"""A Person's footprint: "Person X is leaving: what do they own, run, follow or appear in".

One section per key of references.SECTIONS, each `{key, label, available, reason, count, items}`. An item is
`{type, id, name, path, roles, archived, detail}`: `path` is the SPA page to open (null when there is none), `roles`
says how the Person appears on it (business_owner, technical_owner, run_as, member, ...). A section is unavailable,
with the reason in words, when the organisation lacks its capability, when it is for Admins only (the account and the
notification rules, which only Admins read anywhere else), when it is read through a user account the Person doesn't
have, or when Softwarden has no licence data (no Microsoft Entra licence usage connector has read the tenant's
licensed users, for licence waste).

Everything comes from Softwarden's own rows; nothing here calls Microsoft. Two sections match text rather than keys:
AI agents whose `run_as_identity` is the Person's email (or their account's) or Entra object ID, and discovery
observations whose Entra owners match the Person by object ID or email, by the same rule the candidates queue uses
(discovery.engine.suggested_owners)."""

from collections.abc import Callable
from typing import Any

from django.db.models import BooleanField, Count, Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import Lower, Trim

from api.models import ApiToken
from catalog.models import (
    KIND_PATHS,
    AIAgent,
    ChangeAssessment,
    Kind,
    LinkConfirmation,
    Person,
    QualityException,
    RecordReview,
    Resource,
    SavedFilter,
    TeamMember,
    Vendor,
)
from catalog.quality import BY_ID as QUALITY_RULES
from core.capabilities import (
    AI_AGENTS,
    CONTRACTS,
    DATA_QUALITY,
    DISCOVERY_ENTRA,
    RELATIONSHIPS,
    RENEWALS,
    REVIEWS,
    has_capability,
)
from core.graph import LOGIN_HOST as ENTRA_LOGIN_HOST
from core.models import Membership, Role, UserIdentity
from discovery.engine import suggested_owners
from discovery.models import Connector, LicensedUser, Observation, Source
from money.models import CalendarFeed, Contract, RenewalObligation
from notify.models import (
    NotificationRule,
    NotificationRuleEscalationPerson,
    NotificationRulePerson,
    SavedViewSubscription,
)

from .references import SECTIONS

# Until a Microsoft Entra licence usage connector (User.Read.All) has read the tenant's licensed users.
LICENCES_NOT_READ = "Licenses: not read; needs User.Read.All"
ADMINS_ONLY = "Only Admins see this."
NO_ACCOUNT = "They have no user account here."
CAPABILITY_FOR = {
    "licences": DISCOVERY_ENTRA,
    "ai_agents": AI_AGENTS,
    "contracts": CONTRACTS,
    "renewals": RENEWALS,
    "reviews": REVIEWS,
    "assessments": RELATIONSHIPS,
    "quality_exceptions": DATA_QUALITY,
}
ADMIN_SECTIONS = ("notification_rules", "account")
ACCOUNT_SECTIONS = ("it_steward", "assessments", "discovery", "account")  # read through the Person's user account
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def record_path(resource_id, kind) -> str | None:
    if kind == Kind.INTEGRATION:
        return f"/integrations/{resource_id}"
    return f"/{KIND_PATHS[kind]}/{resource_id}" if kind in KIND_PATHS else None


def item(type_, id_, name, path=None, roles=(), archived=False, detail="") -> dict[str, Any]:
    return {
        "type": type_,
        "id": str(id_) if id_ is not None else None,
        "name": str(name),
        "path": path,
        "roles": list(roles),
        "archived": bool(archived),
        "detail": detail,
    }


def owner_roles(resource, person) -> list[str]:
    return [f for f in ("business_owner", "technical_owner") if getattr(resource, f"{f}_id") == person.id]


def identities(person) -> tuple[set[str], set[str]]:
    """(emails, Entra object IDs) the Person is known by, lowercased: their own email and their account's, and the
    object IDs of the account's Entra sign-in identities."""
    emails = {e.strip().lower() for e in (person.email, getattr(person.user, "email", "")) if e and e.strip()}
    object_ids: set[str] = set()
    if person.user_id is not None:
        object_ids = {
            s.lower()
            for s in UserIdentity.objects.filter(
                user_id=person.user_id, issuer__startswith=f"https://{ENTRA_LOGIN_HOST}/"
            ).values_list("subject", flat=True)
        }
    return emails, object_ids


# ---- sections -------------------------------------------------------------------------------------------------------


def hidden_kinds(org) -> list[str]:
    """Record kinds the organisation lacks the capability for: their rows stay out of every section."""
    return [] if has_capability(org, AI_AGENTS) else [Kind.AI_AGENT]


def _owned(org, person, kinds) -> list[dict[str, Any]]:
    rows = Resource.objects.filter(organization=org, kind__in=kinds).filter(
        Q(business_owner=person) | Q(technical_owner=person)
    )
    return [
        item(r.kind, r.id, r.name, record_path(r.id, r.kind), owner_roles(r, person), r.archived_at is not None)
        for r in rows.order_by("name", "id")
    ]


def records(org, person, **_):
    return _owned(org, person, [Kind.APPLICATION, Kind.COMPONENT])


def integrations(org, person, **_):
    return _owned(org, person, [Kind.INTEGRATION])


def ai_agents(org, person, emails, object_ids, **_):
    """Agents they own, and agents whose run-as identity is one of their emails or Entra object IDs."""
    names = emails | object_ids
    found: dict[Any, dict[str, Any]] = {r["id"]: r for r in _owned(org, person, [Kind.AI_AGENT])}
    if names:
        run_as = (
            AIAgent.objects.filter(organization=org)
            .annotate(identity=Lower(Trim("run_as_identity")))
            .filter(identity__in=names)
            .select_related("resource")
            .order_by("resource__name", "resource_id")
        )
        for agent in run_as:
            r = agent.resource
            row = found.setdefault(
                str(r.id), item(r.kind, r.id, r.name, record_path(r.id, r.kind), [], r.archived_at is not None)
            )
            row["roles"].append("run_as")
            row["detail"] = f"Runs as {agent.run_as_identity.strip()}"
    return sorted(found.values(), key=lambda r: (r["name"].lower(), r["id"]))


def it_steward(org, person, **_):
    if person.user_id is None:
        return []
    rows = Resource.objects.filter(organization=org, it_steward_id=person.user_id).exclude(kind__in=hidden_kinds(org))
    rows = rows.order_by("name", "id")
    return [
        item(r.kind, r.id, r.name, record_path(r.id, r.kind), ["it_steward"], r.archived_at is not None) for r in rows
    ]


def teams(org, person, **_):
    links = TeamMember.objects.filter(organization=org, person=person).select_related("team").order_by("team__name")
    return [item("team", m.team_id, m.team.name, "/admin/people", ["member"]) for m in links]


def vendors(org, person, **_):
    rows = Vendor.objects.filter(organization=org, account_manager=person).order_by("name", "id")
    return [item("vendor", v.id, v.name, f"/vendors/{v.id}", ["account_manager"]) for v in rows]


def contracts(org, person, **_):
    rows = (
        Contract.objects.filter(organization=org, renewal_owner=person)
        .annotate(pools=Count("license_pools"))
        .order_by("name", "id")
    )
    out = []
    for c in rows:
        pools = f"; {c.pools} license pool{'' if c.pools == 1 else 's'}" if c.pools else ""
        detail = f"{c.get_status_display()}{pools}"
        out.append(
            item("contract", c.id, c.name, f"/contracts/{c.id}", ["renewal_owner"], c.archived_at is not None, detail)
        )
    return out


def renewals(org, person, **_):
    match = Q(responsible=person)
    if person.user_id is not None:
        match |= Q(followup_id=person.user_id)
    out = []
    for o in RenewalObligation.objects.filter(organization=org).filter(match).order_by("name", "id"):
        roles = ["responsible"] if o.responsible_id == person.id else []
        if person.user_id is not None and o.followup_id == person.user_id:
            roles.append("followup")
        out.append(item("renewal", o.id, o.name, f"/renewals/{o.id}", roles, o.archived_at is not None))
    return out


def reviews(org, person, **_):
    """The records and links whose review or confirmation they gave, the latest one each."""
    out: dict[tuple[str, Any], dict[str, Any]] = {}
    hidden = hidden_kinds(org)
    for rv in (
        RecordReview.objects.filter(organization=org, confirmed_by=person)
        .exclude(resource__kind__in=hidden)
        .select_related("resource")
        .order_by("-confirmation_date", "-created_at")
    ):
        r = rv.resource
        out.setdefault(
            ("record", r.id),
            item(
                r.kind,
                r.id,
                r.name,
                record_path(r.id, r.kind),
                ["confirmed_review"],
                r.archived_at is not None,
                f"Confirmed {rv.confirmation_date.isoformat()}; next review {rv.next_review_date.isoformat()}",
            ),
        )
    for lc in (
        LinkConfirmation.objects.filter(organization=org, confirmed_by=person)
        .exclude(relationship__source__kind__in=hidden)
        .exclude(relationship__target__kind__in=hidden)
        .select_related("relationship__source", "relationship__target", "relationship__type", "integration__resource")
        .order_by("-confirmed_on", "-created_at")
    ):
        detail = f"Confirmed {lc.confirmed_on.isoformat()}"
        if lc.integration_id is not None and lc.integration is not None:
            r = lc.integration.resource
            out.setdefault(
                ("integration", r.id),
                item(r.kind, r.id, r.name, record_path(r.id, r.kind), ["confirmed_link"], False, detail),
            )
        elif lc.relationship is not None:
            rel = lc.relationship
            name = f"{rel.source.name} {rel.type.name.lower()} {rel.target.name}"
            out.setdefault(
                ("relationship", rel.id),
                item(
                    "relationship",
                    rel.id,
                    name,
                    record_path(rel.source_id, rel.source.kind),
                    ["confirmed_link"],
                    False,
                    detail,
                ),
            )
    return list(out.values())


def assessments(org, person, **_):
    if person.user_id is None:
        return []
    rows = ChangeAssessment.objects.filter(organization=org, owner_id=person.user_id).order_by("-updated_at", "id")
    return [
        item("assessment", a.id, a.title, f"/assessments/{a.id}", ["owner"], False, a.get_status_display())
        for a in rows
    ]


def quality_exceptions(org, person, **_):
    rows = (
        QualityException.objects.filter(organization=org, owner=person)
        .exclude(resource__kind__in=hidden_kinds(org))
        .select_related("resource")
        .order_by("expires_on", "id")
    )
    out = []
    for e in rows:
        rule = QUALITY_RULES.get(e.rule)
        label = rule.label if rule else e.rule
        out.append(
            item(
                "quality_exception",
                e.id,
                f"{e.resource.name}: {label}",
                record_path(e.resource_id, e.resource.kind),
                ["accountable"],
                e.resource.archived_at is not None,
                f"Until {e.expires_on.isoformat()}",
            )
        )
    return out


def notification_rules(org, person, **_):
    """Rules naming them, with how: two reads of the through tables for this person, then the rules."""
    audience = set(
        NotificationRulePerson.objects.filter(organization=org, person=person).values_list("rule_id", flat=True)
    )
    escalation = set(
        NotificationRuleEscalationPerson.objects.filter(organization=org, person=person).values_list(
            "rule_id", flat=True
        )
    )
    rows = NotificationRule.objects.filter(organization=org).filter(
        Q(fallback_person=person) | Q(id__in=audience | escalation)
    )
    out = []
    for rule in rows.order_by("name", "id"):
        roles = []
        if rule.id in audience:
            roles.append("audience")
        if rule.id in escalation:
            roles.append("escalation")
        if rule.fallback_person_id == person.id:
            roles.append("fallback")
        detail = "" if rule.enabled else "Off"
        out.append(
            item("notification_rule", rule.id, rule.name, f"/admin/notification-rules/{rule.id}", roles, False, detail)
        )
    return out


def discovery(org, person, **_):
    if person.user_id is None:
        return []
    rows = Connector.objects.filter(organization=org, reviewer_id=person.user_id).order_by("name", "id")
    return [
        item(
            "connector",
            c.id,
            c.name,
            f"/discovery/connectors/{c.id}",
            ["reviewer"],
            False,
            "" if c.enabled else "Turned off",
        )
        for c in rows
    ]


def entra_owner_rows(org, observations, person) -> list[tuple[Any, dict[str, Any]]]:
    """(observation, owner row) for each owner of these observations matched to `person`."""
    matched = suggested_owners(org, observations)
    return [
        (obs, row)
        for obs in observations
        for row in matched.get(obs.id, [])
        if row["person"] is not None and row["person"]["id"] == str(person.id)
    ]


# An owner of the observation whose email or object ID is one of the person's, compared lower-case in the database, so
# only candidate observations are read; suggested_owners then confirms the match with its own rule. Fixed SQL, every
# value bound.
OWNER_MATCH = (
    "EXISTS (SELECT 1 FROM jsonb_array_elements(catalog_observation.data -> 'owners') AS o "
    "WHERE lower(o ->> 'email') = ANY(%s) OR lower(o ->> 'external_id') = ANY(%s))"
)


def observations_owned_by(org, emails: set[str], object_ids: set[str]):
    """The organisation's Entra observations listing an owner with one of these emails or object IDs."""
    return list(
        Observation.objects.filter(organization=org, connector__source=Source.ENTRA, data__has_key="owners")
        .annotate(owned=RawSQL(OWNER_MATCH, (sorted(emails), sorted(object_ids)), output_field=BooleanField()))  # noqa: S611
        .filter(owned=True)
        .select_related("connector", "decision", "decision__resource")
        .order_by("display_name", "id")
    )


def entra_owners(org, person, emails, object_ids, **_):
    if not (emails or object_ids):
        return []
    out = []
    hidden = hidden_kinds(org)
    for obs, row in entra_owner_rows(org, observations_owned_by(org, emails, object_ids), person):
        decision = getattr(obs, "decision", None)
        linked = decision.resource if decision is not None and decision.resource_id is not None else None
        if linked is not None and linked.kind in hidden:
            linked = None  # an AI agent the organisation lacks the capability for: not named
        how = "Entra object ID" if row["external_id"] and row["external_id"] in object_ids else "email"
        detail = f"Owner in {obs.connector.name}, matched by {how}"
        if linked is not None:
            detail += f"; linked to {linked.name}"
        if obs.gone_at is not None:
            detail += "; no longer seen in the source"
        out.append(
            item(
                "observation",
                obs.id,
                obs.display_name,
                record_path(linked.id, linked.kind) if linked is not None else "/discovery",
                ["source_owner"],
                linked is not None and linked.archived_at is not None,
                detail,
            )
        )
    return out


def account(org, person, **_):
    """Their membership, whether they are the IT coordinator, live API tokens and calendar feeds, the saved views they
    own and their saved-view subscriptions."""
    if person.user_id is None:
        return []
    user = person.user
    membership = Membership.objects.filter(organization=org, user_id=user.id).first()
    out = []
    if membership is not None:
        status = "active" if user.is_active else "deactivated"
        out.append(
            item("membership", membership.id, user.email, "/admin/users", [membership.role], False, f"Account {status}")
        )
    if org.coordinator_id == user.id:
        out.append(item("coordinator", org.id, org.name, "/admin/organization", ["coordinator"]))
    for t in ApiToken.objects.filter(organization=org, user_id=user.id, revoked_at=None).order_by("name", "id"):
        access = "read-only" if t.read_only else "read and write"
        detail = f"{t.prefix}…, {access}, expires {t.expires_on.isoformat()}"
        out.append(item("api_token", t.id, t.name, "/admin/tokens", ["owner"], False, detail))
    for f in CalendarFeed.objects.filter(organization=org, user_id=user.id, revoked_at=None):
        out.append(item("calendar_feed", f.id, "Renewals calendar feed", None, ["owner"], False, f"{f.prefix}…"))
    for v in SavedFilter.objects.filter(organization=org, user_id=user.id).order_by("name", "id"):
        out.append(item("saved_view", v.id, v.name, None, ["owner"], False, "Shared" if v.shared else "Private"))
    if membership is not None:
        for s in (
            SavedViewSubscription.objects.filter(organization=org, membership=membership)
            .select_related("saved_view")
            .order_by("weekday", "id")
        ):
            name = s.saved_view.name if s.saved_view is not None else "A deleted saved view"
            detail = f"Weekly on {WEEKDAYS[s.weekday]}" + ("" if s.active else ", paused")
            out.append(item("subscription", s.id, name, None, ["subscriber"], False, detail))
    return out


def licences(org, person, **_):
    """The licences the Person holds in a tenant a licence usage connector reads: one item per SKU, named
    by its part number (from the Entra licences source, else a bound pool's SKU name). The person is linked at each
    run by their Entra sign-in identity or email (discovery/entra_licence_usage.link_people). No sign-in date here:
    that is the Admin-only licence waste drill-down's."""
    from discovery import licence_waste

    rows = list(
        LicensedUser.objects.filter(organization=org, person=person, connector__enabled=True)
        .select_related("connector")
        .order_by("connector__name", "user_key")
    )
    if not rows:
        return []
    names = {sku: v["part"] for sku, v in licence_waste.observed(org).items()}
    for sku, pools in licence_waste.bound_pools(org).items():
        names.setdefault(sku, pools[0].sku_name)
    out = []
    for row in rows:
        detail = f"In {row.connector.name}" + ("; the account is disabled" if not row.account_enabled else "")
        for sku in row.sku_ids:
            out.append(item("licence", sku, names.get(sku, sku), None, ["assigned"], False, detail))
    out.sort(key=lambda i: (i["name"].lower(), i["id"], i["detail"]))
    return out


BUILDERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "records": records,
    "ai_agents": ai_agents,
    "integrations": integrations,
    "it_steward": it_steward,
    "teams": teams,
    "vendors": vendors,
    "contracts": contracts,
    "renewals": renewals,
    "reviews": reviews,
    "assessments": assessments,
    "quality_exceptions": quality_exceptions,
    "notification_rules": notification_rules,
    "discovery": discovery,
    "entra_owners": entra_owners,
    "licences": licences,
    "account": account,
}
assert set(BUILDERS) == set(SECTIONS), "every section has a builder"


def person_json(org, person, admin: bool) -> dict[str, Any]:
    """The person, and for Admins their user account (members and roles are an Admin's page)."""
    user = person.user
    account_json = None
    if user is not None and admin:
        membership = Membership.objects.filter(organization=org, user_id=user.id).first()
        account_json = {
            "id": user.id,
            "email": user.email,
            "active": bool(user.is_active),
            "role": membership.role if membership is not None else None,
        }
    return {
        "id": str(person.id),
        "name": person.name,
        "email": person.email,
        "active": person.active,
        "anonymized": person.anonymized_at is not None,
        "account": account_json,
    }


def build(org, person: Person, role: str) -> dict[str, Any]:
    """The footprint as the requesting member (`role`) may see it."""
    emails, object_ids = identities(person)
    # Licences are read once a licence usage connector stored the tenant's licensed users (licence waste).
    licences_read = LicensedUser.objects.filter(organization=org, connector__enabled=True).exists()
    sections = []
    for key, label in SECTIONS.items():
        reason = ""
        if key == "licences" and not licences_read:
            reason = LICENCES_NOT_READ
        elif key in ADMIN_SECTIONS and role != Role.ADMIN:
            reason = ADMINS_ONLY
        elif key in CAPABILITY_FOR and not has_capability(org, CAPABILITY_FOR[key]):
            reason = f"Needs the {CAPABILITY_FOR[key]} capability."
        elif key in ACCOUNT_SECTIONS and person.user_id is None:
            reason = NO_ACCOUNT
        items = [] if reason else BUILDERS[key](org, person, emails=emails, object_ids=object_ids)
        sections.append(
            {
                "key": key,
                "label": label,
                "available": not reason,
                "reason": reason,
                "count": len(items),
                "items": items,
            }
        )
    return {
        "person": person_json(org, person, role == Role.ADMIN),
        "total": sum(s["count"] for s in sections),
        "sections": sections,
    }
