"""One organization's catalog as catalog-as-code sees it: every row of the kinds it manages, loaded in a few queries,
with the indexes that resolve a file's references (natural keys) and render a row's (names, slugs, emails).

Everything is read with the organization's filter, so a reference can only ever resolve to a row of the same
organization; another organization's slug, name or Softwarden ID is simply not found. During an apply, rows the
run creates are added (`add`) so later files can name them."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from catalog import compliance, customfields, tags
from catalog.models import (
    AgentModel,
    AIModel,
    AIProvider,
    ApplicationInstance,
    Attachment,
    CustomField,
    Department,
    Kind,
    Person,
    Relationship,
    RelationshipType,
    Resource,
    SoftwareCategory,
    Tag,
    Team,
    TeamMember,
    Vendor,
)
from core import capabilities as caps

NAMED_MODELS = (Department, SoftwareCategory, Tag, Team, Vendor, AIProvider)


def _uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


class State:
    def __init__(self, org, *, include_people: bool = True):
        self.org = org
        self.caps = caps.capabilities(org)
        self.include_people = include_people
        self.compliance = compliance.enabled(org)
        self.ai = caps.AI_AGENTS in self.caps
        self.custom = caps.CUSTOM_FIELDS in self.caps
        self.relationships_on = caps.RELATIONSHIPS in self.caps
        self.attachments_on = caps.ATTACHMENTS in self.caps
        by_org = {"organization": org}
        self.rows: dict[Any, dict[uuid.UUID, Any]] = {
            Department: {d.id: d for d in Department.objects.filter(**by_org)},
            SoftwareCategory: {c.id: c for c in SoftwareCategory.objects.filter(**by_org)},
            Tag: {t.id: t for t in Tag.objects.filter(**by_org)},
            Team: {t.id: t for t in Team.objects.filter(**by_org)},
            Vendor: {v.id: v for v in Vendor.objects.filter(**by_org)},
            AIProvider: {p.id: p for p in AIProvider.objects.filter(**by_org)} if self.ai else {},
        }
        self.people: dict[uuid.UUID, Person] = {p.id: p for p in Person.objects.filter(**by_org)}
        self.members: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for team_id, person_id in TeamMember.objects.filter(**by_org).values_list("team_id", "person_id"):
            self.members[team_id].append(person_id)
        self.custom_fields: dict[uuid.UUID, CustomField] = (
            {cf.id: cf for cf in CustomField.objects.filter(**by_org)} if self.custom else {}
        )
        self.rtypes: dict[uuid.UUID, RelationshipType] = {t.id: t for t in RelationshipType.objects.filter(**by_org)}
        self.models: dict[uuid.UUID, AIModel] = (
            {m.id: m for m in AIModel.objects.filter(**by_org).select_related("provider")} if self.ai else {}
        )
        kinds = [Kind.APPLICATION, Kind.COMPONENT, Kind.INTEGRATION] + ([Kind.AI_AGENT] if self.ai else [])
        self.resources: dict[uuid.UUID, Resource] = {
            r.id: r
            for r in Resource.objects.filter(organization=org, kind__in=kinds).select_related(
                "application", "component", "integration", "ai_agent"
            )
        }
        self.tag_names: dict[uuid.UUID, list[str]] = tags.names_for(self.resources)
        self.instances: dict[uuid.UUID, ApplicationInstance] = {
            i.id: i for i in ApplicationInstance.objects.filter(**by_org)
        }
        self.agent_models: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        if self.ai:
            for agent_id, model_id in AgentModel.objects.filter(**by_org).values_list("agent_id", "model_id"):
                self.agent_models[agent_id].append(model_id)
        self.relationships: dict[uuid.UUID, Relationship] = (
            {r.id: r for r in Relationship.objects.filter(**by_org).select_related("type")}
            if self.relationships_on
            else {}
        )
        self.attachments: dict[tuple[str, uuid.UUID], list[Attachment]] = defaultdict(list)
        if self.attachments_on:
            for a in Attachment.objects.filter(organization=org, contract__isnull=True):
                parent = ("resource", a.resource_id) if a.resource_id else ("vendor", a.vendor_id)
                self.attachments[parent].append(a)  # type: ignore[index]
        # Files whose Person has no email are keyed by the Softwarden ID they had where they were exported; a Person
        # this run creates from one answers to that ID for the rest of the run (catalog_code/kinds.PersonKind).
        self.person_alias: dict[uuid.UUID, uuid.UUID] = {}
        # The people there were before the run: the only ones a Person file can be matched to by name.
        self.initial_people: frozenset[uuid.UUID] = frozenset(self.people)
        self._index()

    def _index(self) -> None:
        self.by_name: dict[Any, dict[str, uuid.UUID]] = {
            model: {row.name.lower(): pk for pk, row in rows.items()} for model, rows in self.rows.items()
        }
        self.emails = {p.email.lower(): p.id for p in self.people.values() if p.email}
        self.by_slug = {(r.kind, r.slug): r.id for r in self.resources.values()}
        self.instance_index = {(i.application_id, i.name.lower()): i.id for i in self.instances.values()}
        self.model_index = {(m.provider_id, m.name.lower(), m.version.lower()): m.id for m in self.models.values()}
        self.rtype_index = {t.slug: t.id for t in self.rtypes.values()}
        self.field_index = {cf.name: cf.id for cf in self.custom_fields.values()}

    # ---- registering what a run writes ------------------------------------------------------------------------------

    def add(self, obj: Any) -> None:
        """A row this run created or changed, so later files resolve it and later renders show it."""
        model = type(obj)
        if model in self.rows:
            self.rows[model][obj.id] = obj
            self.by_name[model][obj.name.lower()] = obj.id
        elif isinstance(obj, Person):
            self.people[obj.id] = obj
            if obj.email:
                self.emails[obj.email.lower()] = obj.id
        elif isinstance(obj, Resource):
            self.resources[obj.id] = obj
            self.by_slug[(obj.kind, obj.slug)] = obj.id
            self.tag_names[obj.id] = tags.names_of(obj)
        elif isinstance(obj, ApplicationInstance):
            self.instances[obj.id] = obj
            self.instance_index[(obj.application_id, obj.name.lower())] = obj.id
        elif isinstance(obj, AIModel):
            self.models[obj.id] = obj
            self.model_index[(obj.provider_id, obj.name.lower(), obj.version.lower())] = obj.id
        elif isinstance(obj, RelationshipType):
            self.rtypes[obj.id] = obj
            self.rtype_index[obj.slug] = obj.id
        elif isinstance(obj, CustomField):
            self.custom_fields[obj.id] = obj
            self.field_index[obj.name] = obj.id
        elif isinstance(obj, Relationship):
            self.relationships[obj.id] = obj

    # ---- resolving a file's references --------------------------------------------------------------------------------

    def find_person(self, text: str) -> uuid.UUID | None:
        pid = _uuid(text)
        if pid is not None:
            return pid if pid in self.people else self.person_alias.get(pid)
        return self.emails.get(text.lower())

    def find_named(self, model, text: str) -> uuid.UUID | None:
        return self.by_name.get(model, {}).get(text.lower())

    def find_record(self, kind: str, slug: str) -> uuid.UUID | None:
        return self.by_slug.get((kind, slug))

    def find_instance(self, record: uuid.UUID, name: str) -> uuid.UUID | None:
        return self.instance_index.get((record, name.lower()))

    def find_model(self, provider: str, name: str, version: str) -> uuid.UUID | None:
        provider_id = self.find_named(AIProvider, provider)
        return self.model_index.get((provider_id, name.lower(), version.lower())) if provider_id else None

    def find_rtype(self, slug: str) -> uuid.UUID | None:
        return self.rtype_index.get(slug)

    def rtype_slugs(self) -> list[str]:
        return sorted(self.rtype_index)

    def custom_fields_for(self, kind: str) -> dict[str, CustomField]:
        return {cf.name: cf for cf in self.custom_fields.values() if kind in cf.applies_to} if self.custom else {}

    # ---- rendering references -----------------------------------------------------------------------------------------

    def person_ref(self, pid: uuid.UUID) -> str:
        person = self.people.get(pid)
        if person is not None and self.include_people and person.email and person.anonymized_at is None:
            return person.email
        return str(pid)

    def name_of(self, model, pk: uuid.UUID) -> str:
        return self.rows[model][pk].name

    def record_ref(self, pk: uuid.UUID) -> str:
        record = self.resources[pk]
        return f"{record.kind}:{record.slug}"

    def instance_name(self, pk: uuid.UUID) -> str:
        return self.instances[pk].name

    def model_ref(self, pk: uuid.UUID) -> dict[str, str]:
        model = self.models[pk]
        ref = {"provider": model.provider.name, "name": model.name}
        if model.version:
            ref["version"] = model.version
        return ref

    def custom_values(self, kind: str, stored: dict | None) -> dict[str, Any]:
        """The stored custom field values of a row, for the fields that apply to its kind now."""
        fields = self.custom_fields_for(kind)
        return {k: v for k, v in sorted((stored or {}).items()) if k in fields and v is not None}

    def forget(self) -> None:
        customfields.forget(self.org)
