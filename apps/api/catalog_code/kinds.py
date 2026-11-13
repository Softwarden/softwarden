"""The kinds a catalog-as-code tree holds: their fields, natural keys, file names, and the service functions that
write them (validation, organization scope and the audit event are the services'; nothing here saves a model).

Order matters twice: ORDER is the order files are applied in (a file can name what an earlier kind created), and
PRUNE_ORDER the order rows missing from the tree are deleted in (what references a row goes first)."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from django.utils.text import slugify

from catalog import admin_services, ai, customfields, relationship_types, services, tags
from catalog.models import (
    CUSTOM_FIELD_KINDS,
    AIProvider,
    Department,
    Kind,
    SoftwareCategory,
    Tag,
    Team,
    Vendor,
)
from core import capabilities as caps
from core.problems import FieldErrors

from . import fields as F
from .fields import Field, f
from .fmt import Path

# Windows can't create these as file names, with any extension.
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
BASENAME_MAX = 120


def file_stem(text: str, fallback: str) -> str:
    """A file name for a record: its key, lower-case ASCII with hyphens (dana@example.test -> dana-example-test).
    The name is only where the file lives; what a file is about is in its content, so a clash just adds -2."""
    slug = slugify(re.sub(r"[^\w\s-]", "-", text))
    stem = slug[:BASENAME_MAX].strip("-_") or fallback
    if len(slug) > BASENAME_MAX:
        stem = f"{stem[:100].rstrip('-_')}-{hashlib.sha256(text.encode()).hexdigest()[:10]}"
    return f"_{stem}" if stem in RESERVED else stem


class PathErrors(Exception):
    """Problems a write found, each at a path of the file."""

    def __init__(self, items: list[tuple[Path, str]]):
        super().__init__(items[0][1] if items else "")
        self.items = items


def uuid_list(values) -> list[uuid.UUID]:
    return sorted(values, key=str)


class KindSpec:
    name = ""
    folder = ""
    capability: str | None = None
    prunable = True
    custom_kind = ""  # what custom fields apply to, for kinds that have them
    base_fields: list[Field] = []

    def fields(self, state) -> list[Field]:
        return self.base_fields

    def available(self, state) -> bool:
        return self.capability is None or self.capability in state.caps

    def objects(self, state) -> list[Any]:
        raise NotImplementedError

    def values(self, state, obj) -> dict[str, Any]:
        raise NotImplementedError

    def key(self, values: dict[str, Any]) -> Any:
        return values["name"].lower()

    def label(self, state, values: dict[str, Any]) -> str:
        return values["name"]

    def basename(self, state, values: dict[str, Any]) -> str:
        return file_stem(values["name"], "unnamed")

    def status(self, state, obj) -> dict[str, Any] | None:
        return None

    def find(self, run, index: dict[Any, Any], values: dict[str, Any]) -> Any:
        """The row the file is about, or None to create one. May raise PathErrors (a key that can't be used)."""
        return index.get(self.key(values))

    def delete_fields(self, state, obj) -> list[dict[str, Any]]:
        """What a prune's delete line shows besides the key: what deleting the row takes with it."""
        return []

    def data(self, state, values: dict[str, Any], names=None) -> dict[str, Any]:
        """Service input for these fields (all but identity-only ones, or `names`)."""
        out: dict[str, Any] = {}
        for field in self.fields(state):
            if names is not None and field.name not in names:
                continue
            if field.type in (F.INSTANCES,) or field.name == "archived":
                continue
            out[field.name] = F.service_value(values[field.name])
        return out

    def changed_data(self, state, changed: list[Field], current, desired) -> dict[str, Any]:
        out = self.data(state, desired, {c.name for c in changed if c.type != F.CUSTOM})
        for field in changed:
            if field.type == F.CUSTOM:
                before, after = current[field.name], desired[field.name]
                out[field.name] = {
                    k: after.get(k) for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)
                }
        return out

    def create(self, run, values: dict[str, Any]) -> Any:
        raise NotImplementedError

    def update(self, run, obj, current, values, changed: list[Field]) -> Any:
        raise NotImplementedError

    def delete(self, run, obj) -> None:
        raise NotImplementedError

    def error_path(self, state, key: str) -> Path | None:
        if key.startswith(customfields.PREFIX):
            for field in self.fields(state):
                if field.type == F.CUSTOM:
                    return (*field.path, key.removeprefix(customfields.PREFIX))
        for field in self.fields(state):
            if field.name == key:
                return field.path
        return None


def _attachments(state, parent: tuple[str, Any]) -> dict[str, Any] | None:
    """Attachment metadata, read-only: never the files (status is ignored when a tree is applied)."""
    rows = state.attachments.get(parent, [])
    if not rows:
        return None
    items = []
    for a in rows:
        item: dict[str, Any] = {"title": a.title, "category": a.category, "kind": a.kind}
        if a.url:
            item["url"] = a.url
        if a.filename:
            item["filename"] = a.filename
        if a.content_type:
            item["contentType"] = a.content_type
        if a.size is not None:
            item["size"] = a.size
        if a.sha256:
            item["sha256"] = a.sha256
        items.append(item)
    items.sort(
        key=lambda i: (i["category"], i["title"].lower(), i.get("filename", ""), i.get("url", ""), i.get("sha256", ""))
    )
    return {"attachments": items}


# ---- reference data -----------------------------------------------------------------------------------------------


class NamedKind(KindSpec):
    """Departments, categories, teams and vendors: admin_services.create_named, update_named, delete_named."""

    model: Any = None

    def objects(self, state):
        return list(state.rows[self.model].values())

    def values(self, state, obj):
        out: dict[str, Any] = {}
        for field in self.fields(state):
            if field.name == "members":
                out["members"] = uuid_list(state.members.get(obj.id, []))
            elif field.type == F.CUSTOM:
                out[field.name] = state.custom_values(self.custom_kind, obj.custom_field_data)
            elif field.type in (F.PERSON, F.NAMED):
                out[field.name] = getattr(obj, f"{field.name}_id")
            else:
                out[field.name] = getattr(obj, field.name)
        return out

    def _create_data(self, state, values):
        data = self.data(state, values)
        if "custom_field_data" in data:
            data["custom_field_data"] = {
                name: values["custom_field_data"].get(name) for name in state.custom_fields_for(self.custom_kind)
            }
        return data

    def create(self, run, values):
        return admin_services.create_named(self.model, run.org, run.actor, self._create_data(run.state, values))

    def update(self, run, obj, current, values, changed):
        data = self.changed_data(run.state, changed, current, values)
        return admin_services.update_named(self.model, run.org, run.actor, obj, data)

    def delete(self, run, obj):
        admin_services.delete_named(self.model, run.org, run.actor, obj)


class DepartmentKind(NamedKind):
    name, folder, model = "Department", "departments", Department
    base_fields = [f("name", F.TEXT, "metadata", required=True)]


class CategoryKind(NamedKind):
    name, folder, model = "Category", "categories", SoftwareCategory
    base_fields = [f("name", F.TEXT, "metadata", required=True), f("description", F.TEXT)]


class TeamKind(NamedKind):
    name, folder, model = "Team", "teams", Team
    base_fields = [
        f("name", F.TEXT, "metadata", required=True),
        f("description", F.TEXT),
        f("members", F.PEOPLE),
    ]


class VendorKind(NamedKind):
    name, folder, model, custom_kind = "Vendor", "vendors", Vendor, "vendor"
    base_fields = [
        f("name", F.TEXT, "metadata", required=True),
        f("website", F.TEXT),
        f("support_url", F.TEXT),
        f("portal_url", F.TEXT),
        f("support_email", F.TEXT),
        f("support_phone", F.TEXT),
        f("account_manager", F.PERSON),
        f("notes", F.TEXT),
        f("custom_field_data", F.CUSTOM, yaml="customFields"),
    ]

    def fields(self, state):
        return [x for x in self.base_fields if x.type != F.CUSTOM or state.custom]

    def status(self, state, obj):
        return _attachments(state, ("vendor", obj.id))


class TagKind(NamedKind):
    """Tags are made on first use by the records that carry them; a Tag file adds a colour and a description, and
    makes the tag if no record has it yet (admin_services.create_tag). A tag's name is matched as tags are: spaces
    collapsed, ignoring case, so "Foo  Bar" is the existing "Foo Bar". Tags can't be deleted in Softwarden, so pruning
    leaves them."""

    name, folder, model, prunable = "Tag", "tags", Tag, False
    base_fields = [
        f("name", F.TEXT, "metadata", required=True, identity=True),
        f("color", F.CHOICE),
        f("description", F.TEXT),
    ]

    def key(self, values):
        return tags.normalise(values["name"]).lower()

    def create(self, run, values):
        tag = admin_services.create_tag(run.org, run.actor, {"name": values["name"]})
        extra = {k: values[k] for k in ("color", "description") if values[k] != getattr(tag, k)}
        return admin_services.update_tag(run.org, run.actor, tag, extra) if extra else tag

    def update(self, run, obj, current, values, changed):
        return admin_services.update_tag(run.org, run.actor, obj, {c.name: values[c.name] for c in changed})


class AIProviderKind(NamedKind):
    name, folder, model, capability = "AIProvider", "ai-providers", AIProvider, caps.AI_AGENTS
    base_fields = [f("name", F.TEXT, "metadata", required=True), f("website", F.TEXT)]

    def create(self, run, values):
        return ai.create_provider(run.org, run.actor, self.data(run.state, values))

    def update(self, run, obj, current, values, changed):
        return ai.update_provider(run.org, run.actor, obj, self.changed_data(run.state, changed, current, values))

    def delete(self, run, obj):
        ai.delete_provider(run.org, run.actor, obj)


class AIModelKind(KindSpec):
    name, folder, capability = "AIModel", "ai-models", caps.AI_AGENTS
    base_fields = [
        f("provider", F.NAMED, "metadata", model=AIProvider, noun="AI provider", required=True, identity=True),
        f("name", F.TEXT, "metadata", required=True, identity=True),
        f("version", F.TEXT, "metadata", identity=True),
        f("modality", F.CHOICE),
        f("hosting_region", F.TEXT),
    ]

    def objects(self, state):
        return list(state.models.values())

    def values(self, state, obj):
        return {
            "provider": obj.provider_id,
            "name": obj.name,
            "version": obj.version,
            "modality": obj.modality,
            "hosting_region": obj.hosting_region,
        }

    def key(self, values):
        return (values["provider"], values["name"].lower(), values["version"].lower())

    def label(self, state, values):
        return " ".join(
            x for x in (state.name_of(AIProvider, values["provider"]), values["name"], values["version"]) if x
        )

    def basename(self, state, values):
        return file_stem(self.label(state, values), "model")

    def create(self, run, values):
        return ai.create_model(run.org, run.actor, self.data(run.state, values))

    def update(self, run, obj, current, values, changed):
        return ai.update_model(run.org, run.actor, obj, self.changed_data(run.state, changed, current, values))

    def delete(self, run, obj):
        ai.delete_model(run.org, run.actor, obj)


class CustomFieldKind(KindSpec):
    name, folder, capability = "CustomField", "custom-fields", caps.CUSTOM_FIELDS
    base_fields = [
        f("name", F.TEXT, "metadata", required=True, identity=True),
        f("label", F.TEXT, required=True),
        f("type", F.CHOICE, required=True),
        f("description", F.TEXT),
        f("choices", F.STRINGS),
        f("required", F.FLAG),
        f("default", F.ANY),
        f("applies_to", F.APPLIES),
        f("weight", F.INT, default=100),
        f("is_reminder_source", F.FLAG),
    ]

    def objects(self, state):
        return list(state.custom_fields.values())

    def values(self, state, obj):
        return {
            "name": obj.name,
            "label": obj.label,
            "type": obj.type,
            "description": obj.description,
            "choices": list(obj.choices),
            "required": obj.required,
            "default": obj.default,
            "applies_to": [k for k in CUSTOM_FIELD_KINDS if k in obj.applies_to],
            "weight": obj.weight,
            "is_reminder_source": obj.is_reminder_source,
        }

    def key(self, values):
        return values["name"]

    def create(self, run, values):
        return customfields.create_field(run.org, run.actor, self.data(run.state, values))

    def update(self, run, obj, current, values, changed):
        return customfields.update_field(
            run.org, run.actor, obj, self.changed_data(run.state, changed, current, values)
        )

    def delete(self, run, obj):
        customfields.delete_field(run.org, run.actor, obj)

    def delete_fields(self, state, obj):
        """How many records and vendors lose their value for the field (delete_field clears them)."""
        held = sum(customfields.value_counts(state.org, obj.name).values())
        return [{"path": "values", "before": held, "after": 0}]


class RelationshipTypeKind(KindSpec):
    """A type's slug never changes; the three core types can be relabelled but never deleted, so pruning skips them."""

    name, folder, capability = "RelationshipType", "relationship-types", caps.RELATIONSHIPS
    base_fields = [
        f("slug", F.TEXT, "metadata", required=True, identity=True),
        f("name", F.TEXT, required=True),
        f("inverse_name", F.TEXT),
        f("description", F.TEXT),
        f("is_dependency", F.FLAG),
    ]

    def objects(self, state):
        return list(state.rtypes.values())

    def values(self, state, obj):
        return {k: getattr(obj, k) for k in ("slug", "name", "inverse_name", "description", "is_dependency")}

    def key(self, values):
        return values["slug"]

    def label(self, state, values):
        return values["slug"]

    def basename(self, state, values):
        return file_stem(values["slug"], "type")

    def create(self, run, values):
        return relationship_types.create_type(run.org, run.actor, self.data(run.state, values))

    def update(self, run, obj, current, values, changed):
        return relationship_types.update_type(
            run.org, run.actor, obj, self.changed_data(run.state, changed, current, values)
        )

    def delete(self, run, obj):
        if not obj.is_core:
            relationship_types.delete_type(run.org, run.actor, obj)

    def keep(self, obj) -> bool:
        return obj.is_core


class PersonKind(KindSpec):
    """People: exported only when asked for (personal data). Keyed by email; a person without one by the Softwarden ID
    they have where the tree was exported. Pruning never deletes people (anonymize them in the app instead).

    A file without an email whose ID this organization doesn't know (it came from another one) is matched by the exact
    name, and only created when nobody has that name, so applying it twice doesn't make two people; the plan
    warns either way. The name is matched only against people who were
    there before the run, never one an earlier file just created; two such files with one name are both errors; and a
    name-only match is reference-only: it links the tree's references to the person but never changes their fields
    (differences are warnings). A file can't remove or change the email of the person its ID names: when that person
    has one, the file must give the same."""

    name, folder, prunable = "Person", "people", False
    base_fields = [
        f("email", F.TEXT, "metadata"),
        f("id", F.TEXT, "metadata", identity=True),
        f("name", F.TEXT, "metadata", required=True),
        f("title", F.TEXT),
        f("department", F.NAMED, model=Department, noun="department"),
        f("active", F.FLAG, default=True),
    ]

    def objects(self, state):
        return [p for p in state.people.values() if p.anonymized_at is None]

    def values(self, state, obj):
        return {
            "email": obj.email,
            "id": "" if obj.email else str(obj.id),
            "name": obj.name,
            "title": obj.title,
            "department": obj.department_id,
            "active": obj.active,
        }

    def key(self, values):
        return ("email", values["email"].lower()) if values["email"] else ("id", values["id"].lower())

    def find(self, run, index, values):
        state = run.state
        pid = None
        if values["id"]:
            try:
                pid = uuid.UUID(values["id"])
            except ValueError:
                raise PathErrors(
                    [(("metadata", "id"), "Write the id as a Softwarden ID (a UUID), or give metadata.email instead.")]
                ) from None
        email = values["email"]
        if email:
            found = index.get(("email", email.lower()))
            if found is not None or pid is None:
                return found
        known = state.find_person(str(pid)) if pid is not None else None
        by_id = state.people.get(known) if known is not None else None
        if by_id is not None:
            if by_id.email and by_id.email.lower() != email.lower():
                message = "This Softwarden ID is a person with another email here; a file can't change or remove it."
                raise PathErrors([(("metadata", "email"), message)])
            return by_id
        if email:
            return None  # a new person with this email
        if pid is None:  # Run.parse requires an email or an id; never a guess
            raise PathErrors([(("metadata",), "Add metadata.email, or metadata.id for a person without one.")])
        twins = run.unkeyed_names.get(values["name"], [])
        if len(twins) > 1:
            others = ", ".join(path for path in twins if path != run.doc_path())
            message = (
                f"Another file without an email names {values['name']} too ({others}); add metadata.email to each."
            )
            raise PathErrors([(("metadata", "name"), message)])
        # Only people who were here before this run: never one an earlier file of the same tree just created.
        named = [
            p
            for pk, p in state.people.items()
            if pk in state.initial_people and p.name == values["name"] and p.anonymized_at is None
        ]
        if len(named) > 1:
            message = f"{len(named)} people are named {values['name']}; add metadata.email to say which."
            raise PathErrors([(("metadata", "name"), message)])
        if named:
            if named[0].email:
                message = (
                    f"The person named {values['name']} here has an email; add it as metadata.email to match them."
                )
                raise PathErrors([(("metadata", "email"), message)])
            state.person_alias[pid] = named[0].id
            run.reference_only.add(named[0].pk)  # a name is enough to link references, never to edit a person
            run.warn(
                ("metadata", "id"),
                f"No person has this Softwarden ID here; matched by name to {values['name']}, whose fields "
                "this file doesn't change.",
            )
            return named[0]
        run.warn(("metadata", "id"), "No person has this Softwarden ID or name here; applying creates them.")
        return None

    def label(self, state, values):
        return values["email"] or values["id"]

    def basename(self, state, values):
        return file_stem(values["email"] or values["id"], "person")

    def create(self, run, values):
        data = self.data(run.state, values)
        data.pop("id")
        person = services.create_person(run.org, run.actor, data)
        if values["id"]:
            run.state.person_alias[uuid.UUID(values["id"])] = person.id  # checked in find
        return person

    def update(self, run, obj, current, values, changed):
        data = self.changed_data(run.state, changed, current, values)
        data.pop("id", None)
        return admin_services.update_person(run.org, run.actor, obj, data)


# ---- records ------------------------------------------------------------------------------------------------------

COMPLIANCE = [
    f("is_gxp", F.BOOL),
    f("validation_status", F.NCHOICE),
    f("validation_doc_url", F.NTEXT),
    f("last_periodic_review", F.DATE),
    f("next_periodic_review", F.DATE),
    f("risk_tier", F.NCHOICE),
]
COMPLIANCE_NAMES = {c.name for c in COMPLIANCE}
OWNED = [
    f("lifecycle", F.CHOICE, required=True),
    f("criticality", F.CHOICE),
    f("business_owner", F.PERSON),
    f("technical_owner", F.PERSON),
    f("support_team", F.NAMED, model=Team, noun="team"),
    f("department", F.NAMED, model=Department, noun="department"),
    f("cost_center", F.TEXT),
    f("data_classification", F.CHOICE),
]
TAIL = [f("custom_field_data", F.CUSTOM, yaml="customFields"), f("archived", F.FLAG)]
# Fields on the shared Resource row; the rest are the kind's extension row's.
RESOURCE_ATTRIBUTES = {
    "name",
    "description",
    "lifecycle",
    "criticality",
    "business_owner",
    "technical_owner",
    "support_team",
    "department",
    "cost_center",
    "data_classification",
}
# Extension attributes that differ from the service's field name.
ATTRIBUTES = {
    "source": "source_resource_id",
    "target": "target_resource_id",
}


def _meta(with_tags=True) -> list[Field]:
    out = [
        f("slug", F.SLUG, "metadata", required=True, identity=True),
        f("name", F.TEXT, "metadata", required=True),
        f("description", F.TEXT, "metadata"),
    ]
    if with_tags:
        out.append(f("tags", F.TAGS, "metadata"))
    return out


class ResourceKind(KindSpec):
    """Applications, components, AI agents and integrations: catalog/services.create_resource (create_integration),
    update_resource, archive_resource, restore_resource and delete_resource. A file's slug is the record's natural key
    and is kept when the record is created."""

    kind = ""
    head: list[Field] = []
    extension: list[Field] = []

    def fields(self, state):
        out = [*self.head, *self.extension]
        if state.compliance and self.kind != Kind.INTEGRATION:
            out += COMPLIANCE
        out += [x for x in TAIL if x.type != F.CUSTOM or state.custom]
        return out

    def objects(self, state):
        return [r for r in state.resources.values() if r.kind == self.kind]

    def values(self, state, obj):
        ext = obj.extension
        out: dict[str, Any] = {}
        for field in self.fields(state):
            name = field.name
            if name == "slug":
                out[name] = obj.slug
            elif name == "tags":
                out[name] = state.tag_names.get(obj.id, [])
            elif name == "custom_field_data":
                out[name] = state.custom_values(self.kind, obj.custom_field_data)
            elif name == "archived":
                out[name] = obj.archived_at is not None
            elif name == "instances":
                out[name] = {
                    i.name.lower(): {"name": i.name, "environment": i.environment, "url": i.url, "status": i.status}
                    for i in sorted(state.instances.values(), key=lambda i: i.name.lower())
                    if i.application_id == obj.id
                }
            elif name == "models_used":
                out[name] = uuid_list(state.agent_models.get(obj.id, []))
            elif name in ("channels", "data_categories"):
                out[name] = list(getattr(ext, name))
            elif name in COMPLIANCE_NAMES:
                value = getattr(obj, name)
                out[name] = value.isoformat() if hasattr(value, "isoformat") else value
            elif name in RESOURCE_ATTRIBUTES:
                out[name] = getattr(obj, f"{name}_id") if field.type in (F.PERSON, F.NAMED) else getattr(obj, name)
            elif field.type in (F.PERSON, F.NAMED, F.RECORD, F.INSTANCE):
                out[name] = getattr(ext, ATTRIBUTES.get(name, f"{name}_id"))
            else:
                out[name] = getattr(ext, name)
        return out

    def key(self, values):
        return (self.kind, values["slug"])

    def label(self, state, values):
        return f"{self.kind}:{values['slug']}"

    def basename(self, state, values):
        return file_stem(values["slug"], self.kind)

    def status(self, state, obj):
        return _attachments(state, ("resource", obj.id))

    def _create_data(self, state, values):
        data = self.data(state, values)
        data.pop("slug")
        if "custom_field_data" in data:
            data["custom_field_data"] = {
                name: values["custom_field_data"].get(name) for name in state.custom_fields_for(self.kind)
            }
        return data

    def create(self, run, values):
        data = self._create_data(run.state, values)
        if self.kind == Kind.INTEGRATION:
            record = services.create_integration(run.org, run.actor, data, slug=values["slug"])
        else:
            record = services.create_resource(run.org, run.actor, {**data, "kind": self.kind}, slug=values["slug"])
        run.state.add(record)
        self.sync_instances(run, record, {}, values)
        if values["archived"]:
            record = services.archive_resource(run.org, run.actor, record)
        return record

    def update(self, run, obj, current, values, changed):
        if not values["archived"] and obj.archived_at is not None:
            obj = services.restore_resource(run.org, run.actor, obj)
        data = self.changed_data(run.state, [c for c in changed if c.name != "instances"], current, values)
        if data:
            obj = services.update_resource(run.org, run.actor, obj, data)
        self.sync_instances(run, obj, current.get("instances") or {}, values)
        if values["archived"] and obj.archived_at is None:
            obj = services.archive_resource(run.org, run.actor, obj)
        return obj

    def sync_instances(self, run, record, current: dict, values: dict) -> None:
        pass

    def delete(self, run, obj):
        services.delete_resource(run.org, run.actor, obj)


class ApplicationKind(ResourceKind):
    name, folder, kind, custom_kind = "Application", "applications", Kind.APPLICATION, "application"
    head = [*_meta(), *OWNED]
    extension = [
        f("vendor", F.NAMED, model=Vendor, noun="vendor"),
        f("category", F.NAMED, model=SoftwareCategory, noun="category"),
        f("purpose", F.TEXT),
        f("built_by", F.CHOICE),
        f("license_model", F.CHOICE),
        f("deployment_model", F.CHOICE),
        f("version", F.TEXT),
        f("primary_url", F.TEXT),
        f("admin_url", F.TEXT),
        f("documentation_url", F.TEXT),
        f("support_url", F.TEXT),
        f("authentication_method", F.CHOICE),
        f("sso_enabled", F.BOOL),
        f("scim_provisioning", F.BOOL),
        f("entra_app_id", F.GUID),
        f("service_principal_id", F.GUID),
        f("instances", F.INSTANCES),
    ]

    def sync_instances(self, run, record, current, values):
        """The file's instances: created or updated by name; one the file leaves out is deleted only when pruning (after
        the links that name it are)."""
        wanted = values.get("instances") or {}
        problems: list[tuple[Path, str]] = []
        by_name = {i.name.lower(): i for i in run.state.instances.values() if i.application_id == record.id}
        for key, want in wanted.items():
            where = run.instance_path(key)
            have = current.get(key)
            try:
                if key not in by_name:
                    instance = services.create_instance(run.org, run.actor, record, dict(want))
                    run.instance_change(key, None, want)
                elif have != want:
                    change = {k: v for k, v in want.items() if have is None or have.get(k) != v}
                    instance = services.update_instance(run.org, run.actor, by_name[key], change)
                    run.instance_change(key, have, want)
                else:
                    continue
            except FieldErrors as e:
                problems += [((*where, k if k in F.INSTANCE_KEYS else "name"), m[0]) for k, m in e.errors.items()]
                continue
            run.state.add(instance)
        if problems:
            raise PathErrors(problems)
        for key, instance in by_name.items():
            if key not in wanted:
                run.instance_missing(record, instance)

    def delete(self, run, obj):
        for instance in [i for i in run.state.instances.values() if i.application_id == obj.id]:
            services.delete_instance(run.org, run.actor, instance)
        super().delete(run, obj)


class ComponentKind(ResourceKind):
    name, folder, kind, custom_kind = "Component", "components", Kind.COMPONENT, "component"
    head = [*_meta(), *OWNED]
    extension = [
        f("component_type", F.CHOICE, default="other"),
        f("purpose", F.TEXT),
        f("built_by", F.CHOICE),
        f("license_model", F.CHOICE),
        f("hosting", F.CHOICE),
        f("environment", F.CHOICE),
        f("host_runtime", F.TEXT),
        f("schedule_or_trigger", F.TEXT),
        f("repository_url", F.TEXT),
        f("documentation_url", F.TEXT),
        f("runbook_url", F.TEXT),
        f("azure_resource_id", F.TEXT),
        f("pp_environment_id", F.TEXT),
        f("external_id", F.TEXT),
    ]


class AIAgentKind(ResourceKind):
    name, folder, kind, custom_kind, capability = "AIAgent", "ai-agents", Kind.AI_AGENT, "ai_agent", caps.AI_AGENTS
    head = [*_meta(), *OWNED]
    extension = [
        f("purpose", F.TEXT),
        f("platform", F.CHOICE),
        f("models_used", F.MODELS),
        f("environment", F.CHOICE),
        f("pp_environment_id", F.TEXT),
        f("channels", F.CHANNELS),
        f("knowledge_sources", F.TEXT),
        f("tools_connectors", F.TEXT),
        f("auth_mode", F.CHOICE),
        f("run_as_identity", F.TEXT),
        f("data_access_scope", F.TEXT),
        f("human_in_loop", F.BOOL),
        f("actions_can_write", F.BOOL),
        f("risk_classification", F.CHOICE),
        f("repo_url", F.TEXT),
        f("external_id", F.TEXT),
    ]


ENDS = ("application", "component")


class IntegrationKind(ResourceKind):
    name, folder, kind, custom_kind = "Integration", "integrations", Kind.INTEGRATION, "integration"
    head = [
        *_meta(with_tags=False),
        f("lifecycle", F.CHOICE, required=True),
        f("source", F.RECORD, kinds=ENDS, required=True),
        f("source_instance", F.INSTANCE, of="source"),
        f("target", F.RECORD, kinds=ENDS, required=True),
        f("target_instance", F.INSTANCE, of="target"),
        f("technical_owner", F.PERSON),
        f("support_team", F.NAMED, model=Team, noun="team"),
        f("data_classification", F.CHOICE),
        f("data_categories", F.CATEGORIES),  # data-flow map
    ]
    extension = [
        f("data_purpose", F.TEXT),
        f("protocol", F.TEXT),
        f("schedule", F.TEXT),
        f("direction", F.CHOICE),
        f("auth_method", F.CHOICE),
        f("documentation_url", F.TEXT),
        f("monitoring_url", F.TEXT),
    ]


class RelationshipKind(KindSpec):
    """A link between two records. Its type and ends are its identity; only the purpose is ever updated."""

    name, folder, capability = "Relationship", "relationships", caps.RELATIONSHIPS
    base_fields = [
        f("type", F.RTYPE, required=True, identity=True),
        f("source", F.RECORD, kinds=F.RECORD_KINDS, required=True, identity=True),
        f("source_instance", F.INSTANCE, of="source", identity=True),
        f("target", F.RECORD, kinds=F.RECORD_KINDS, required=True, identity=True),
        f("target_instance", F.INSTANCE, of="target", identity=True),
        f("purpose", F.TEXT),
    ]

    def objects(self, state):
        return [
            r for r in state.relationships.values() if r.source_id in state.resources and r.target_id in state.resources
        ]

    def values(self, state, obj):
        return {
            "type": obj.type.slug,
            "source": obj.source_id,
            "source_instance": obj.source_instance_id,
            "target": obj.target_id,
            "target_instance": obj.target_instance_id,
            "purpose": obj.purpose,
        }

    def key(self, values):
        return tuple(values[k] for k in ("type", "source", "source_instance", "target", "target_instance"))

    def _end(self, state, values, end):
        ref = state.record_ref(values[end])
        instance = values[f"{end}_instance"]
        return f"{ref}@{state.instance_name(instance)}" if instance else ref

    def label(self, state, values):
        return f"{self._end(state, values, 'source')} {values['type']} {self._end(state, values, 'target')}"

    def basename(self, state, values):
        def end(name):
            record = state.resources[values[name]]
            text = f"{record.kind.replace('_', '-')}-{record.slug}"
            instance = values[f"{name}_instance"]
            return f"{text}--{slugify(state.instance_name(instance))}" if instance else text

        text = f"{end('source')}--{values['type'].replace('_', '-')}--{end('target')}"
        return file_stem(text, "relationship") if len(text) > BASENAME_MAX else text

    def create(self, run, values):
        return services.create_relationship(run.org, run.actor, self.data(run.state, values))

    def update(self, run, obj, current, values, changed):
        return services.update_relationship(run.org, run.actor, obj, {"purpose": values["purpose"]})

    def delete(self, run, obj):
        services.delete_relationship(run.org, run.actor, obj)


ORDER: list[KindSpec] = [
    DepartmentKind(),
    CategoryKind(),
    TagKind(),
    CustomFieldKind(),
    RelationshipTypeKind(),
    PersonKind(),
    TeamKind(),
    VendorKind(),
    AIProviderKind(),
    AIModelKind(),
    ApplicationKind(),
    ComponentKind(),
    AIAgentKind(),
    IntegrationKind(),
    RelationshipKind(),
]
KINDS: dict[str, KindSpec] = {k.name: k for k in ORDER}
PRUNE_ORDER = [k for k in reversed(ORDER) if k.prunable]
FOLDERS = [k.folder for k in ORDER]
