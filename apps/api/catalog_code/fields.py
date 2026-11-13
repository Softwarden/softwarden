"""The fields of a catalog-as-code file and how each kind of value is read, compared and written.

Every field has a canonical value: what the comparison uses and what the service function is given. References are
the referenced row's UUID (resolved within the organization only), text is stripped, a GUID is lower-case, a set
(tags, members, an agent's models) is sorted. A file's value is parsed into the canonical form (`parse`); a stored
row's is read by its kind (catalog_code/kinds.py); the YAML form is rendered from the canonical one (`render`), so an
export and a plan's "before" column read the same. A field whose value is its default is left out of a file, and a
field left out of a file means its default: the files are the whole desired state of each record they name."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from catalog import customfields, tags
from catalog.models import CUSTOM_FIELD_KINDS, AgentChannel, DataCategory

from .fmt import Path, camel

# Field types.
TEXT = "text"  # "" when empty
GUID = "guid"  # text, stored lower-case
NTEXT = "ntext"  # text, None when empty (the compliance URL)
CHOICE = "choice"  # one of a choice list's values; the service checks which
NCHOICE = "nchoice"  # the same, None when empty (the compliance choices)
DATE = "date"  # YYYY-MM-DD text, None when empty
BOOL = "bool"  # true, false or not recorded (None)
FLAG = "flag"  # true or false, with a default
INT = "int"
SLUG = "slug"  # a record's slug: its natural key
PERSON = "person"  # a Person by email, or by Softwarden ID
PEOPLE = "people"  # a list of those
NAMED = "named"  # a team, department, vendor, category or AI provider by name
RECORD = "record"  # <kind>:<slug> of an application, component, AI agent or integration
INSTANCE = "instance"  # an instance of the record another field names, by its name
RTYPE = "rtype"  # a relationship type by slug
TAGS = "tags"
CUSTOM = "custom"  # custom field values by field name
CHANNELS = "channels"
CATEGORIES = "categories"  # an integration's data categories, in choice order
MODELS = "models"  # AI models as {provider, name, version}
STRINGS = "strings"  # a list of text, order kept
APPLIES = "applies"  # what a custom field applies to
ANY = "any"  # a custom field's default: any value its type takes
INSTANCES = "instances"  # an application's instances: {name, environment, url, status}
INSTANCE_KEYS = ("name", "environment", "url", "status")
INSTANCE_STATUS_DEFAULT = "unknown"

LISTS = {PEOPLE, TAGS, CHANNELS, CATEGORIES, MODELS, STRINGS, APPLIES}
NULLABLE = {NTEXT, NCHOICE, DATE, BOOL, PERSON, NAMED, RECORD, INSTANCE, RTYPE, ANY}
TEXTUAL = {TEXT, GUID, NTEXT, CHOICE, NCHOICE, DATE, SLUG}
SLUG_MAX = 220
RECORD_KINDS = ("application", "component", "ai_agent", "integration")


@dataclass(frozen=True)
class Field:
    name: str  # the service's (and API's) name for it
    type: str
    section: str = "spec"
    default: Any = None
    model: Any = None  # NAMED: the model whose rows are named
    of: str = ""  # INSTANCE: the RECORD field naming the record the instance belongs to
    kinds: tuple[str, ...] = ()  # RECORD: the kinds it may name
    yaml: str = ""  # the YAML key when it isn't camel(name)
    required: bool = False  # a file must give it
    identity: bool = False  # part of the natural key: never an update
    noun: str = ""  # NAMED: what an error calls the row

    @property
    def key(self) -> str:
        return self.yaml or camel(self.name)

    @property
    def path(self) -> Path:
        return (self.section, self.key)

    def empty(self) -> Any:
        if self.default is not None:
            return list(self.default) if isinstance(self.default, list) else self.default
        if self.type in LISTS:
            return []
        if self.type in (CUSTOM, INSTANCES):
            return {}
        if self.type in NULLABLE:
            return None
        if self.type == FLAG:
            return False
        return ""


def f(name: str, type: str, section: str = "spec", **kw: Any) -> Field:
    return Field(name, type, section, **kw)


class Problems:
    """Errors found while parsing one file: (path, message)."""

    def __init__(self) -> None:
        self.items: list[tuple[Path, str]] = []

    def add(self, path: Path, message: str) -> None:
        self.items.append((path, message))

    def __bool__(self) -> bool:
        return bool(self.items)


BAD = object()  # a value that couldn't be read (its error is recorded)


def _text(value: Any, path: Path, problems: Problems) -> Any:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool | int | float):
        problems.add(path, f"Put {value!r} in quotes: this field is text.")
    else:
        problems.add(path, "Write this field as text.")
    return BAD


def _list_of_text(value: Any, path: Path, problems: Problems) -> list[str] | Any:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.add(path, "Write this field as a list of text.")
        return BAD
    return [v.strip() for v in value]


def parse(field: Field, raw: Any, ctx: Any, path: Path, problems: Problems, values: dict[str, Any], kind: str) -> Any:
    """The canonical value of `raw` (None: left out), or BAD with the problem recorded. `ctx` resolves references
    (catalog_code/state.State); `values` are the fields parsed so far (an instance needs its record); `kind` is what
    custom fields apply to."""
    t = field.type
    if raw is None:
        return field.empty()
    if t in TEXTUAL:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        if t == GUID:
            return text.lower()
        if t == SLUG:
            if not text or len(text) > SLUG_MAX or not all(c.isascii() and (c.isalnum() or c in "-_") for c in text):
                problems.add(path, "Use lower-case letters, digits, hyphens and underscores (crm, payroll-api).")
                return BAD
            if text != text.lower():
                problems.add(path, "Use lower case: slugs are compared exactly.")
                return BAD
            return text
        if t in (NTEXT, NCHOICE, DATE):
            return text or None
        return text or field.empty()
    if t in (BOOL, FLAG):
        if not isinstance(raw, bool):
            problems.add(path, "Use true or false.")
            return BAD
        return raw
    if t == INT:
        if isinstance(raw, bool) or not isinstance(raw, int):
            problems.add(path, "Use a whole number.")
            return BAD
        return raw
    if t == ANY:
        return raw
    if t == PERSON:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        if not text:
            return None
        found = ctx.find_person(text)
        if found is None:
            problems.add(path, f'No person "{text}" is in this organization or these files.')
            return BAD
        return found
    if t == PEOPLE:
        names = _list_of_text(raw, path, problems)
        if names is BAD:
            return BAD
        ids = []
        for i, text in enumerate(names):
            found = ctx.find_person(text) if text else None
            if found is None:
                problems.add((*path, i), f'No person "{text}" is in this organization or these files.')
                return BAD
            ids.append(found)
        return sorted(set(ids), key=str)
    if t == NAMED:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        if not text:
            return None
        found = ctx.find_named(field.model, text)
        if found is None:
            problems.add(path, f'No {field.noun} named "{text}" is in this organization or these files.')
            return BAD
        return found
    if t == RTYPE:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        if ctx.find_rtype(text) is None:
            problems.add(path, f"Use one of the organization's relationship types: {', '.join(ctx.rtype_slugs())}.")
            return BAD
        return text
    if t == RECORD:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        record_kind, _, slug = text.partition(":")
        if record_kind not in field.kinds or not slug:
            shown = ", ".join(f"{k}:<slug>" for k in field.kinds)
            problems.add(path, f"Name a record as {shown}.")
            return BAD
        found = ctx.find_record(record_kind, slug)
        if found is None:
            problems.add(
                path,
                f'No {record_kind.replace("_", " ")} with the slug "{slug}" is in this organization or these files.',
            )
            return BAD
        return found
    if t == INSTANCE:
        text = _text(raw, path, problems)
        if text is BAD:
            return BAD
        if not text:
            return None
        record = values.get(field.of)
        if record is None or record is BAD:
            if record is None:
                problems.add(path, f"Name the {camel(field.of)} record first: an instance belongs to it.")
            return BAD
        found = ctx.find_instance(record, text)
        if found is None:
            problems.add(path, f'{ctx.record_ref(record)} has no instance named "{text}".')
            return BAD
        return found
    if t == TAGS:
        if not isinstance(raw, list):
            problems.add(path, "Write tags as a list of names.")
            return BAD
        errors: dict[str, list[str]] = {}
        names = tags.clean(raw, errors)
        if names is None:
            problems.add(path, errors["tags"][0])
            return BAD
        return sorted(names, key=str.lower)
    if t == STRINGS:
        return _list_of_text(raw, path, problems)
    if t == APPLIES:
        names = _list_of_text(raw, path, problems)
        if names is BAD:
            return BAD
        known = [k for k in CUSTOM_FIELD_KINDS if k in names]
        return known + [n for n in names if n not in CUSTOM_FIELD_KINDS]
    if t == CHANNELS:
        names = _list_of_text(raw, path, problems)
        if names is BAD:
            return BAD
        unknown = [n for n in names if n not in AgentChannel.values]
        if unknown:
            problems.add(path, f"Use any of: {', '.join(AgentChannel.values)}.")
            return BAD
        return [c for c in AgentChannel.values if c in names]
    if t == CATEGORIES:
        names = _list_of_text(raw, path, problems)
        if names is BAD:
            return BAD
        unknown = [n for n in names if n not in DataCategory.values]
        if unknown:
            problems.add(path, f"Use any of: {', '.join(DataCategory.values)}.")
            return BAD
        return [c for c in DataCategory.values if c in names]
    if t == MODELS:
        if not isinstance(raw, list):
            problems.add(path, "List the models as {provider, name, version}.")
            return BAD
        ids = []
        for i, item in enumerate(raw):
            where = (*path, i)
            if not isinstance(item, dict) or set(item) - {"provider", "name", "version"}:
                problems.add(where, "Name a model with provider, name and (if it has one) version.")
                return BAD
            provider, name, version = item.get("provider"), item.get("name"), item.get("version") or ""
            if not all(isinstance(v, str) for v in (provider, name, version)):
                problems.add(where, "Write the provider, name and version as text.")
                return BAD
            found = ctx.find_model(str(provider).strip(), str(name).strip(), str(version).strip())
            if found is None:
                label = f"{provider} {name} {version}".strip()
                problems.add(where, f'No AI model "{label}" is in this organization or these files.')
                return BAD
            ids.append(found)
        return sorted(set(ids), key=str)
    if t == CUSTOM:
        if not isinstance(raw, dict):
            problems.add(path, "Write custom fields as field name: value.")
            return BAD
        fields = ctx.custom_fields_for(kind)
        out: dict[str, Any] = {}
        ok = True
        for name, value in raw.items():
            cf = fields.get(name)
            if cf is None:
                problems.add(
                    (*path, name), f"There is no custom field {name} for {customfields.KIND_NOUNS.get(kind, kind)}."
                )
                ok = False
                continue
            try:
                cleaned = customfields.clean_value(cf, value)
            except customfields.Invalid as e:
                problems.add((*path, name), f"{cf.label}: {e.message}")
                ok = False
                continue
            if cleaned is not None:
                out[name] = cleaned
        return dict(sorted(out.items())) if ok else BAD
    if t == INSTANCES:
        return _instances(raw, path, problems)
    raise ValueError(f"Unknown field type {t}")


def _instances(raw: Any, path: Path, problems: Problems) -> Any:
    """{lower-case name: {name, environment, url, status}}: an application's instances, one per name."""
    if not isinstance(raw, list):
        problems.add(path, "List the instances, each with a name.")
        return BAD
    out: dict[str, dict[str, str]] = {}
    for i, item in enumerate(raw):
        where = (*path, i)
        if not isinstance(item, dict):
            problems.add(where, "Write an instance as name, environment, url and status.")
            return BAD
        unknown = [k for k in item if k not in INSTANCE_KEYS]
        if unknown:
            problems.add((*where, unknown[0]), f"An instance has {', '.join(INSTANCE_KEYS)}; {unknown[0]} isn't one.")
            return BAD
        values = {}
        for key in INSTANCE_KEYS:
            text = _text(item.get(key) or "", (*where, key), problems)
            if text is BAD:
                return BAD
            values[key] = text
        if not values["name"]:
            problems.add((*where, "name"), "Give the instance a name.")
            return BAD
        values["status"] = values["status"] or INSTANCE_STATUS_DEFAULT
        if values["name"].lower() in out:
            problems.add((*where, "name"), f"Two instances are named {values['name']}.")
            return BAD
        out[values["name"].lower()] = values
    return dict(sorted(out.items()))


def render_instance(values: dict[str, str]) -> dict[str, str]:
    return {
        k: values[k]
        for k in INSTANCE_KEYS
        if values.get(k) and not (k == "status" and values[k] == INSTANCE_STATUS_DEFAULT)
    }


def _sorted_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sorted_deep(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_sorted_deep(v) for v in value]
    return value


def render(field: Field, value: Any, ctx: Any) -> Any:
    """The YAML form of a canonical value, or None when the field is left out (its default, or empty)."""
    t = field.type
    if t not in (FLAG, INT) and value in (None, "", [], {}):
        return None
    if field.default is not None and value == field.default:
        return None
    if t == FLAG and field.default is None and value is False:
        return None
    if t == PERSON:
        return ctx.person_ref(value)
    if t == PEOPLE:
        return sorted(ctx.person_ref(v) for v in value)
    if t == NAMED:
        return ctx.name_of(field.model, value)
    if t == RECORD:
        return ctx.record_ref(value)
    if t == INSTANCE:
        return ctx.instance_name(value)
    if t == MODELS:
        return sorted(
            (ctx.model_ref(v) for v in value),
            key=lambda m: (m["provider"].lower(), m["name"].lower(), m.get("version", "").lower()),
        )
    if t == TAGS:
        return sorted(value, key=str.lower)
    if t in (CUSTOM, ANY):
        return _sorted_deep(value)
    if t == INSTANCES:
        return [render_instance(value[k]) for k in sorted(value)]
    return value


def same(field: Field, a: Any, b: Any) -> bool:
    if field.type == TAGS:
        return [x.lower() for x in a or []] == [x.lower() for x in b or []]
    return a == b


def service_value(value: Any) -> Any:
    """A canonical value as the service functions take it: IDs as text."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list):
        return [service_value(v) for v in value]
    return value
