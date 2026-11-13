"""All writes go through these functions: organization scope, role, validation, and audit in one transaction."""

from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from core import dates, limits
from core.access import WRITE_ROLES as WRITE_ROLES  # defined in core; read here as services.WRITE_ROLES
from core.input import text_value, whole_number
from core.models import AuditEvent, Membership
from core.problems import Conflict, FieldErrors
from core.request_context import audit_via, current_impersonator, current_request_id

from . import compliance, customfields, relationship_types, snapshots, staleness, tags
from .compliance import yes_no
from .models import (
    RECORD_KINDS,
    AgentAuthMode,
    AgentChannel,
    AgentModel,
    AgentPlatform,
    AgentRisk,
    AIAgent,
    AIModel,
    Application,
    ApplicationInstance,
    AuthenticationMethod,
    BuiltBy,
    Component,
    ComponentType,
    Criticality,
    DataCategory,
    DataClassification,
    Department,
    DeploymentModel,
    Environment,
    Hosting,
    InstanceStatus,
    Integration,
    IntegrationAuthMethod,
    IntegrationDirection,
    Kind,
    LicenseModel,
    Lifecycle,
    LinkConfirmation,
    Person,
    RecordReview,
    Relationship,
    RelationshipKind,
    RelationshipType,
    Resource,
    SoftwareCategory,
    Team,
    Vendor,
)
from .redaction import AUDIT_DENYLIST as AUDIT_DENYLIST  # read as services.AUDIT_DENYLIST
from .redaction import REDACTED as REDACTED
from .redaction import redact
from .state import (
    ACTIVE_LIFECYCLES,
    CONFIRMED_FIELDS,
    confirmed_snapshot,
    integration_signature,
    relationship_signature,
)

org_today = dates.org_today  # dates are judged in the organization's timezone, not the server's


# Secrets never enter an audit payload, by key name at any depth (catalog/redaction.py); a Person's identifying fields
# are referenced by the Person ID (target_id) instead of copied.
PERSON_FIELDS = {"name", "email", "title"}


def audit(org, actor, action, target=None, changes=None):
    """Write the AuditEvent for a change in the caller's transaction. `changes` is the action's payload, which the
    labels and summaries read; the target's before and after snapshots, the related record of a child row
    (snapshots.related_of) and the unit's request id are added here."""
    changes = redact({**(changes or {}), **audit_via()})
    if isinstance(target, Person):
        changes = {k: v for k, v in changes.items() if k not in PERSON_FIELDS}
    prechange, postchange = snapshots.take(target, action)
    parent = snapshots.related_of(target)
    AuditEvent.objects.create(
        organization=org,
        actor=actor,
        action=action,
        target_type=type(target).__name__ if target is not None else "",
        target_id=getattr(target, "id", None),
        related_target_type=parent[0] if parent else "",
        related_target_id=parent[1] if parent else None,
        request_id=current_request_id(),
        changes=changes,
        prechange=prechange,
        postchange=postchange,
        impersonated_by_id=current_impersonator(),
    )
    # A write that can move a reminder's date regenerates that source's reminders once the transaction commits.
    from notify import reminders, webhooks

    reminders.after_write(org, target)
    # The actions webhooks subscribe to become events, posted once the transaction commits.
    webhooks.after_audit(org, actor, action, target, changes)


def audit_account(user, actor, action, changes=None):
    """An event about the account rather than one organization's data (a password change, a failed sign-in, a linked
    identity), written once in every organization the user belongs to, so each organization's Admins see it."""
    org_ids = Membership.objects.filter(user=user).values_list("organization_id", flat=True)
    request_id = current_request_id()
    for org_id in org_ids:
        AuditEvent.objects.create(
            organization_id=org_id,
            actor=actor,
            action=action,
            request_id=request_id,
            changes=redact({**(changes or {}), **audit_via()}),
            impersonated_by_id=current_impersonator(),
        )


def _in_org(model, org, value, field, errors, active_only=False):
    """Resolve an ID within the organization; another organization's ID is reported as unknown."""
    if value in (None, ""):
        return None
    obj = model.objects.filter(organization=org, id=value).first() if is_uuid(value) else None
    if obj is None:
        errors[field] = ["Choose an existing record."]
    elif active_only and not obj.active:
        errors[field] = ["Inactive people can't be owners."]
    return obj


def is_uuid(value):
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def apply_person(org, person, data):
    """Validate and set the Person fields present in data. Email and linked User are unique per
    organization, case-insensitive; the error names the existing Person. Returns the changed field names."""
    from django.contrib.auth import get_user_model

    errors: dict[str, list[str]] = {}
    changed = []
    others = Person.objects.filter(organization=org).exclude(id=person.id)
    if "name" in data or person.pk is None:
        person.name = text_value(data.get("name"), "name", errors)
        if not person.name:
            errors.setdefault("name", ["Enter a name."])
        elif len(person.name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        changed.append("name")
    if "email" in data:
        person.email = text_value(data.get("email"), "email", errors)
        if person.email:
            try:
                EmailValidator()(person.email)
            except ValidationError:
                errors["email"] = ["Enter an email address such as name@example.org, or leave it blank."]
            else:
                clash = others.filter(email__iexact=person.email).first()
                if clash is not None:
                    errors["email"] = [f"A person with this email already exists: {clash.name}."]
        changed.append("email")
    if "title" in data:
        person.title = text_value(data.get("title"), "title", errors)
        if len(person.title) > 200:
            errors["title"] = ["Use 200 characters or fewer."]
        changed.append("title")
    if "department" in data:
        person.department = _in_org(Department, org, data["department"], "department", errors)
        changed.append("department")
    if "user" in data:
        user = None
        if data["user"] not in (None, ""):
            uid = user_id(data["user"])  # "²" or a number past the key range is a field error, not a 500
            user = (
                get_user_model().objects.filter(id=uid, membership__organization=org).first()
                if uid is not None
                else None
            )
            if user is None:
                errors["user"] = ["Choose a user in this organization."]
            else:
                clash = others.filter(user=user).first()
                if clash is not None:
                    errors["user"] = [f"This user is already linked to {clash.name}."]
        person.user = user
        changed.append("user")
    if "active" in data:
        if isinstance(data["active"], bool):  # "false" or 0 is refused, not read as true or false
            person.active = data["active"]
            changed.append("active")
        else:
            errors["active"] = ["Send true or false."]
    if errors:
        raise FieldErrors(errors)
    return changed


@transaction.atomic
def create_person(org, actor, data, note=None):
    """`note` adds context to the event (an import names its run); never a person's fields."""
    person = Person(organization=org)
    apply_person(org, person, data)
    limits.check_room(org, limits.OTHER_RECORDS)
    person.save()
    audit(org, actor, "person.created", person, note)  # the Person ID is the reference
    return person


def unique_slug(org, kind, name, exclude=None):
    """name, then name-2, name-3 ... until (organization, kind, slug) is free; names never merge records."""
    base = slugify(name)[:200] or kind
    taken = Resource.objects.filter(organization=org, kind=kind)
    if exclude is not None:
        taken = taken.exclude(id=exclude.id)
    existing = set(taken.filter(slug__startswith=base).values_list("slug", flat=True))
    slug, n = base, 1
    while slug in existing:
        n += 1
        slug = f"{base}-{n}"
    return slug


DUPLICATE_NAME = "/problems/duplicate-name"


def warn_duplicate_name(org, kind, data, resource=None):
    """Same-named records are allowed but the API asks first: a 409 with
    type DUPLICATE_NAME unless data carries confirm_duplicate. A record keeping its own name is not a clash."""
    name = _text(data.get("name"))
    if not name or data.get("confirm_duplicate") or (resource is not None and resource.name.lower() == name.lower()):
        return
    clash = Resource.objects.filter(organization=org, kind=kind, name__iexact=name)
    if resource is not None:
        clash = clash.exclude(id=resource.id)
    other = clash.first()
    if other is not None:
        raise Conflict(
            f"Another {kind} is named {other.name}. Save anyway?",
            extra={"type": DUPLICATE_NAME, "duplicate_of": {"id": str(other.id), "name": other.name}},
        )


def create_extension(resource, **fields):
    """Exactly one extension row, matching kind. The only place extension rows are made. An
    integration can't exist without its two ends, so it needs source_resource and target_resource in fields. Every
    extension row carries its resource's organization, which composite foreign keys check."""
    if resource.kind == Kind.APPLICATION:
        return Application.objects.create(resource=resource, organization_id=resource.organization_id)
    if resource.kind == Kind.COMPONENT:
        return Component.objects.create(resource=resource, organization_id=resource.organization_id)
    if resource.kind == Kind.AI_AGENT:
        return AIAgent.objects.create(resource=resource, organization_id=resource.organization_id)
    if resource.kind == Kind.INTEGRATION:
        if not {"source_resource", "target_resource"} <= fields.keys():
            raise ValueError("An integration needs a source and a target.")
        return Integration.objects.create(resource=resource, organization_id=resource.organization_id, **fields)
    raise ValueError(f"No extension table exists for kind {resource.kind}.")


def check_one_extension(resource):
    """Raise unless the resource has its own kind's extension row and no other."""
    has = {kind: hasattr(resource, kind) for kind in Kind.values}
    if has != {kind: resource.kind == kind for kind in Kind.values}:
        raise ValueError(f"Resource {resource.id} must have exactly one extension row, matching kind {resource.kind}.")


@transaction.atomic
def create_resource(org, actor, data, import_run=None, slug=None):
    """import_run: the ImportRun committing this row (never from a request); the record and its creation event say
    so. slug: the record's slug when the caller names it (catalog-as-code keeps a file's slug; catalog_code/), already
    checked to be free; never taken from a request's data. Otherwise it is made from the name."""
    return _create(org, actor, data, data.get("kind") or Kind.APPLICATION, RECORD_KINDS, import_run, slug)


@transaction.atomic
def create_integration(org, actor, data, slug=None):
    """Integrations are created through their own API; any kind in data is ignored. slug: as for create_resource."""
    return _create(org, actor, data, Kind.INTEGRATION, [Kind.INTEGRATION], slug=slug)


def _create(org, actor, data, kind, allowed, import_run=None, slug=None):
    errors: dict[str, list[str]] = {}
    name = text_value(data.get("name"), "name", errors)
    if not name:
        errors.setdefault("name", ["Enter a name."])
    elif len(name) > NAME_MAX:
        errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
    lifecycle = data.get("lifecycle")
    if lifecycle not in Lifecycle.values:
        errors["lifecycle"] = ["Choose a lifecycle status."]
    if kind not in allowed:  # integrations are created through their own API
        errors["kind"] = ["Choose application, component or ai_agent."]
    if errors:
        raise FieldErrors(errors)
    resource = Resource(
        organization=org,
        kind=kind,
        name=name,
        slug=slug or unique_slug(org, kind, name),
        lifecycle=lifecycle,
        it_steward=actor,
        created_by_import=import_run,
    )
    _apply(org, resource, data, errors)
    custom = customfields.clean(org, kind, {}, data, errors, creating=True)  # defaults for what data leaves out
    if custom is not None:
        resource.custom_field_data = custom
    tag_names = _clean_tags(data, kind, errors)
    ends = _integration_ends(org, data, None, errors) if kind == Kind.INTEGRATION else {}
    if errors:
        raise FieldErrors(errors)
    if import_run is None:  # an import commit checks its whole batch first (imports/importer._write)
        limits.check_room(org, limits.RECORDS)
    resource.save()
    extension = create_extension(resource, **ends)
    models_used = _apply_extension(org, extension, data, errors)
    if errors:
        raise FieldErrors(errors)
    extension.save()
    if models_used is not None:
        set_models_used(org, resource, models_used)
    # Fresh, with every extension joined in one query, so the check below costs nothing more (an import makes
    # thousands of these in one request).
    resource = Resource.objects.select_related(*Kind.values).get(id=resource.id)
    check_one_extension(resource)
    created: dict[str, object] = {"name": name, "lifecycle": lifecycle}
    if import_run is not None:
        created["import_run"] = str(import_run.id)
    if tag_names:
        created["tags"] = tags.set_tags(org, resource, tag_names)[1]
    audit(org, actor, "resource.created", resource, created)
    if kind == Kind.INTEGRATION:
        staleness.integration_added(org, actor, resource)
    return resource


def _clean_tags(data, kind, errors):
    """Validated tag names when the request carries `tags` (records only), else None. Setting them is the caller's job."""
    if "tags" not in data or kind == Kind.INTEGRATION:
        return None
    return tags.clean(data["tags"], errors)


def audit_snapshot(resource):
    """Every editable field of a record, so resource.updated shows each change with before and after.
    References are stored as IDs; the audit view resolves them to current names."""
    ext = resource.extension
    if resource.kind == Kind.INTEGRATION:  # no business owner or criticality, and the ends are columns
        return {
            "name": resource.name,
            "description": resource.description,
            "lifecycle": resource.lifecycle,
            "technical_owner": str(resource.technical_owner_id) if resource.technical_owner_id else None,
            "support_team": str(resource.support_team_id) if resource.support_team_id else None,
            "it_steward": resource.it_steward_id,
            "data_classification": resource.data_classification,
            **{f: getattr(ext, f) for f in EXTENSION_TEXT[Kind.INTEGRATION] + EXTENSION_OTHER[Kind.INTEGRATION]},
            "data_categories": list(ext.data_categories),  # data-flow map, in choice order
            **integration_ends_audit(ext),
            **customfields.audit_values(resource.organization_id, resource.kind, resource.custom_field_data),
        }
    values = {
        **confirmed_snapshot(resource),
        "name": resource.name,
        "description": resource.description,
        "it_steward": resource.it_steward_id,
        "department": str(resource.department_id) if resource.department_id else None,
        "cost_center": resource.cost_center,
        "data_classification": resource.data_classification,
    }
    fields = EXTENSION_TEXT[resource.kind] + EXTENSION_BUILT.get(resource.kind, ()) + EXTENSION_OTHER[resource.kind]
    values.update({f: getattr(ext, f) for f in fields})
    if resource.kind == Kind.APPLICATION:
        values["vendor"] = str(ext.vendor_id) if ext.vendor_id else None
        values["category"] = str(ext.category_id) if ext.category_id else None
    elif resource.kind == Kind.COMPONENT:
        values["component_type"] = ext.component_type
    else:  # an AI agent: its channels in choice order, its models as IDs read now (not a prefetched list)
        values["channels"] = list(ext.channels)
        values["models_used"] = models_used_ids(resource)
    if compliance.enabled_for(resource.organization_id):
        values.update({f: compliance.as_text(getattr(resource, f)) or None for f in compliance.FIELDS})
    # custom_field_data.<name> per field that applies (catalog/customfields.py)
    values.update(customfields.audit_values(resource.organization_id, resource.kind, resource.custom_field_data))
    return values


STALE_VALUE = "/problems/stale-value"
UNDOABLE = {"business_owner", "technical_owner", "support_team", "criticality", "it_steward"}


class StaleValue(Conflict):
    """An expected-value check failed: someone changed the field since the caller read it."""

    def __init__(self, detail, fields):
        super().__init__(detail, extra={"type": STALE_VALUE, "fields": fields})
        self.fields = fields


def _current(resource, field):
    value = getattr(resource, f"{field}_id") if field != "criticality" else resource.criticality
    return str(value) if value not in (None, "") else None


def _check_expected(org, resource, expected):
    """Refuse when a field no longer holds the value the caller expects to replace (Undo). The message
    names who changed it and when, from the newest audit event that touched the field."""
    if not isinstance(expected, dict) or not set(expected) <= UNDOABLE:
        raise FieldErrors({"expected": [f"Only {', '.join(sorted(UNDOABLE))} can be checked."]})
    stale = [f for f, v in expected.items() if _current(resource, f) != (str(v) if v not in (None, "") else None)]
    if not stale:
        return
    labels = dict(CONFIRMED_FIELDS, it_steward="IT steward")
    events = AuditEvent.objects.filter(organization=org, target_id=resource.id, action="resource.updated")
    event = next((e for e in events.select_related("actor") if set(stale) & set(e.changes)), None)
    field = labels[stale[0]] if len(stale) == 1 else "these fields"
    if event is None:
        raise StaleValue(f"Not undone: {field} changed since.", stale)
    who = (event.actor.get_full_name() or event.actor.email) if event.actor else "Someone"
    at = event.created_at.astimezone(ZoneInfo(org.timezone)).strftime("%H:%M")
    raise StaleValue(f"Not undone: {who} changed {field} at {at}.", stale)


# Called after every record edit (update_resource), inside its transaction, with (org, resource). Other apps register
# theirs in AppConfig.ready (discovery: the record's "source differs" rows, discovery/drift.py).
RECORD_CHANGED: list = []


def on_record_changed(hook) -> None:
    if hook not in RECORD_CHANGED:
        RECORD_CHANGED.append(hook)


@transaction.atomic
def update_resource(org, actor, resource, data, audit_extra=None):
    """audit_extra: members added to the resource.updated event (bulk edit's marker); never taken from the request."""
    errors: dict[str, list[str]] = {}
    if "expected" in data:
        # Lock the row so the check and the write see the same values.
        resource = Resource.objects.select_for_update().get(id=resource.id)
        _check_expected(org, resource, data["expected"])
    elif customfields.KEY in data:
        # Two PATCHes of different custom fields merge onto the stored values one after the other, so neither is lost.
        locked = Resource.objects.select_for_update().only("custom_field_data").get(id=resource.id)
        resource.custom_field_data = locked.custom_field_data
    before = audit_snapshot(resource)
    if "name" in data:
        name = text_value(data.get("name"), "name", errors)
        if not name:
            errors.setdefault("name", ["Enter a name."])
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        else:
            resource.name = name
    if "lifecycle" in data:
        if data["lifecycle"] not in Lifecycle.values:
            errors["lifecycle"] = ["Choose a lifecycle status."]
        else:
            resource.lifecycle = data["lifecycle"]
    _apply(org, resource, data, errors)
    custom = customfields.clean(org, resource.kind, resource.custom_field_data, data, errors)  # merges by name
    if custom is not None:
        resource.custom_field_data = custom
    tag_names = _clean_tags(data, resource.kind, errors)
    extension = resource.extension
    models_used = _apply_extension(org, extension, data, errors)
    if resource.kind == Kind.INTEGRATION:
        for field, value in _integration_ends(org, data, extension, errors).items():
            setattr(extension, field, value)
    # The acknowledgment's types are checked on every edit, not only when the lifecycle becomes Retired.
    if "retirement_ack_reason" in data:
        text_value(data["retirement_ack_reason"], "retirement_ack_reason", errors)
    if data.get("retirement_ack") is not None and not isinstance(data["retirement_ack"], bool):
        errors["retirement_ack"] = ["Send true or false."]
    if errors:
        raise FieldErrors(errors)
    acknowledged = None
    if resource.lifecycle == Lifecycle.RETIRED and before["lifecycle"] != Lifecycle.RETIRED:
        acknowledged = retirement_check(org, resource, data)  # refused unless acknowledged with a reason
    resource.save()
    extension.save()
    if models_used is not None:
        set_models_used(org, resource, models_used)
    after = audit_snapshot(resource)
    changes: dict[str, object] = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if tag_names is not None:
        tags_before, tags_after = tags.set_tags(org, resource, tag_names)
        if tags_before != tags_after:  # identical tags change nothing
            changes["tags"] = [tags_before, tags_after]
    if acknowledged:
        changes["retirement_ack"] = acknowledged
    changes.update(audit_extra or {})
    if data.get("undo"):
        changes["undo"] = True
    audit(org, actor, "resource.updated", resource, changes)
    staleness.resource_changed(org, actor, resource, before, after)
    for hook in RECORD_CHANGED:
        hook(org, resource)
    return resource


def _apply(org, resource, data, errors):

    integration = resource.kind == Kind.INTEGRATION  # criticality and a business owner don't apply
    if "criticality" in data and not integration:
        value = _choice_input(data.get("criticality"))
        if value not in Criticality.values and value != "":
            errors["criticality"] = ["Choose low, medium, high or critical."]
        else:
            resource.criticality = value
    if "description" in data:
        description = text_value(data.get("description"), "description", errors)
        if len(description) > TEXT_MAX:
            errors["description"] = [f"Use {TEXT_MAX} characters or fewer."]
        elif "description" not in errors:
            resource.description = description
    for field in ("technical_owner",) if integration else ("business_owner", "technical_owner"):
        if field in data:
            setattr(resource, field, _in_org(Person, org, data[field], field, errors, active_only=True))
    if "support_team" in data:
        resource.support_team = _in_org(Team, org, data["support_team"], "support_team", errors)
    if "data_classification" in data:  # records and integrations
        value = _choice_input(data.get("data_classification"))
        if value not in DataClassification.values and value != "":
            errors["data_classification"] = [f"Use one of: {', '.join(DataClassification.values)}."]
        else:
            resource.data_classification = value
    if not integration:  # department, cost center and the compliance profile are for records only
        if "department" in data:
            resource.department = _in_org(Department, org, data["department"], "department", errors)
        if "cost_center" in data:
            cost_center = text_value(data.get("cost_center"), "cost_center", errors)
            if len(cost_center) > COST_CENTER_MAX:
                errors["cost_center"] = [f"Use {COST_CENTER_MAX} characters or fewer."]
            elif "cost_center" not in errors:
                resource.cost_center = cost_center
        for field, value in compliance.clean(org, data, errors, current=resource).items():
            setattr(resource, field, value)
    if "it_steward" in data:
        steward = data["it_steward"]
        user = None
        if steward not in (None, ""):
            user = steward_user(org, steward)
            if user is None:
                errors["it_steward"] = [STEWARD_MESSAGE]
        resource.it_steward = user


STEWARD_MESSAGE = "Choose an Admin or Editor in this organization."
BIGINT_MAX = 2**63 - 1


def user_id(value):
    """A user ID from request input: an integer, or a string of ASCII digits, within the bigint key range; else None
    (so bad input is a field error, never a database error)."""
    return whole_number(value, 1, BIGINT_MAX)


def steward_user(org, value):
    """The user `value` names if they are an Admin or Editor of org, else None. Single and bulk edits share it."""
    from django.contrib.auth import get_user_model

    uid = user_id(value)
    if uid is None:
        return None
    return (
        get_user_model().objects.filter(id=uid, membership__organization=org, membership__role__in=WRITE_ROLES).first()
    )


EXTENSION_CHOICES = {
    "built_by": BuiltBy,
    "license_model": LicenseModel,
    "component_type": ComponentType,
    "deployment_model": DeploymentModel,
    "authentication_method": AuthenticationMethod,
    "hosting": Hosting,
    "environment": Environment,
    "platform": AgentPlatform,
    "auth_mode": AgentAuthMode,
    "risk_classification": AgentRisk,
    "direction": IntegrationDirection,
    "auth_method": IntegrationAuthMethod,
}
EXTENSION_TEXT = {
    Kind.APPLICATION: (
        "purpose",
        "documentation_url",
        "support_url",
        "version",
        "primary_url",
        "admin_url",
        "entra_app_id",
        "service_principal_id",
    ),
    Kind.COMPONENT: (
        "purpose",
        "host_runtime",
        "repository_url",
        "documentation_url",
        "runbook_url",
        "schedule_or_trigger",
        "azure_resource_id",
        "pp_environment_id",
        "external_id",
    ),
    Kind.INTEGRATION: ("data_purpose", "protocol", "schedule", "documentation_url", "monitoring_url"),
    Kind.AI_AGENT: (
        "purpose",
        "pp_environment_id",
        "knowledge_sources",
        "tools_connectors",
        "run_as_identity",
        "data_access_scope",
        "repo_url",
        "external_id",
    ),
}
# Applications and components say who built them and how they are licensed; AI agents don't.
EXTENSION_BUILT = {Kind.APPLICATION: ("built_by", "license_model"), Kind.COMPONENT: ("built_by", "license_model")}
# Choice and yes/no fields each kind has besides built_by and license_model, for the audit payload.
EXTENSION_OTHER = {
    Kind.APPLICATION: ("deployment_model", "authentication_method", "sso_enabled", "scim_provisioning"),
    Kind.COMPONENT: ("hosting", "environment"),
    Kind.INTEGRATION: ("direction", "auth_method"),
    Kind.AI_AGENT: (
        "platform",
        "environment",
        "auth_mode",
        "human_in_loop",
        "actions_can_write",
        "risk_classification",
    ),
}
# null: not recorded (an agent's human_in_loop: no decision yet)
EXTENSION_BOOLS = ("sso_enabled", "scim_provisioning", "human_in_loop", "actions_can_write")
# The column sizes of the short text fields.
SHORT_TEXT_MAX = {
    "host_runtime": 200,
    "protocol": 100,
    "schedule": 200,
    "version": 50,
    "schedule_or_trigger": 200,
    "azure_resource_id": 500,
    "pp_environment_id": 64,
    "external_id": 200,
    "run_as_identity": 200,
}
# Long text fields, kept under TEXT_MAX.
LONG_TEXT = {"purpose", "data_purpose", "knowledge_sources", "tools_connectors", "data_access_scope"}
MODELS_USED_MAX = 50
# URL fields and their column sizes: the older columns are 200 characters, the others 2,000 (admin portals and
# SharePoint links run long).
URL_MAX = {
    "documentation_url": 200,
    "support_url": 200,
    "repository_url": 200,
    "primary_url": 2000,
    "admin_url": 2000,
    "runbook_url": 2000,
    "repo_url": 2000,
    "monitoring_url": 2000,
}
EXTENSION_URLS = set(URL_MAX)
# Entra IDs are GUIDs: the app registration's application (client) ID and the service principal's object ID.
GUID_FIELDS = {"entra_app_id", "service_principal_id"}
NAME_MAX = 200  # Resource.name, and the URL and host_runtime columns, are 200 characters
COST_CENTER_MAX = 50
EVIDENCE_MAX = 500  # the evidence and inactive_reason columns
TEXT_MAX = 5000  # description and purpose are unbounded columns; this keeps one record from holding a book


def _text(value):
    """A stripped string; anything that isn't text counts as empty. Only for values already known to be text or
    absent; a request body's text field goes through core.input.text_value, which refuses other types."""
    return value.strip() if isinstance(value, str) else ""


def _choice_input(value):
    """A choice field from a request body: null clears it like "", and anything else is checked against the choices,
    so an empty list or object is refused rather than read as empty."""
    return "" if value is None else value


EXTENSION_KINDS = {
    Application: Kind.APPLICATION,
    Component: Kind.COMPONENT,
    Integration: Kind.INTEGRATION,
    AIAgent: Kind.AI_AGENT,
}


def _apply_extension(org, extension, data, errors):
    """Kind-specific fields. Fields that belong to another kind are ignored, not stored. Returns an AI agent's
    models_used (AIModel rows) when data names them, else None: the links are written after the extension row is
    saved (set_models_used)."""
    kind = EXTENSION_KINDS[type(extension)]
    for field in EXTENSION_TEXT[kind]:
        if field not in data:
            continue
        value = text_value(data.get(field), field, errors)
        if field in errors:
            continue
        if field in EXTENSION_URLS:
            if len(value) > URL_MAX[field]:
                errors[field] = [f"Use {URL_MAX[field]:,} characters or fewer."]
                continue
            try:
                if value:
                    URLValidator(schemes=["http", "https"])(value)
            except ValidationError:
                errors[field] = ["Enter a full web address starting with http:// or https://."]
                continue
        elif field in GUID_FIELDS:
            if value and not is_uuid(value):
                errors[field] = ["Enter the ID as a GUID, such as 00000000-0000-0000-0000-000000000000."]
                continue
            value = value.lower()
        elif field in SHORT_TEXT_MAX and len(value) > SHORT_TEXT_MAX[field]:
            errors[field] = [f"Use {SHORT_TEXT_MAX[field]} characters or fewer."]
            continue
        elif field in LONG_TEXT and len(value) > TEXT_MAX:
            errors[field] = [f"Use {TEXT_MAX} characters or fewer."]
            continue
        setattr(extension, field, value)
    for field, choices in EXTENSION_CHOICES.items():
        if field in data and hasattr(extension, field):
            value = _choice_input(data.get(field))
            if value == "" and field == "component_type":
                value = ComponentType.OTHER
            if value not in choices.values and not (value == "" and field != "component_type"):
                errors[field] = ["Choose one of the listed values."]
            else:
                setattr(extension, field, value)
    for field in EXTENSION_BOOLS:
        if field in data and hasattr(extension, field):
            setattr(extension, field, yes_no(data[field], field, errors))
    if kind == Kind.APPLICATION and "vendor" in data:
        extension.vendor = _in_org(Vendor, org, data["vendor"], "vendor", errors)
    if kind == Kind.APPLICATION and "category" in data:
        extension.category = _in_org(SoftwareCategory, org, data["category"], "category", errors)
    if kind == Kind.INTEGRATION and "data_categories" in data:
        categories = data_categories(data["data_categories"], errors)
        if categories is not None:
            extension.data_categories = categories
    if kind != Kind.AI_AGENT:
        return None
    if "channels" in data:
        channels = _channels(data["channels"], errors)
        if channels is not None:
            extension.channels = channels
    return _models_used(org, data["models_used"], errors) if "models_used" in data else None


def _channels(value, errors):
    """An agent's channels: a list of AgentChannel values, kept in choice order and once each; null clears them. None,
    with a field error, for anything else, a string included (agents aren't imported from CSV yet; when they are, the
    importer splits its "teams; web" cell into a list first)."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors["channels"] = ["Send channels as a list."]
        return None
    unknown = [v for v in value if v not in AgentChannel.values]
    if unknown:
        errors["channels"] = [f"Use any of: {', '.join(AgentChannel.values)}."]
        return None
    return [c for c in AgentChannel.values if c in value]


def data_categories(value, errors):
    """An integration's data categories (data-flow map): a list of DataCategory values, kept in choice order
    and once each; null or an empty list clears them. None, with a field error, for anything else."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors["data_categories"] = ["Send data categories as a list."]
        return None
    unknown = [v for v in value if v not in DataCategory.values]
    if unknown:
        errors["data_categories"] = [f"Use any of: {', '.join(DataCategory.values)}."]
        return None
    return [c for c in DataCategory.values if c in value]


def _models_used(org, value, errors):
    """The AI models a list of IDs names, in this organization, once each; another organization's ID is unknown."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors["models_used"] = ["Send models as a list of AI model IDs."]
        return None
    ids = list(dict.fromkeys(value))
    if len(ids) > MODELS_USED_MAX:
        errors["models_used"] = [f"Choose {MODELS_USED_MAX} models or fewer."]
        return None
    found = {str(m.id): m for m in AIModel.objects.filter(organization=org, id__in=[i for i in ids if is_uuid(i)])}
    if len(found) != len(ids):
        errors["models_used"] = ["Choose existing AI models."]
        return None
    return [found[i] for i in ids]


def models_used_ids(resource):
    """An agent's model IDs as sorted strings, read from the database (the audit payload's before and after)."""
    return sorted(str(i) for i in AgentModel.objects.filter(agent_id=resource.pk).values_list("model_id", flat=True))


def set_models_used(org, resource, models):
    """Make the agent's models exactly `models`. Its audit snapshot lists them, and bulk_create sends no signal, so
    the record's "before" is read first (snapshots.touch)."""
    snapshots.touch(resource)
    wanted = {m.id for m in models}
    AgentModel.objects.filter(agent_id=resource.pk).exclude(model_id__in=wanted).delete()
    have = set(AgentModel.objects.filter(agent_id=resource.pk).values_list("model_id", flat=True))
    AgentModel.objects.bulk_create(
        [AgentModel(organization=org, agent_id=resource.pk, model=m) for m in models if m.id not in have]
    )


def _instance_fields(application, data, instance=None):
    """Validate instance input; only keys present in data are returned. A name clash is a field error."""
    errors: dict[str, list[str]] = {}
    values: dict[str, object] = {}
    if "name" in data or instance is None:
        name = text_value(data.get("name"), "name", errors)
        if not name:
            errors.setdefault("name", ["Enter a name."])
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        else:
            values["name"] = name
            clash = ApplicationInstance.objects.filter(application=application, name__iexact=name)
            if instance is not None:
                clash = clash.exclude(id=instance.id)
            if clash.exists():
                errors["name"] = [f'An instance named "{name}" already exists for {application.name}.']
    if "environment" in data:
        values["environment"] = environment = text_value(data.get("environment"), "environment", errors)
        if len(environment) > 100:
            errors["environment"] = ["Use 100 characters or fewer."]
    if "url" in data:
        url = text_value(data.get("url"), "url", errors)
        if len(url) > NAME_MAX:
            errors["url"] = [f"Use {NAME_MAX} characters or fewer."]
        try:
            if url and "url" not in errors:
                URLValidator(schemes=["http", "https"])(url)
            values["url"] = url
        except ValidationError:
            errors["url"] = ["Enter a full web address starting with http:// or https://."]
    if "status" in data or instance is None:
        status = data.get("status")
        if status in (None, ""):  # only null or "" mean the default; [] or {} is refused, not read as "unknown"
            status = InstanceStatus.UNKNOWN
        if status not in InstanceStatus.values:
            errors["status"] = ["Choose operational, degraded, offline or unknown."]
        else:
            values["status"] = status
    if errors:
        raise FieldErrors(errors)
    return values


@transaction.atomic
def archive_resource(org, actor, resource):
    """Sets the archived flag only; lifecycle and review state are untouched. No-op if already archived."""
    if resource.archived_at is None:
        resource.archived_at = timezone.now()
        resource.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "resource.archived", resource, {"kind": resource.kind, "name": resource.name})
    return resource


@transaction.atomic
def restore_resource(org, actor, resource):
    if resource.archived_at is not None:
        resource.archived_at = None
        resource.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "resource.restored", resource, {"kind": resource.kind, "name": resource.name})
    return resource


# The noun a refusal uses for each reverse accessor; any other model is named by its verbose_name, so a new table
# that points at a resource or an instance (E9's integrations are "integration") blocks the delete with no change
# here. Map an accessor only when the model's own name would read badly.
REFERENCE_NOUNS = {"reviews": "review", "instances": "instance", "renewal_coverage": "renewal cycle"}
# Rows a record owns rather than references: they go with it (attachments, exceptions and end-of-life
# tracking each audited, tag links in the record's own deleted event) instead of blocking its delete. A record's and
# an instance's end-of-life tracking share the accessor `eol_tracking` (eol/models.py).
OWNED_ACCESSORS = {"attachments", "quality_exceptions", "tag_links", "eol_tracking"}


def references_to_many(objs):
    """{obj.pk: {noun: count}} for objects of one model: every row that points at each, read with one query per
    referencing foreign key however many objects there are (bulk delete's preview). Extension rows (1:1, same record)
    don't count, nor does a many-to-many with an explicit through model (the through table's own foreign key is
    counted, so a renewal cycle covering a record counts once, and a tag is the record's own link row); one with an
    auto-created through table is counted, so it blocks rather than cascading unaudited. A model with two foreign keys to the same
    object (a relationship's source and target) counts each row once."""
    objs = list(objs)
    if not objs:
        return {}
    ids = [o.pk for o in objs]
    by_model: dict = {}
    for rel in type(objs[0])._meta.related_objects:
        if rel.one_to_one or rel.get_accessor_name() in OWNED_ACCESSORS:
            continue
        if rel.many_to_many and not rel.through._meta.auto_created:
            continue  # its explicit through model's own foreign key is counted (or owned) instead
        by_model.setdefault(rel.related_model, []).append(rel)
    found: dict = {pk: {} for pk in ids}
    for model, rels in by_model.items():
        rows: dict = {}
        for rel in rels:
            name = rel.field.name
            for row_pk, target in model._default_manager.filter(**{f"{name}__in": ids}).values_list("pk", name):
                rows.setdefault(target, set()).add(row_pk)
        noun = REFERENCE_NOUNS.get(rels[0].get_accessor_name() or "", str(model._meta.verbose_name))
        for target, pks in rows.items():
            found[target][noun] = len(pks)
    # stable message order: reviews first, then by noun
    return {pk: dict(sorted(refs.items(), key=lambda kv: (kv[0] != "review", kv[0]))) for pk, refs in found.items()}


def references_to(obj):
    """Every row that points at obj, as {noun: count} (references_to_many for one object)."""
    return references_to_many([obj])[obj.pk]


def resource_references(resource):
    return references_to(resource)


def references_phrase(refs):
    """'2 instances and 3 relationships' for {noun: count}."""
    parts = [f"{n} {noun}{'s' if n != 1 else ''}" for noun, n in refs.items()]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def refuse_while_referenced(obj, refs, then):
    """Raise the 409 for a delete blocked by refs; `then` says what to do instead."""
    if refs:
        raise Conflict(
            f"{obj.name} can't be deleted because it has {references_phrase(refs)}. {then}",
            extra={"blocking_references": refs},
        )


RESOURCE_DELETE_INSTEAD = "Archive it instead."


@transaction.atomic
def delete_resource(org, actor, resource, audit_extra=None):
    """Hard delete (Admin only): refused while any review, instance, relationship or other record references
    it. Its attachments, data-quality exceptions and end-of-life tracking go with it (each audited, files retired for
    the sweep), and so do its tag links. Audit events are not foreign-keyed to the target, so they outlive it. audit_extra: members added to
    the resource.deleted event (bulk delete's marker); never taken from the request."""
    from eol import services as eol_services

    from . import attachments, quality

    refuse_while_referenced(resource, resource_references(resource), RESOURCE_DELETE_INSTEAD)
    attachments.remove_for(org, actor, resource)
    quality.remove_for(org, actor, resource)
    eol_services.untrack(org, actor, resource)  # its end-of-life tracking goes with it
    audit(
        org, actor, "resource.deleted", resource, {"kind": resource.kind, "name": resource.name, **(audit_extra or {})}
    )
    resource.delete()


@transaction.atomic
def create_instance(org, actor, application, data):
    values = _instance_fields(application, data)
    instance = ApplicationInstance(organization=org, application=application, **values)
    instance.save()
    audit(org, actor, "instance.created", instance, {"application": str(application.id), **values})
    return instance


@transaction.atomic
def update_instance(org, actor, instance, data):
    values = _instance_fields(instance.application, data, instance)
    changes = {k: [getattr(instance, k), v] for k, v in values.items() if getattr(instance, k) != v}
    for k, v in values.items():
        setattr(instance, k, v)
    instance.save()
    audit(org, actor, "instance.updated", instance, {"application": str(instance.application_id), **changes})
    return instance


@transaction.atomic
def delete_instance(org, actor, instance):
    # Relationships and integrations that name this instance block the delete.
    from eol import services as eol_services

    refs = references_to(instance)
    refuse_while_referenced(instance, refs, f"Remove or change {'it' if sum(refs.values()) == 1 else 'them'} first.")
    eol_services.untrack_instance(org, actor, instance)  # its own end-of-life version goes with it
    audit(
        org, actor, "instance.deleted", instance, {"application": str(instance.application_id), "name": instance.name}
    )
    instance.delete()


@transaction.atomic
def record_review(org, actor, resource, data, today=None):
    today = today or org_today(org)
    errors: dict[str, list[str]] = {}
    person = _in_org(Person, org, data.get("confirmed_by"), "confirmed_by", errors)
    if person is None and "confirmed_by" not in errors:
        errors["confirmed_by"] = ["Choose who confirmed it."]
    inactive_reason = text_value(data.get("inactive_reason"), "inactive_reason", errors)
    if person is not None and not person.active and not inactive_reason:
        errors.setdefault("inactive_reason", ["Give a reason for using an inactive person."])
    elif len(inactive_reason) > EVIDENCE_MAX:
        errors["inactive_reason"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    evidence = text_value(data.get("evidence"), "evidence", errors)
    if not evidence:
        errors.setdefault("evidence", ["Describe how you got the confirmation."])
    elif len(evidence) > EVIDENCE_MAX:
        errors["evidence"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    try:
        confirmation_date = dates.parse_stored(data.get("confirmation_date") or today.isoformat())
        if confirmation_date > today:
            errors["confirmation_date"] = ["The confirmation date can't be in the future."]
    except ValueError as exc:
        errors["confirmation_date"] = [str(exc)]
        confirmation_date = today
    # The confirmation date is in the window and the interval at most a few years, so this always has a date.
    default_next = dates.shift(confirmation_date, org.review_interval_days, "days") or today
    try:
        next_review = dates.parse_stored(data.get("next_review_date") or default_next.isoformat())
        if next_review <= confirmation_date:
            errors["next_review_date"] = ["The next review must be after the confirmation date."]
    except ValueError as exc:
        errors["next_review_date"] = [str(exc)]
        next_review = default_next
    if errors:
        raise FieldErrors(errors)
    review = RecordReview.objects.create(
        organization=org,
        resource=resource,
        confirmed_by=person,
        inactive_reason=inactive_reason,
        recorded_by=actor,
        confirmation_date=confirmation_date,
        next_review_date=next_review,
        evidence=evidence,
        confirmed_values=confirmed_snapshot(resource),
    )
    audit(org, actor, "review.recorded", review, {"review": str(review.id), "confirmed_by": str(person.id)})
    return review


# ---- Retirement with dependents -------------------------------------------------------------

RETIREMENT_NEEDS_ACK = "/problems/retirement-needs-acknowledgment"
NAMES_IN_MESSAGE = 5


def dependents_of(org, resource):
    """The records that depend on resource through a dependency type (`depends_on`, or an added type with
    `is_dependency`), once each, as (record, why not) pairs where
    why not is None for an active one (Active or Deprecated and not archived) and otherwise "archived" or the
    lifecycle that keeps it from needing acknowledgment. Direct dependents only; a record linked to itself through
    two instances isn't its own dependent."""
    links = (
        Relationship.objects.filter(organization=org, type__is_dependency=True, target=resource)
        .exclude(source=resource)
        .select_related("source")
        .order_by("source__name", "source_id")
    )
    found: dict = {}
    for link in links:
        record = link.source
        if record.id in found:
            continue
        if record.archived_at is not None:
            why_not = "archived"
        elif record.lifecycle not in ACTIVE_LIFECYCLES:
            why_not = record.lifecycle
        else:
            why_not = None
        found[record.id] = (record, why_not)
    return list(found.values())


def retirement_check(org, resource, data):
    """The retirement acknowledgment rule, for a record about to be Retired (or a retirement assessment about to be Ready for external
    execution). With no active dependents it returns None. With some, the request must carry `retirement_ack: true`
    and a `retirement_ack_reason`: without the acknowledgment it is refused with a 409 listing them (type
    /problems/retirement-needs-acknowledgment) and without the reason with a field error. Returns what to audit:
    {"reason", "dependents": [IDs]}. Links are never removed; this only gates the lifecycle change."""
    found = dependents_of(org, resource)
    active = [record for record, why_not in found if why_not is None]
    if not active:
        return None
    if data.get("retirement_ack") is not True:
        names = ", ".join(r.name for r in active[:NAMES_IN_MESSAGE]) + (
            f" and {len(active) - NAMES_IN_MESSAGE} more" if len(active) > NAMES_IN_MESSAGE else ""
        )
        raise Conflict(
            f"{resource.name} is still depended on by {len(active)} active "
            f"{'record' if len(active) == 1 else 'records'}: {names}. Acknowledge them and give a reason to continue.",
            extra={
                "type": RETIREMENT_NEEDS_ACK,
                "dependents": [
                    {"id": str(r.id), "name": r.name, "kind": r.kind, "lifecycle": r.lifecycle} for r in active
                ],
                "not_required": [
                    {"id": str(r.id), "name": r.name, "kind": r.kind, "reason": why_not}
                    for r, why_not in found
                    if why_not is not None
                ],
            },
        )
    problems: dict[str, list[str]] = {}
    reason = text_value(data.get("retirement_ack_reason"), "retirement_ack_reason", problems)
    if problems:
        raise FieldErrors(problems)
    if not reason:
        raise FieldErrors({"retirement_ack_reason": ["Give a reason for retiring it while records depend on it."]})
    if len(reason) > TEXT_MAX:
        raise FieldErrors({"retirement_ack_reason": [f"Use {TEXT_MAX} characters or fewer."]})
    return {"reason": reason, "dependents": [str(r.id) for r in active]}


# ---- Relationships ---------------------------------------------------------------

APPLICATION_OR_COMPONENT = {Kind.APPLICATION, Kind.COMPONENT}
RECORD_ENDS = set(RECORD_KINDS)  # applications, components and AI agents
# Endpoint matrix: the kinds each core type accepts at its source and target, by slug. `replaces` also needs both
# ends to be the same kind, checked separately. An integration is never a `depends_on` endpoint; it takes part through
# `implements` here and through its own source and target columns (E9). An AI agent depends on what it reads or acts
# on; integration ends stay applications and components. A type an Admin added follows the
# `depends_on` rules (ADDED_TYPE_ENDS).
ENDPOINT_MATRIX: dict[str, tuple[set, set]] = {
    RelationshipKind.DEPENDS_ON: (RECORD_ENDS, RECORD_ENDS),
    RelationshipKind.IMPLEMENTS: ({Kind.COMPONENT}, {Kind.INTEGRATION}),
    RelationshipKind.REPLACES: (RECORD_ENDS, RECORD_ENDS),
}
ADDED_TYPE_ENDS = (RECORD_ENDS, RECORD_ENDS)
KIND_WORDS = {
    Kind.APPLICATION: "an application",
    Kind.COMPONENT: "a component",
    Kind.AI_AGENT: "an AI agent",
    Kind.INTEGRATION: "an integration",
}
DUPLICATE_RELATIONSHIP = "/problems/duplicate-relationship"


def _kinds_phrase(kinds):
    words = [KIND_WORDS[k] for k in KIND_WORDS if k in kinds]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def relationship_audit_values(rel):
    """The fields of a link as audit payload values; references are IDs, the type by its slug (it never
    changes)."""
    return {
        "type": rel.type.slug,
        "source": str(rel.source_id),
        "source_instance": str(rel.source_instance_id) if rel.source_instance_id else None,
        "target": str(rel.target_id),
        "target_instance": str(rel.target_instance_id) if rel.target_instance_id else None,
        "purpose": rel.purpose,
    }


def _replaces_cycle(org, source, target, exclude=None):
    """True if source replacing target would close a loop: target already replaces source, directly or through
    other records (or they are the same record). Instances don't matter; `replaces` is between records."""
    if source.id == target.id:
        return True
    links = Relationship.objects.filter(organization=org, type__slug=RelationshipKind.REPLACES)
    if exclude is not None:
        links = links.exclude(id=exclude.id)
    replaced_by: dict = {}
    for src, tgt in links.values_list("source_id", "target_id"):
        replaced_by.setdefault(src, []).append(tgt)
    seen, stack = set(), [target.id]
    while stack:
        node = stack.pop()
        if node == source.id:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(replaced_by.get(node, []))
    return False


def _instance_in(org, value, resource, field, errors):
    """An instance of this resource in this organization; instances belong to applications only."""
    if value in (None, ""):
        return None
    instance = ApplicationInstance.objects.filter(organization=org, id=value).first() if is_uuid(value) else None
    if instance is None:
        errors[field] = ["Choose an existing instance."]
    elif resource is not None and instance.application_id != resource.id:
        errors[field] = [f"This instance doesn't belong to {resource.name}."]
    return instance


def _relationship_fields(org, data, rel=None):
    """Validate input for a link, checking the merged result so a PATCH is judged as a whole. Returns the values to
    set. Another organization's IDs are reported as unknown."""
    errors: dict[str, list[str]] = {}
    values: dict = {}
    rtype = rel.type if rel is not None and "type" not in data else None
    if "type" in data or rel is None:
        rtype = relationship_types.for_link(org, data.get("type"))
        if rtype is None:  # another organization's type is unknown too
            names = RelationshipType.objects.filter(organization=org).order_by("name").values_list("slug", flat=True)
            errors["type"] = [f"Choose one of the organization's relationship types: {', '.join(names)}."]
    rel_type = rtype.slug if rtype is not None else None
    for end in ("source", "target"):
        if end in data:
            if data[end] in (None, ""):
                errors[end] = ["Choose a record."]
            values[end] = _in_org(Resource, org, data[end], end, errors)
        elif rel is None:
            errors[end] = ["Choose a record."]
        else:
            values[end] = getattr(rel, end)
    if "purpose" in data:
        purpose = text_value(data["purpose"], "purpose", errors)
        if len(purpose) > TEXT_MAX:
            errors["purpose"] = [f"Use {TEXT_MAX} characters or fewer."]
        values["purpose"] = purpose
    elif rel is None:
        values["purpose"] = ""
    for end in ("source", "target"):
        field, record = f"{end}_instance", values.get(end)
        if field in data:
            values[field] = _instance_in(org, data[field], record, field, errors)
        elif rel is not None:
            values[field] = getattr(rel, field)
            # Changing the record without naming a new instance would leave an instance of the old one behind.
            if values[field] is not None and record is not None and values[field].application_id != record.id:
                errors[field] = [f"This instance doesn't belong to {record.name}."]
        else:
            values[field] = None
    source, target = values.get("source"), values.get("target")
    if rtype is not None and source is not None and target is not None and not errors:
        source_kinds, target_kinds = (
            ENDPOINT_MATRIX.get(rtype.slug, ADDED_TYPE_ENDS) if rtype.is_core else ADDED_TYPE_ENDS
        )
        if source.kind not in source_kinds:
            errors["source"] = [f"The source of this link must be {_kinds_phrase(source_kinds)}."]
        if target.kind not in target_kinds:
            errors["target"] = [f"The target of this link must be {_kinds_phrase(target_kinds)}."]
        replaces = rtype.is_core and rel_type == RelationshipKind.REPLACES
        if replaces and not errors and source.kind != target.kind:
            words = KIND_WORDS[source.kind]  # "an AI agent"
            errors["target"] = [f"{words[0].upper()}{words[1:]} can only replace another {words.split(' ', 1)[1]}."]
        if not errors and source.id == target.id and not replaces:
            a, b = values["source_instance"], values["target_instance"]
            if a is None or b is None or a.id == b.id:
                errors["target"] = ["A record can only link to itself through two different instances."]
        if not errors and replaces and _replaces_cycle(org, source, target, rel):
            errors["target"] = ["This would make a record replace itself, directly or through other records."]
    if errors:
        raise FieldErrors(errors)
    values["type"] = rtype
    return values


def _already_recorded(extra=None):
    return Conflict("This link is already recorded.", extra={"type": DUPLICATE_RELATIONSHIP, **(extra or {})})


def _refuse_duplicate(org, values, rel=None):
    clash = Relationship.objects.filter(
        organization=org,
        type=values["type"],
        source=values["source"],
        source_instance=values["source_instance"],
        target=values["target"],
        target_instance=values["target_instance"],
    )
    if rel is not None:
        clash = clash.exclude(id=rel.id)
    other = clash.first()
    if other is not None:
        raise _already_recorded({"duplicate_of": {"id": str(other.id)}})


@transaction.atomic
def create_relationship(org, actor, data):
    values = _relationship_fields(org, data)
    _refuse_duplicate(org, values)
    rel = Relationship(organization=org, created_by=actor, **values)
    limits.check_room(org, limits.OTHER_RECORDS)
    try:
        with transaction.atomic():
            rel.save()
    except IntegrityError:  # a concurrent identical request won the unique index
        raise _already_recorded() from None
    audit(org, actor, "relationship.created", rel, relationship_audit_values(rel))
    staleness.relationship_added(org, actor, rel)
    return rel


@transaction.atomic
def update_relationship(org, actor, rel, data):
    values = _relationship_fields(org, data, rel)
    _refuse_duplicate(org, values, rel)
    before = relationship_audit_values(rel)
    for field, value in values.items():
        setattr(rel, field, value)
    try:
        with transaction.atomic():
            rel.save()
    except IntegrityError:
        raise _already_recorded() from None
    after = relationship_audit_values(rel)
    audit(org, actor, "relationship.updated", rel, {k: [before[k], after[k]] for k in after if before[k] != after[k]})
    staleness.relationship_changed(org, actor, rel, before, after)
    return rel


@transaction.atomic
def delete_relationship(org, actor, rel):
    """Removing a link on purpose; the audit event keeps what it was."""
    audit(
        org,
        actor,
        "relationship.deleted",
        rel,
        {**relationship_audit_values(rel), "confirmations": rel.confirmations.count()},
    )
    staleness.relationship_removed(org, actor, rel)  # while the snapshot links still point at it
    rel.delete()  # its confirmations go with it


# ---- Integrations ---------------------------------------------------------------------
# An integration is a Resource with kind=integration and an Integration row. It carries a technical owner, a support
# team and an IT steward; criticality and business owner don't apply, and it has no record review.


def integration_ends_audit(integration):
    """The ends of an integration as audit payload values; references are IDs."""
    return {
        "source": str(integration.source_resource_id),
        "source_instance": str(integration.source_instance_id) if integration.source_instance_id else None,
        "target": str(integration.target_resource_id),
        "target_instance": str(integration.target_instance_id) if integration.target_instance_id else None,
    }


def _integration_ends(org, data, integration, errors):
    """Validate the source and target (and their optional instances), checking the merged result so a PATCH is
    judged as a whole. Returns the Integration column values; problems go into errors. Another organization's IDs
    are reported as unknown."""
    values: dict = {}
    for end in ("source", "target"):
        if end in data:
            if data[end] in (None, ""):
                errors[end] = ["Choose a record."]
            values[f"{end}_resource"] = _in_org(Resource, org, data[end], end, errors)
        elif integration is None:
            errors[end] = ["Choose a record."]
        else:
            values[f"{end}_resource"] = getattr(integration, f"{end}_resource")
    for end in ("source", "target"):
        field, record = f"{end}_instance", values.get(f"{end}_resource")
        if field in data:
            values[field] = _instance_in(org, data[field], record, field, errors)
        elif integration is not None:
            values[field] = getattr(integration, field)
            # Changing the record without naming a new instance would leave an instance of the old one behind.
            if values[field] is not None and record is not None and values[field].application_id != record.id:
                errors[field] = [f"This instance doesn't belong to {record.name}."]
        else:
            values[field] = None
    ends = ("source", "target", "source_instance", "target_instance")
    source, target = values.get("source_resource"), values.get("target_resource")
    if source is not None and target is not None and not any(end in errors for end in ends):
        for end, record in (("source", source), ("target", target)):
            if record.kind not in APPLICATION_OR_COMPONENT:
                errors[end] = [f"The {end} of an integration must be {_kinds_phrase(APPLICATION_OR_COMPONENT)}."]
        if not any(end in errors for end in ends) and source.id == target.id:
            a, b = values["source_instance"], values["target_instance"]
            if a is None or b is None or a.id == b.id:
                errors["target"] = ["A record can only link to itself through two different instances."]
    return values


def update_integration(org, actor, resource, data):
    return update_resource(org, actor, resource, data)


# ---- Link confirmations -------------------------------------------------------------------


@transaction.atomic
def confirm_link(org, actor, link, data, today=None):
    """Record that a Person confirmed a relationship or an integration (link is a Relationship, or the Integration
    row) is still accurate. Stores who, when, the evidence and the signature as it is now (endpoints, instances,
    type, owner), so a later change makes the link Needs verification. Like a record review it takes a
    Person, a recording User and evidence, but it has no next-review date: the interval is organization-wide."""
    today = today or org_today(org)
    errors: dict[str, list[str]] = {}
    person = _in_org(Person, org, data.get("confirmed_by"), "confirmed_by", errors)
    if person is None and "confirmed_by" not in errors:
        errors["confirmed_by"] = ["Choose who confirmed it."]
    reason = text_value(data.get("inactive_reason"), "inactive_reason", errors)
    if person is not None and not person.active and not reason:
        errors.setdefault("inactive_reason", ["Give a reason for using an inactive person."])
    evidence = text_value(data.get("evidence"), "evidence", errors)
    if not evidence:
        errors.setdefault("evidence", ["Describe how you got the confirmation."])
    elif len(evidence) > EVIDENCE_MAX:
        errors["evidence"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    if len(reason) > EVIDENCE_MAX:
        errors["inactive_reason"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    confirmed_on = today
    if data.get("confirmed_on") not in (None, ""):
        try:
            confirmed_on = dates.parse_stored(data["confirmed_on"])
        except ValueError as exc:
            confirmed_on = None
            errors["confirmed_on"] = [str(exc)]
        if confirmed_on is not None and confirmed_on > today:
            errors["confirmed_on"] = ["The confirmation date can't be in the future."]
    if errors:
        raise FieldErrors(errors)
    if isinstance(link, Relationship):
        owner = Resource.objects.get(organization=org, id=link.source_id).technical_owner_id
        signature = relationship_signature(link, owner)
        owner_of = {"relationship": link}
    else:
        resource = Resource.objects.get(organization=org, id=link.resource_id)
        signature = integration_signature(link, resource.technical_owner_id)
        owner_of = {"integration": link}
    confirmation = LinkConfirmation.objects.create(
        organization=org,
        confirmed_by=person,
        inactive_reason=reason,
        recorded_by=actor,
        confirmed_on=confirmed_on,
        evidence=evidence,
        confirmed_signature=signature,
        **owner_of,
    )
    # The confirmation is the target; its link (the relationship, or the integration's record) is the related target,
    # so the link's History shows it.
    audit(
        org,
        actor,
        "link.confirmed",
        confirmation,
        {
            "link": "relationship" if isinstance(link, Relationship) else "integration",
            # The ends, so the event stays findable and readable if the link is later removed.
            **(
                {"type": link.type.slug, "source": str(link.source_id), "target": str(link.target_id)}
                if isinstance(link, Relationship)
                else {}
            ),
            "confirmation": str(confirmation.id),
            "confirmed_by": str(person.id),
            "confirmed_on": confirmed_on.isoformat(),
        },
    )
    return confirmation
