"""Writes for accounts, people, reference data and organization settings (E2, E3)."""

import secrets
from collections.abc import Callable
from datetime import time
from typing import Any
from zoneinfo import available_timezones

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from core import limits, sessions
from core.capabilities import COMPLIANCE_PROFILE, SCIM, SSO_OIDC, require_capability
from core.input import text_value, whole_number
from core.models import MailboxProof, Membership, Organization, PlatformStaff, Role
from core.problems import Conflict, FieldErrors, ImpersonationRestricted
from money.models import Contract, RenewalObligation

from . import compliance, customfields, snapshots
from .models import (
    TAG_COLORS,
    Application,
    Department,
    Person,
    Resource,
    SoftwareCategory,
    Tag,
    Team,
    TeamMember,
    Vendor,
)
from .password_reset import end_open_links
from .services import WRITE_ROLES, _in_org, apply_person, audit, audit_account, is_uuid

# ---- reference data (Teams, Departments, Vendors, software categories) -------------------------------


def _department_users(obj):
    """The people and records in a department, named by what is actually there."""
    people = [p.name for p in Person.objects.filter(department=obj)]
    records = [r.name for r in Resource.objects.filter(department=obj)]
    if people and records:
        return people + records, "person or record", "people and records"
    if records:
        return records, "record", "records"
    return people, "person", "people"


# What uses each kind of reference data: obj -> (names of the referencing rows, singular noun, plural noun).
def _with_rules(found, rules: list[str]):
    """Add the notification rules that name the row to what blocks its delete, and say so in the nouns."""
    names, singular, plural = found
    if not rules:
        return found
    if not names:
        return rules, "notification rule", "notification rules"
    return names + rules, f"{singular} or notification rule", f"{plural} and notification rules"


def _team_users(obj):
    from notify import rule_services

    found = ([r.name for r in Resource.objects.filter(support_team=obj)], "record", "records")
    return _with_rules(found, rule_services.rules_naming_team(obj.organization, obj))


def _vendor_users(obj):
    from notify import rule_services

    found = (
        [a.resource.name for a in Application.objects.filter(vendor=obj).select_related("resource")]
        + list(RenewalObligation.objects.filter(vendor=obj).values_list("name", flat=True))
        + list(Contract.objects.filter(vendor=obj).values_list("name", flat=True)),
        "application, renewal or contract",
        "applications, renewals and contracts",
    )
    return _with_rules(found, rule_services.using(obj.organization, "vendor", obj))


REFERENCES = {
    "Team": _team_users,
    "Department": _department_users,
    "Vendor": _vendor_users,
    "SoftwareCategory": lambda obj: (
        [a.resource.name for a in Application.objects.filter(category=obj).select_related("resource")],
        "application",
        "applications",
    ),
}
# The word an audit action uses for each kind: team.created, category.deleted, ...
NOUNS = {Team: "team", Department: "department", Vendor: "vendor", SoftwareCategory: "category"}


def usage_counts(model, org):
    """{id: number of rows that use it} for every row of the model, in one query per source."""
    # Notification rules aren't counted here (the lists stay within their query budget); the delete guard
    # (REFERENCES) still names the rules that use a team or vendor.
    by_model: dict[str, list[tuple[str, Any]]] = {
        "Team": [("support_team", Resource.objects)],
        "Department": [("department", Person.objects), ("department", Resource.objects)],
        # renewals too, and contracts
        "Vendor": [
            ("vendor", Application.objects),
            ("vendor", RenewalObligation.objects),
            ("vendor", Contract.objects),
        ],
        "SoftwareCategory": [("category", Application.objects)],
    }
    sources = by_model[model.__name__]
    counts: dict = {}
    for field, source in sources:
        rows = source.filter(**{f"{field}__organization": org}).values(field).annotate(n=Count("pk"))
        for row in rows:
            if row[field]:
                counts[row[field]] = counts.get(row[field], 0) + row["n"]
    return counts


def _unique_name(model, org, name, exclude=None):
    name = (name or "").strip() if isinstance(name, str) or name is None else ""
    if not name:
        raise FieldErrors({"name": ["Enter a name."]})
    limit = model._meta.get_field("name").max_length
    if len(name) > limit:
        raise FieldErrors({"name": [f"Use {limit} characters or fewer."]})
    clash = model.objects.filter(organization=org, name__iexact=name)
    if exclude is not None:
        clash = clash.exclude(id=exclude.id)
    if clash.first() is not None:
        raise FieldErrors({"name": [f"A {model._meta.verbose_name} named {clash.first().name} already exists."]})
    return name


URL_MESSAGE = "Enter a full web address starting with http:// or https://."
# Vendor fields besides name: {field: (kind, column size)}.
VENDOR_FIELDS = {
    "website": ("url", 200),
    "support_url": ("url", 2000),
    "portal_url": ("url", 2000),
    "support_email": ("email", 254),
    "support_phone": ("text", 50),
    "notes": ("text", 5000),
}
DESCRIPTION_MAX = {Team: 5000, SoftwareCategory: 200}


def _text_value(data, field, errors, limit):
    value = data.get(field)
    value = value.strip() if isinstance(value, str) else ""
    if data.get(field) not in (None, "") and not isinstance(data.get(field), str):
        errors[field] = ["Send text."]
    elif len(value) > limit:
        errors[field] = [f"Use {limit:,} characters or fewer."]
    return value


def _apply_extra(org, obj, data, required=True):
    """The fields besides the name, when present in data: a team's description and members, a vendor's contact
    details, notes, account manager and custom fields, a category's description. Returns the members to set (a team),
    or None. `required=False`: a vendor's required custom fields aren't enforced (an import creates vendors by name)."""
    errors: dict[str, list[str]] = {}
    members = None
    if type(obj) in DESCRIPTION_MAX and "description" in data:
        obj.description = _text_value(data, "description", errors, DESCRIPTION_MAX[type(obj)])
    if isinstance(obj, Team) and "members" in data:
        members = _members(org, data["members"], errors)
    if isinstance(obj, Vendor):
        for field, (kind, limit) in VENDOR_FIELDS.items():
            if field not in data:
                continue
            value = _text_value(data, field, errors, limit)
            if value and kind == "url":
                try:
                    URLValidator(schemes=["http", "https"])(value)
                except ValidationError:
                    errors[field] = [URL_MESSAGE]
            if value and kind == "email":
                try:
                    EmailValidator()(value)
                except ValidationError:
                    errors[field] = ["Enter an email address such as support@example.com, or leave it blank."]
            setattr(obj, field, value)
        if "account_manager" in data:
            obj.account_manager = _in_org(Person, org, data["account_manager"], "account_manager", errors)
        custom = customfields.clean(
            org, "vendor", obj.custom_field_data, data, errors, creating=obj._state.adding, required=required
        )
        if custom is not None:
            obj.custom_field_data = custom
    if errors:
        raise FieldErrors(errors)
    return members


MEMBERS_MAX = 500


def _members(org, value, errors):
    """The People a team's member list names (Person IDs in this organization), de-duplicated in order."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors["members"] = ["Send members as a list of Person IDs."]
        return None
    ids = list(dict.fromkeys(value))
    if len(ids) > MEMBERS_MAX:
        errors["members"] = [f"A team can have at most {MEMBERS_MAX} members."]
        return None
    valid = [i for i in ids if is_uuid(i)]
    found = {str(p.id): p for p in Person.objects.filter(organization=org, id__in=valid)}
    if len(found) != len(ids):  # another organization's ID is unknown
        errors["members"] = ["Choose existing people."]
        return None
    return [found[i] for i in ids]


def _set_members(org, team, people):
    """Make the team's members exactly `people`. Returns (before, after) as sorted Person IDs (no names: audit data holds people as IDs only)."""
    before = sorted(str(i) for i in TeamMember.objects.filter(team=team).values_list("person_id", flat=True))
    snapshots.touch(team)  # the bulk insert below sends no signal; the team's snapshot lists its members
    wanted = {p.id for p in people}
    TeamMember.objects.filter(team=team).exclude(person_id__in=wanted).delete()
    have = set(TeamMember.objects.filter(team=team).values_list("person_id", flat=True))
    TeamMember.objects.bulk_create(
        [TeamMember(organization=org, team=team, person=p) for p in people if p.id not in have]
    )
    return before, sorted(str(p.id) for p in people)


def _audited_values(obj):
    """The fields besides the name an event records as before/after pairs; references as IDs."""
    if isinstance(obj, Vendor):
        values = {f: getattr(obj, f) for f in VENDOR_FIELDS}
        values["account_manager"] = str(obj.account_manager_id) if obj.account_manager_id else None
        values.update(customfields.audit_values(obj.organization_id, "vendor", obj.custom_field_data))
        return values
    if isinstance(obj, Team | SoftwareCategory):
        return {"description": obj.description}
    return {}


def noun(model):
    return NOUNS[model]


@transaction.atomic
def create_named(model, org, actor, data, required=True, note=None):
    """`required=False` leaves a vendor's required custom fields unchecked: the import creates vendors by name.
    `note` adds context to the event (an import names its run)."""
    obj = model(organization=org, name=_unique_name(model, org, data.get("name")))
    members = _apply_extra(org, obj, data, required)
    limits.check_room(org, limits.OTHER_RECORDS)
    obj.save()
    changes: dict = {"name": obj.name}
    if members:
        changes["members"] = _set_members(org, obj, members)[1]
    audit(org, actor, f"{noun(model)}.created", obj, {**changes, **(note or {})})
    return obj


@transaction.atomic
def update_named(model, org, actor, obj, data):
    if isinstance(obj, Vendor) and customfields.KEY in data:
        # Custom field values merge onto the stored ones under a row lock, so concurrent edits of two fields both stay.
        obj.custom_field_data = (
            Vendor.objects.select_for_update().only("custom_field_data").get(id=obj.id).custom_field_data
        )
    before_name, before = obj.name, _audited_values(obj)
    if "name" in data:
        obj.name = _unique_name(model, org, data.get("name"), exclude=obj)
    members = _apply_extra(org, obj, data)
    obj.save()
    changes: dict = {"name": [before_name, obj.name]}
    after = _audited_values(obj)
    changes.update({k: [before[k], after[k]] for k in after if before[k] != after[k]})
    if members is not None:
        was, now = _set_members(org, obj, members)
        if was != now:
            changes["members"] = [was, now]
    audit(org, actor, f"{noun(model)}.updated", obj, changes)
    return obj


@transaction.atomic
def delete_named(model, org, actor, obj):
    """Delete only when unreferenced, naming what blocks it."""
    names, singular, plural = REFERENCES[model.__name__](obj)
    refs = sorted(names, key=str.lower)
    if refs:
        shown = ", ".join(refs[:3]) + (f" and {len(refs) - 3} more" if len(refs) > 3 else "")
        uses = f"1 {singular} uses it" if len(refs) == 1 else f"{len(refs)} {plural} use it"
        raise Conflict(
            f"{obj.name} can't be deleted: {uses} ({shown}). Reassign them first.",
            extra={"blocking_references": {plural: len(refs)}, "blocking_names": refs[:10]},
        )
    if isinstance(obj, Vendor):  # its risk file and attachments go with it, each audited (files retired)
        from . import attachments, vendor_risk

        vendor_risk.remove_for(org, actor, obj)  # first: its documents link the attachments
        attachments.remove_for(org, actor, obj)
    audit(org, actor, f"{noun(model)}.deleted", obj, {"name": obj.name})
    obj.delete()


# ---- tags -----------------------------------------------------------------------


@transaction.atomic
def create_tag(org, actor, data):
    """The tag named data["name"] (spaces collapsed, matched ignoring case, as records' tags are: catalog/tags.py), made
    if it doesn't exist yet. Only a real create is audited (tag.created); an existing tag is returned as it is.
    Catalog-as-code uses it for a Tag file whose tag no record has yet."""
    from . import tags

    errors: dict[str, list[str]] = {}
    names = tags.clean([data.get("name")] if isinstance(data.get("name"), str) else None, errors, field="name")
    if not names:
        raise FieldErrors(errors or {"name": ["Enter a name."]})
    existing = Tag.objects.filter(organization=org, name__iexact=names[0]).first()
    if existing is not None:
        return existing
    limits.check_room(org, limits.OTHER_RECORDS)
    tag = tags.resolve(org, names)[0]
    audit(org, actor, "tag.created", tag, {"name": tag.name})
    return tag


@transaction.atomic
def update_tag(org, actor, tag, data):
    """A tag's colour (one of TAG_COLORS, or blank for the neutral default) and description. Names are changed by
    retagging records (catalog/tags.py), not here."""
    errors: dict[str, list[str]] = {}
    before = {"color": tag.color, "description": tag.description}
    if "color" in data:
        color = data.get("color")
        color = "" if color is None else color  # null clears; 0, false or [] are refused, not read as empty
        if not isinstance(color, str) or (color not in TAG_COLORS and color != ""):
            errors["color"] = [f"Use one of: {', '.join(TAG_COLORS)}, or leave it empty."]
        else:
            tag.color = color
    if "description" in data:
        tag.description = _text_value(data, "description", errors, 500)
    if errors:
        raise FieldErrors(errors)
    tag.save()
    after = {"color": tag.color, "description": tag.description}
    audit(
        org,
        actor,
        "tag.updated",
        tag,
        {"name": tag.name, **{k: [before[k], v] for k, v in after.items() if before[k] != v}},
    )
    return tag


# ---- people ---------------------------------------------------------------------

# Called with (org, person) when a Person is anonymized; each returns {what: count purged}. The imports
# app registers the import-file, raw-row and error-report purge here.
ANONYMIZE_HOOKS: list[Callable[[Organization, Person], dict[str, int]]] = []


def _locked_person(person):
    """The Person as it is now, locked until the transaction ends: the caller's copy may predate an anonymization or
    an edit that committed meanwhile, and anonymizing can't be undone by a stale save."""
    return Person.objects.select_for_update(of=("self",)).select_related("department", "user").get(pk=person.pk)


@transaction.atomic
def update_person(org, actor, person, data):
    person = _locked_person(person)
    if person.anonymized_at is not None:
        raise Conflict(f"{person.name} was anonymized and can't be edited.")
    changed = apply_person(org, person, data)
    person.save()
    audit(org, actor, "person.updated", person, {"fields": changed})  # field names only, no personal data
    return person


@transaction.atomic
def anonymize_person(org, actor, person, confirm_name):
    """Remove personal data but keep the row so history still resolves (Admin only). The caller
    types the Person's name to confirm; it can't be undone."""
    confirmed_name = person.name  # what the Admin saw and typed
    person = _locked_person(person)
    if person.anonymized_at is not None:
        raise Conflict(f"{person.name} is already anonymized.")
    if (confirm_name or "").strip() not in (person.name, confirmed_name):
        raise FieldErrors({"confirm_name": [f"Type {person.name} exactly to confirm."]})
    # Next unused number; counting rows could reuse a number, since names like #3 can be seeded or imported.
    taken = Person.objects.filter(organization=org, name__regex=r"^Former person #[0-9]+$").values_list(
        "name", flat=True
    )
    n = max((int(name.rsplit("#", 1)[1]) for name in taken), default=0) + 1
    person.name, person.email, person.title = f"Former person #{n}", "", ""
    person.department, person.user, person.active = None, None, False
    person.anonymized_at = timezone.now()
    person.save()
    purged: dict[str, int] = {}
    for hook in ANONYMIZE_HOOKS:
        purged.update(hook(org, person))
    audit(org, actor, "person.anonymized", person, {"purged": purged})
    return person


# ---- users and organization -----------------------------------------------------


def _active_admins(org):
    return Membership.objects.filter(organization=org, role=Role.ADMIN, user__is_active=True)


def _lock_memberships(user_id):
    """Every membership of the user, locked in a fixed order (by id) so two organizations changing the same account
    at once queue instead of each judging a state the other is about to change (or deadlocking)."""
    return list(Membership.objects.select_for_update().filter(user_id=user_id).order_by("id"))


def lock_account_changes(org, membership):
    """The locks an account change takes, in the order update_member and remove_member take them: the organization,
    then every membership of the user by id. An If-Match check (catalog/account_viewsets.py) takes them before it
    compares revisions, so it never holds a membership while waiting for the organization."""
    list(Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True))
    _lock_memberships(membership.user_id)


def _other_organizations(membership):
    """Does the member also belong to another organization, or is the account platform staff? Their account
    (password, active flag, sign-in identity) is then not one organization's, and no single organization's Admin may
    change it: a staff account in one organization would otherwise be under that organization's Admin's control, and
    with it the platform console. Locks the user's memberships first; a membership removed meanwhile (by a concurrent
    request) is a conflict rather than a stale answer."""
    rows = _lock_memberships(membership.user_id)
    if not any(m.pk == membership.pk for m in rows):
        raise Conflict("This person is no longer a member of this organization.")
    return len(rows) > 1 or PlatformStaff.objects.filter(user_id=membership.user_id).exists()


def shared_refusal(membership) -> str:
    """The message for an account-level change refused by _other_organizations."""
    if PlatformStaff.objects.filter(user_id=membership.user_id).exists():
        return STAFF_ACCOUNT
    return SHARED_ACCOUNT


def account_facts(user_ids):
    """(users who belong to more than one organization, users whose password must change) among user_ids. Both are
    about the account, not one membership: see core/access.py."""
    rows = Membership.objects.filter(user_id__in=list(user_ids)).values_list("user_id", "must_change_password")
    seen: set[int] = set()
    shared: set[int] = set()
    must_change: set[int] = set()
    for user_id, flag in rows:
        (shared if user_id in seen else seen).add(user_id)
        if flag:
            must_change.add(user_id)
    # Platform staff accounts aren't any one organization's either (_other_organizations).
    shared |= set(PlatformStaff.objects.filter(user_id__in=seen).values_list("user_id", flat=True))
    return shared, must_change


SHARED_ACCOUNT = (
    "This person also belongs to another organization, so their account isn't this organization's to change. "
    "Remove them from this organization instead, or ask them to change it themselves."
)
SSO_ONLY_ACCOUNT = (
    "This person signs in through single sign-on only and has no password, so there is none to reset. Help them sign "
    "in through your identity provider."
)
STAFF_ACCOUNT = (
    "This account belongs to the service operator's staff, so it isn't this organization's to change. Remove them "
    "from this organization instead, or ask them to change it themselves."
)


# pg_advisory_xact_lock key for first-run setup: the setup API and the CLI create the first Admin one at a time.
BOOTSTRAP_LOCK = 0x5357_0001


@transaction.atomic
def bootstrap_admin(data, via="cli"):
    """Create the first Organization, Admin and coordinator, from the CLI (`bootstrap_admin --create`)
    or the first-run setup page (`via="setup"`, core/setup_views.py); never forced to change. Serialised by an
    advisory lock and refused once setup is closed (core/bootstrap.py), which this closes."""
    from core import bootstrap

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BOOTSTRAP_LOCK])
    if not bootstrap.pending():
        raise Conflict("This instance already has an organization. Use reset_admin to recover an Admin account.")
    errors: dict[str, list[str]] = {}
    email = text_value(data.get("email"), "email", errors).lower()
    org_name = text_value(data.get("org_name"), "org_name", errors)
    if not org_name:
        errors.setdefault("org_name", ["Enter the organization name."])
    if not isinstance(data.get("timezone"), str) or data.get("timezone") not in available_timezones():
        errors["timezone"] = ["Choose a timezone such as America/New_York."]
    if "@" not in email:
        errors.setdefault("email", ["Enter an email address."])
    first, _, last = text_value(data.get("name"), "name", errors).partition(" ")
    if not first:
        errors.setdefault("name", ["Enter a display name."])
    user = get_user_model()(username=email, email=email, first_name=first, last_name=last)
    password = data.get("password")
    if not isinstance(password, str):
        errors["password"] = ["Enter a password."]
    else:
        try:
            validate_password(password, user)
        except ValidationError as e:
            errors["password"] = e.messages
    if errors:
        raise FieldErrors(errors)
    user.set_password(password)
    user.save()
    MailboxProof.objects.get_or_create(user=user, defaults={"via": via})  # the operator's
    org = Organization.objects.create(name=org_name, timezone=data["timezone"], coordinator=user)
    membership = Membership.objects.create(user=user, organization=org, role=Role.ADMIN)
    bootstrap.close()
    audit(org, None, "user.created", None, {"user": user.id, "role": membership.role, "via": via})
    return membership


CLOUD_SETUP = (
    "This is a hosted (cloud) installation: first-run setup creates the operator's platform staff account, not an "
    "organization. Customers create their organizations by signing up."
)


@transaction.atomic
def bootstrap_platform_staff(data):
    """Cloud mode's first-run setup (SOFTWARDEN_DEPLOYMENT=cloud, docs/CLOUD.md; `bootstrap_admin --create`): the
    operator's own account, made platform staff, in no organization. No organization is created here (so nothing
    about its plan is decided); customers' organizations come from signup (core/capabilities.SIGNUP_EDITION).

    Allowed while no platform staff exists, even when setup is already closed because a customer signed up first
    (signup is on by default in cloud mode); refused once any staff row exists, after which `platform_staff grant`
    adds more. Serialised by the same advisory lock as bootstrap_admin, so two runs at once make one account. Closes
    setup. Audited in the platform stream (`staff.granted`, `via: bootstrap`), since there is no organization to write
    in."""
    from core import bootstrap

    from .platform_admin import platform_audit

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BOOTSTRAP_LOCK])
    if PlatformStaff.objects.exists():
        raise Conflict(
            "This instance already has platform staff. Grant platform staff to another existing account with "
            "`manage.py platform_staff grant <email>`."
        )
    errors: dict[str, list[str]] = {}
    email = text_value(data.get("email"), "email", errors).lower()
    try:
        EmailValidator()(email)
    except ValidationError:
        errors["email"] = ["Enter an email address."]
    else:
        users = get_user_model().objects
        if users.filter(username=email).exists() or users.filter(email__iexact=email).exists():
            errors["email"] = [
                "An account already has this email. Make it platform staff with `manage.py platform_staff grant "
                "<email>`, or choose another email."
            ]
    first, _, last = text_value(data.get("name"), "name", errors).partition(" ")
    if not first:
        errors.setdefault("name", ["Enter a display name."])
    user = get_user_model()(username=email, email=email, first_name=first, last_name=last)
    password = data.get("password")
    if not isinstance(password, str):
        errors["password"] = ["Enter a password."]
    else:
        try:
            validate_password(password, user)
        except ValidationError as e:
            errors["password"] = e.messages
    if errors:
        raise FieldErrors(errors)
    user.set_password(password)
    user.save()
    MailboxProof.objects.get_or_create(user=user, defaults={"via": "bootstrap"})  # the operator's
    PlatformStaff.objects.create(user=user)
    bootstrap.close()
    platform_audit(None, "staff.granted", details={"user": user.pk, "email": user.email, "via": "bootstrap"})
    return user


@transaction.atomic
def reactivate_account(email) -> tuple[Any, list[Any]]:
    """`manage.py reactivate_account`: turn back on a deactivated account that
    belongs to no organization, as SCIM leaves one it can't link. Refused for an account with a membership (each of
    its organizations' Admins reactivates it there) and for an active one. The account has no organization to write
    the event in, so `user.updated` (`active` false to true, `via: operator`) is written in each organization whose
    audit log names it (as the actor or as the `user`), which is where its deactivation was recorded; with none, the
    command's log line is the record. Returns (user, those organizations)."""
    from core.models import AuditEvent

    user = get_user_model().objects.select_for_update().filter(username=(email or "").strip().lower()).first()
    if user is None:
        raise FieldErrors({"email": ["No account has this email."]})
    if Membership.objects.filter(user=user).exists():
        raise FieldErrors(
            {"email": ["This account belongs to an organization. An Admin there reactivates it (Users and access)."]}
        )
    if user.is_active:
        raise FieldErrors({"email": ["This account is already active."]})
    user.is_active = True
    user.save(update_fields=["is_active"])
    named = AuditEvent.objects.filter(Q(actor=user) | Q(changes__user=user.id))
    orgs = list(Organization.objects.filter(id__in=named.values("organization_id")).order_by("name"))
    for org in orgs:
        audit(org, None, "user.updated", None, {"user": user.id, "active": [False, True], "via": "operator"})
    return user, orgs


@transaction.atomic
def break_glass_reset(email, any_member=False):
    """CLI reset for a locked-out Admin: temporary password, forced change at next sign-in. The operator acts for
    the whole instance, so an account in several organizations may be reset; the event is written in each of them.
    `any_member` also resets someone who isn't an Admin anywhere: an Admin can't reset an account shared with another
    organization, so this is how such a member recovers."""
    rows = Membership.objects.select_related("user", "organization").filter(
        user__username=(email or "").strip().lower()
    )
    if not any_member:
        rows = rows.filter(role=Role.ADMIN)
    membership = rows.order_by("created_at", "id").first()
    if membership is None:
        if any_member:
            raise FieldErrors({"email": ["No member account has this email."]})
        raise FieldErrors({"email": ["No Admin account has this email. Use --any-member for someone who isn't one."]})
    if not membership.user.is_active:
        membership.user.is_active = True
        membership.user.save()
        audit_account(membership.user, None, "user.updated", {"user": membership.user.id, "active": [False, True]})
    return membership, _set_temporary_password(membership, None)


@transaction.atomic
def update_member(org, actor, membership, data):
    # One change to an organization's memberships at a time, judged on the state after the lock:
    # two concurrent demotions each counted two Admins and together left none.
    org = Organization.objects.select_for_update().get(pk=org.pk)
    shared = _other_organizations(membership)  # locks the user's memberships (by id) before the user row
    membership = Membership.objects.select_for_update(of=("self", "user")).select_related("user").get(pk=membership.pk)
    user = membership.user
    new_role = data.get("role", membership.role)
    new_active = data.get("active", user.is_active)
    if not isinstance(new_active, bool):  # "false" is refused, not read as true
        raise FieldErrors({"active": ["Send true or false."]})
    if new_role not in Role.values:
        raise FieldErrors({"role": ["Choose a role."]})
    if new_active != user.is_active and shared:
        raise Conflict(shared_refusal(membership))
    losing_admin = membership.role == Role.ADMIN and user.is_active and (new_role != Role.ADMIN or not new_active)
    if losing_admin and _active_admins(org).count() <= 1:
        raise Conflict("This is the last active Admin. Make someone else an Admin first.")
    if org.coordinator_id == user.id and (not new_active or new_role == Role.VIEWER):
        raise Conflict("This person is the IT coordinator. Choose a new coordinator in Organization settings first.")
    if new_active and not user.is_active:
        # Reactivating takes a place under the members limit like a new member does (core/limits.py): an
        # Admin's PATCH answers 409 /problems/limit-reached, SCIM a SCIM 409.
        limits.check_room(org, limits.MEMBERS)
    changes: dict[str, Any] = {}
    was_admin = membership.role == Role.ADMIN and user.is_active
    if new_role != membership.role:
        changes["role"] = [membership.role, new_role]
        membership.role = new_role
        membership.save()
    if new_active != user.is_active:
        changes["active"] = [user.is_active, new_active]
        user.is_active = new_active
        user.save()
        if not new_active:
            # A deactivated account's sessions end now, so reactivating it doesn't bring an old (possibly
            # stolen) cookie back to life; the person signs in again. API tokens are kept and resume on reactivation
            # (docs/API.md); an Admin revokes them separately when the account was compromised.
            changes["sessions_ended"] = sessions.delete_for_users([user.pk])
    if was_admin and not (membership.role == Role.ADMIN and user.is_active):
        # Tokens this Admin made for other members stand on their authority, which ends here.
        revoked = _revoke_tokens_created_for_others(org, actor, user, "creator_no_longer_admin")
        if revoked:
            changes["api_access_revoked"] = revoked  # not "tokens_revoked": keys naming a token are redacted
    audit(org, actor, "user.updated", None, {"user": user.id, **changes})
    return membership


def _revoke_tokens_created_for_others(org, actor, user, reason) -> int:
    from api.tokens import revoke_created_for_others  # the api app sits above catalog; imported here, one-way

    return revoke_created_for_others(org, actor, user, reason)


@transaction.atomic
def remove_member(org, actor, membership):
    """Remove someone from this organization only: their membership, its digest preference, their API tokens and their
    renewals calendar feed here go (revoked); their account and other memberships stay. Someone in no other
    organization is deactivated instead, which keeps their sign-in history with the one organization they belong to."""
    from api.models import ApiToken  # the api app sits above catalog; imported here to keep module imports one-way
    from money import calendar_feeds  # likewise the money app
    from money.models import CalendarFeed

    org = Organization.objects.select_for_update().get(pk=org.pk)
    # Lock all of the user's memberships before judging "another organization": two Admins removing the same person
    # from their two organizations at once would otherwise each see the other membership and leave none.
    shared = _other_organizations(membership)
    membership = Membership.objects.select_related("user").get(pk=membership.pk)
    user = membership.user
    if not shared:
        raise Conflict("This is their only organization. Deactivate them instead.")
    if membership.role == Role.ADMIN and user.is_active and _active_admins(org).count() <= 1:
        raise Conflict("This is the last active Admin. Make someone else an Admin first.")
    if org.coordinator_id == user.id:
        raise Conflict("This person is the IT coordinator. Choose a new coordinator in Organization settings first.")
    revoked = ApiToken.objects.filter(organization=org, user=user, revoked_at=None).update(revoked_at=timezone.now())
    # And the tokens they made here for other members, which stood on their Admin authority.
    revoked += _revoke_tokens_created_for_others(org, actor, user, "creator_removed")
    # Their renewals calendar feed here stops too, for good: re-adding them later doesn't bring the old address back.
    for feed in CalendarFeed.objects.filter(organization=org, user=user, revoked_at=None):
        calendar_feeds.revoke_feed(org, actor, feed)
    role = membership.role
    membership.delete()
    # "api_access_revoked", not "tokens_revoked": keys naming a token are redacted from audit payloads.
    audit(org, actor, "user.removed", None, {"user": user.id, "role": role, "api_access_revoked": revoked})
    from .invitations import unlink_after_removal  # invitations imports this module

    unlink_after_removal(org, actor, user)


@transaction.atomic
def switch_organization(user, current, target):
    """Audit a switch of the session's current organization (the view stores the choice in the session). One event in
    each organization; neither names the other, so no organization's audit log reveals another's existence."""
    audit(current.organization, user, "user.organization_switched", None, {"user": user.id, "direction": "out"})
    audit(target.organization, user, "user.organization_switched", None, {"user": user.id, "direction": "in"})


@transaction.atomic
def reset_password(org, actor, membership):
    """An Admin's reset (temporary password, forced change). Refused for an account shared with another organization:
    one organization's Admin could otherwise sign in as someone who is an Admin elsewhere. Refused too for an account
    with no password (made by single sign-on or SCIM): a temporary password would let the organization's Admin sign in
    as an address nobody proved, and make that account hold it (invitations.managed)."""
    if _other_organizations(membership):
        raise Conflict(shared_refusal(membership))
    if not membership.user.has_usable_password():  # nor any account an organization made (invitations.managed)
        raise Conflict(SSO_ONLY_ACCOUNT)
    return _set_temporary_password(membership, actor)


def _set_temporary_password(membership, actor):
    temp = secrets.token_urlsafe(12)
    membership.user.set_password(temp)
    membership.user.save()
    end_open_links(membership.user)  # an emailed reset link from before this mustn't set the password again
    membership.must_change_password = True
    membership.save()
    audit_account(membership.user, actor, "user.password_reset", {"user": membership.user.id})
    return temp


@transaction.atomic
def change_password(user, current, new):
    if not user.check_password(current or ""):
        raise FieldErrors({"current_password": ["That isn't your current password."]})
    if current == new:
        raise FieldErrors({"new_password": ["Choose a different password from your current one."]})
    try:
        validate_password(new or "", user)
    except ValidationError as e:
        raise FieldErrors({"new_password": e.messages}) from e
    user.set_password(new)
    user.save()
    end_open_links(user)  # an emailed reset link from before this mustn't set the password again
    # The flag is about the account (core/access.py): clear it everywhere.
    Membership.objects.filter(user=user, must_change_password=True).update(must_change_password=False)
    audit_account(user, user, "user.password_changed", {"user": user.id})


# A license key in PATCH organization is refused, not ignored: it isn't an organization setting.
LICENSE_KEY_REFUSED = (
    "The license key isn't an organization setting and can't be set here. The installation's operator sets the "
    "organization's edition with `manage.py set_edition`."
)
# What an Admin may change in Organization settings. Saving only these keeps the operator-only columns (edition,
# licence key, slug; core/capabilities.py) out of reach of this endpoint, even from a stale copy of the row.
ORGANIZATION_SETTINGS = (
    "name",
    "timezone",
    "review_interval_days",
    "renewal_lead_days",
    "reminder_thresholds",
    "digest_time",
    "coordinator",
)
# Organization.settings keys an Admin may change here (each must be in snapshots.ORG_SETTINGS_AUDITED).
SETTINGS_KEYS = (
    compliance.SETTING,
    "data_quality_disabled_rules",
    "sso_auto_provision",
    "scim_default_role",  # the optional ee package (capability scim)
    "scim_role_groups",
    "inactive_seat_days",  # discovery/licence_waste.py SETTING
)


ORG_NAME_MAX = 200  # Organization.name


def _refuse_sign_in_settings_while_impersonating(data) -> None:
    """Platform staff impersonating a member may change ordinary settings, never the sign-in ones
    (core/impersonation_routes.RESTRICTED_SETTINGS); checked before anything else, capabilities included."""
    from core.impersonation_routes import RESTRICTED_SETTINGS, RESTRICTED_SETTINGS_DETAIL
    from core.request_context import current_impersonator

    if current_impersonator() is not None and isinstance(data, dict) and RESTRICTED_SETTINGS & set(data):
        raise ImpersonationRestricted(RESTRICTED_SETTINGS_DETAIL)


@transaction.atomic
def update_organization(org, actor, data):
    _refuse_sign_in_settings_while_impersonating(data)
    errors: dict[str, list[str]] = {}
    for key in ("licence_key", "license_key"):
        if key in data:  # it used to answer 200 and change nothing
            errors[key] = [LICENSE_KEY_REFUSED]
    if "name" in data:
        name = text_value(data.get("name"), "name", errors)
        if len(name) > ORG_NAME_MAX:
            errors["name"] = [f"Use {ORG_NAME_MAX} characters or fewer."]
        else:
            org.name = name or org.name
    if "timezone" in data:
        if not isinstance(data["timezone"], str) or data["timezone"] not in available_timezones():
            errors["timezone"] = ["Choose a timezone such as America/New_York."]
        else:
            org.timezone = data["timezone"]
    if "review_interval_days" in data:
        # A whole number or its digits; true, 12.5 or "30.0" are refused rather than read as 1, 12 or 30.
        days = whole_number(data["review_interval_days"], 7, 730)
        if days is None:
            errors["review_interval_days"] = ["Enter a number of days from 7 to 730."]
        else:
            org.review_interval_days = days
    if "renewal_lead_days" in data:
        # A whole number or its digits; true, 12.5 or "30.0" are refused rather than read as 1, 12 or 30.
        days = whole_number(data["renewal_lead_days"], 1, 365)
        if days is None:
            errors["renewal_lead_days"] = ["Enter a number of days from 1 to 365."]
        else:
            org.renewal_lead_days = days
    if "reminder_thresholds" in data:
        raw = data["reminder_thresholds"]
        values: list = raw if isinstance(raw, list) else []
        ok = 1 <= len(values) <= 6 and all(isinstance(v, int) and not isinstance(v, bool) for v in values)
        if not ok or any(not 1 <= v <= 365 for v in values) or len(set(values)) != len(values):
            errors["reminder_thresholds"] = ["Enter one to six different numbers of days from 1 to 365."]
        elif values != sorted(values, reverse=True):
            errors["reminder_thresholds"] = [
                "List the thresholds from the largest to the smallest, such as 90, 60, 30, 7."
            ]
        else:
            org.reminder_thresholds = values
    if "digest_time" in data:
        try:
            org.digest_time = time.fromisoformat(data["digest_time"]) if isinstance(data["digest_time"], str) else None
        except ValueError:
            org.digest_time = None
        if org.digest_time is None:
            errors["digest_time"] = ["Enter a time such as 08:00."]
    if "coordinator" in data:
        m = None
        coordinator = whole_number(data["coordinator"], 1)
        if coordinator is not None:
            m = Membership.objects.filter(
                organization=org, user_id=coordinator, role__in=WRITE_ROLES, user__is_active=True
            ).first()
        if m is None:
            errors["coordinator"] = ["Choose an active Admin or Editor."]
        else:
            org.coordinator = m.user
    settings_changes: dict[str, list[Any]] = {}
    if compliance.SETTING in data:
        # The capability is checked before the value, so an organization without it (a platform revoke) gets the
        # capability problem (403) whatever it sends.
        require_capability(org, COMPLIANCE_PROFILE)
        value = data[compliance.SETTING]
        if not isinstance(value, bool):
            errors[compliance.SETTING] = ["Use true or false."]
        else:
            before = (org.settings or {}).get(compliance.SETTING) is True
            org.settings = {**(org.settings or {}), compliance.SETTING: value}
            if before != value:
                settings_changes[compliance.SETTING] = [before, value]
    # Single sign-on auto-provision (core/sso.AUTO_PROVISION): unknown identities wait for an Admin.
    from core import sso

    if sso.AUTO_PROVISION in data:
        require_capability(org, SSO_OIDC)
        value = data[sso.AUTO_PROVISION]
        if not isinstance(value, bool):
            errors[sso.AUTO_PROVISION] = ["Use true or false."]
        else:
            before = sso.auto_provision(org)
            org.settings = {**(org.settings or {}), sso.AUTO_PROVISION: value}
            if before != value:
                settings_changes[sso.AUTO_PROVISION] = [before, value]
    # SCIM provisioning (the optional ee package's scim/config.py): the default role and the opt-in group-to-role mapping.
    scim_changes: dict[str, list[Any]] = {}
    scim_sent = "scim_default_role" in data or "scim_role_groups" in data
    if scim_sent:
        require_capability(org, SCIM)
        from ee.scim import config as scim_config

        scim_changes = scim_config.clean(org, data, errors)
        settings_changes.update(scim_changes)
    # Attachment limits (catalog/attachments.py): an Admin may only tighten the installation's; null removes the key.
    from . import attachments

    limit_values = attachments.clean_limit_settings(data, errors)
    if limit_values and not errors:
        current = dict(org.settings or {})
        for key, value in limit_values.items():
            was = current.get(key)
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
            if was != value:
                settings_changes[key] = [was, value]
        org.settings = current
    # Data-quality rules turned off (catalog/quality.py): a list of rule ids; an empty list (or null) turns all on.
    from . import quality

    disabled = quality.clean_disabled(data, errors)
    if disabled is not None and not errors:
        before_disabled = quality.disabled_ids(org)
        current = dict(org.settings or {})
        if disabled:
            current[quality.SETTING] = disabled
        else:
            current.pop(quality.SETTING, None)
        org.settings = current
        if sorted(before_disabled) != disabled:
            settings_changes[quality.SETTING] = [sorted(before_disabled), disabled]
    # Licence waste (discovery/licence_waste.py): the days without a sign-in that make a licence inactive.
    from discovery import licence_waste

    waste_days = licence_waste.clean_days(data, errors)
    if waste_days is not None and not errors:
        before_days = licence_waste.inactive_days(org)
        org.settings = {**(org.settings or {}), licence_waste.SETTING: waste_days}
        if before_days != waste_days:
            settings_changes[licence_waste.SETTING] = [before_days, waste_days]
    if errors:
        raise FieldErrors(errors)
    settings_written = (
        compliance.SETTING in data
        or sso.AUTO_PROVISION in data
        or bool(limit_values)
        or disabled is not None
        or scim_sent
        or waste_days is not None
    )
    org.save(update_fields=[*ORGANIZATION_SETTINGS, *(["settings"] if settings_written else [])])
    if "renewal_lead_days" in data:  # contracts store their deadline, and Assumed ones depend on the lead time
        from money import contracts

        contracts.refresh_deadlines(org)
    compliance.forget(org.pk)  # later writes in this unit snapshot with the new setting
    changes = {k: str(v) for k, v in data.items() if k in ORGANIZATION_SETTINGS}
    audit(org, actor, "organization.updated", org, {**changes, **settings_changes})
    if scim_changes:  # a new mapping or default role: every provisioned member's role follows it now
        from ee.scim import provisioning as scim_provisioning

        scim_provisioning.recompute_all(org, actor)
    return org
