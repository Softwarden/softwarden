"""Custom fields (re-implemented for the API).

An Admin defines a field once per organization (CustomField: name, label, type, choices, required, default,
applies_to, weight, is_reminder_source); records of the kinds it applies to (applications, components, integrations,
AI agents), vendors and contracts and licence pools keep their values in `custom_field_data`, a JSON object {name: value}. A value that isn't set
has no key. Validation lives here and nowhere else, so the API, CSV import and discovery's "create record" refuse the
same values with the same messages.

Storage format per type: text, longtext, url, select and date (YYYY-MM-DD) are strings; integer an int; decimal a
JSON number (at most 15 significant digits and 6 decimal places, so it reads back exactly); boolean true or false;
multiselect a list of choices in the field's choice order; object a JSON object (at most 5 levels and 5,000 characters,
no key that looks like a secret).

Writes. A record's POST or PATCH carries `custom_field_data` as an object; only the names it carries change (a PATCH
merges), null or "" clears one. A name the organization hasn't defined, or a field that doesn't apply to the kind, is
refused. Errors are field errors keyed `custom_field_data.<name>`. On create, a field the request leaves out gets its
default; a required field must then hold a value. On update, a required field can't be cleared, but a record that
predates the requirement can still be edited otherwise (data quality reports the gap).

Definitions. `name` never changes after creation (the label does). A field's type can't change while any record holds
a value for it, a choice can't be removed while a record uses it, and a kind can't be removed from applies_to while a
record of that kind holds a value: each is a field error naming how many records. Deleting a field removes its values
from every record and is audited once, with the number of records cleared.

Audit. Snapshots and `resource.updated` payloads carry one key per applicable field, `custom_field_data.<name>`, by the
organization's current definitions only (declared keys); an object value with a secret-looking key at any depth is
left out. Diffs and summaries show the field's label, read when the event is read."""

import datetime
import json
import re
from decimal import Decimal
from typing import Any, NamedTuple

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, connection, transaction
from django.db.models import BooleanField, Count, F, Func, JSONField, TextField, Value
from django.db.models.functions import Cast
from django.utils import timezone

from core import dates, limits
from core.capabilities import CONTRACTS, has_capability
from core.problems import FieldErrors
from core.request_context import forget_organization, organization_cache

from .models import CUSTOM_FIELD_KINDS, CustomField, Resource, Vendor
from .models import CustomFieldType as T
from .redaction import SNAPSHOT_DENYLIST, secret_like

KEY = "custom_field_data"
PREFIX = KEY + "."  # field errors, snapshot keys and audit payload keys: custom_field_data.<name>
COLUMN = "cf_"  # CSV and Excel columns and list filters: cf_<name>

NAME_MAX = 50
LABEL_MAX = 100
DESCRIPTION_MAX = 500
CHOICES_MAX = 100
CHOICE_MAX = 100
TEXT_MAX = 500
LONGTEXT_MAX = 5000
URL_MAX = 2000
INTEGER_MAX = 2**53 - 1  # exact in JavaScript too
DECIMAL_DIGITS = 15  # significant digits a JSON number (a double) keeps exactly
DECIMAL_PLACES = 6
OBJECT_MAX = 5000  # characters of compact JSON
OBJECT_DEPTH = 5
WEIGHT_MAX = 32767
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
SEPARATOR = ";"  # between multiselect choices in a CSV cell (as tags and an agent's channels)
CHOICE_TYPES = (T.SELECT, T.MULTISELECT)
CONTRACT_KINDS = ("contract", "license_pool")  # need the `contracts` capability
KIND_NOUNS = {
    "application": "applications",
    "component": "components",
    "integration": "integrations",
    "ai_agent": "AI agents",
    "vendor": "vendors",
    "contract": "contracts",
    "license_pool": "license pools",
}
URL_MESSAGE = "Enter a full web address starting with http:// or https://."
DENIED_KEYS_MESSAGE = (
    "Keys that look like secrets (password, token, key, credential...) can't be stored in a custom field."
)


# The custom field filters can't be static OpenAPI parameters (each organization defines its own), so the pattern is
# described on every list that takes them (catalog/filters.CustomFieldFilters).
FILTERS_DOC = (
    "Custom field filters (GET /custom-fields for the names): `cf_<name>=<value>` for each custom field that applies. "
    "Text, long text and URL fields match text the value contains, ignoring case; choice fields take comma-separated "
    "choices (any of them; a multiple-choice field holding any of them); yes/no fields take true or false; whole "
    "number, decimal and date fields match exactly, and also take `cf_<name>__gte` and `cf_<name>__lte` (dates as "
    "YYYY-MM-DD) for a range. Any field also takes `cf_<name>__set=true|false` (holds a value or not); object fields "
    "take only that. A cf_ parameter that names no custom field of this kind is refused like any unknown parameter."
)


class Invalid(ValueError):
    """A value a field refuses; the message is shown as the field error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---- definitions --------------------------------------------------------------------------------------------------


def _org_id(org) -> Any:
    return getattr(org, "pk", org)


def definitions(org) -> list[CustomField]:
    """Every field of the organization, by weight then name; read once per unit of work (a request, job or command:
    lists, filters, snapshots and exports ask for them on every write), and again after a definition changes."""
    cache = organization_cache(_org_id(org))
    if "custom_fields" not in cache:
        cache["custom_fields"] = list(
            CustomField.objects.filter(organization_id=_org_id(org)).order_by("weight", "name", "id")
        )
    return cache["custom_fields"]


def lock_definitions(org) -> list[CustomField]:
    """Inside a record write's transaction: the organization's fields read again under FOR SHARE, so the write
    serialises with update_field and delete_field (FOR UPDATE on the field): a type change, a removed choice or a delete
    either waits for this write to commit and then sees its value, or commits first and this write validates against
    the new definition. Outside a transaction (a read, an import preview) the unit's cached list is used."""
    if not connection.in_atomic_block:
        return definitions(org)
    rows = list(
        CustomField.objects.raw(
            "SELECT * FROM catalog_customfield WHERE organization_id = %s ORDER BY weight, name, id FOR SHARE",
            [_org_id(org)],
        )
    )
    organization_cache(_org_id(org))["custom_fields"] = rows
    return rows


class _HasPath(Func):
    """custom_field_data @? '$."<name>"': the key is present. The jsonb_path_ops GIN index serves `@?` (and `@>`) but
    not `?`, so this is how every "has a value" query is written."""

    arg_joiner = " @? "
    template = "(%(expressions)s::jsonpath)"
    output_field = BooleanField()


def has_key(name: str) -> _HasPath:
    """A filter expression: the record holds a value for the field `name` (the path is a bound parameter)."""
    return _HasPath(F(KEY), Value(f'$."{name}"'))


def for_kind(org, kind: str) -> list[CustomField]:
    """The fields that apply to a record kind (application, component, integration, ai_agent) or to vendors."""
    return [cf for cf in definitions(org) if kind in cf.applies_to]


def forget(org) -> None:
    forget_organization(_org_id(org))


def column(cf: CustomField) -> str:
    return COLUMN + cf.name


# ---- values -------------------------------------------------------------------------------------------------------


def _depth(value, level=1) -> int:
    if isinstance(value, dict):
        return max([level, *(_depth(v, level + 1) for v in value.values())])
    if isinstance(value, list):
        return max([level, *(_depth(v, level + 1) for v in value)])
    return level - 1


def has_secret_key(value) -> bool:
    """Whether a JSON value has a key that looks like a secret (redaction.SNAPSHOT_DENYLIST), at any depth."""
    if isinstance(value, dict):
        return any(secret_like(k, SNAPSHOT_DENYLIST) or has_secret_key(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_secret_key(v) for v in value)
    return False


def _decimal(value) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise Invalid("Enter a number.")
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(str(value).strip())
        if not number.is_finite():
            raise Invalid("Enter a number, such as 12.5.")
        if number and abs(number.adjusted()) > DECIMAL_DIGITS + DECIMAL_PLACES:  # never format 1e9999999 in full
            raise Invalid(
                f"Use at most {DECIMAL_PLACES} decimal places."
                if number.adjusted() < 0
                else f"Use at most {DECIMAL_DIGITS} digits."
            )
        whole, _, places = format(number.normalize(), "f").lstrip("+-").partition(".")
    except ArithmeticError:  # InvalidOperation, Overflow: "1e9999999" is a value Decimal can hold but not format
        raise Invalid("Enter a number, such as 12.5.") from None
    if len(places) > DECIMAL_PLACES:
        raise Invalid(f"Use at most {DECIMAL_PLACES} decimal places.")
    if len(whole.lstrip("0")) + len(places) > DECIMAL_DIGITS:
        raise Invalid(f"Use at most {DECIMAL_DIGITS} digits.")
    return int(number) if number == number.to_integral_value() else float(number)


def clean_value(cf: CustomField, value: Any) -> Any:
    """`value` (as JSON sends it) in storage format, or None for empty. Raises Invalid with the field error."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    kind = cf.type
    if kind in (T.TEXT, T.LONGTEXT):
        if not isinstance(value, str):
            raise Invalid("Enter text.")
        text = value.strip()
        limit = TEXT_MAX if kind == T.TEXT else LONGTEXT_MAX
        if len(text) > limit:
            raise Invalid(f"Use {limit:,} characters or fewer.")
        if kind == T.TEXT and ("\n" in text or "\r" in text):
            raise Invalid("Use one line; a long text field takes more.")
        return text or None
    if kind == T.INTEGER:
        if isinstance(value, str) and INTEGER_PATTERN.match(value.strip()):
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise Invalid("Enter a whole number.")
        if abs(value) > INTEGER_MAX:
            raise Invalid(f"Use a whole number from -{INTEGER_MAX:,} to {INTEGER_MAX:,}.")
        return value
    if kind == T.DECIMAL:
        return _decimal(value)
    if kind == T.BOOLEAN:
        if not isinstance(value, bool):
            raise Invalid("Use true or false, or leave it empty.")
        return value
    if kind == T.DATE:
        text = value.strip() if isinstance(value, str) else ""
        try:
            if not DATE_PATTERN.match(text):
                raise ValueError(dates.FORMAT_MESSAGE)
            dates.parse_stored(text)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        return text
    if kind == T.URL:
        text = value.strip() if isinstance(value, str) else ""
        if len(text) > URL_MAX:
            raise Invalid(f"Use {URL_MAX:,} characters or fewer.")
        try:
            URLValidator(schemes=["http", "https"])(text)
        except ValidationError:
            raise Invalid(URL_MESSAGE) from None
        return text
    if kind == T.SELECT:
        if not isinstance(value, str) or value not in cf.choices:
            raise Invalid(f"Choose one of: {', '.join(cf.choices)}.")
        return value
    if kind == T.MULTISELECT:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise Invalid("Send the choices as a list.")
        if any(v not in cf.choices for v in value):
            raise Invalid(f"Choose any of: {', '.join(cf.choices)}.")
        return [c for c in cf.choices if c in value]
    if kind == T.OBJECT:
        if not isinstance(value, dict):
            raise Invalid("Send a JSON object.")
        if _depth(value) > OBJECT_DEPTH:
            raise Invalid(f"Nest at most {OBJECT_DEPTH} levels.")
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False)) > OBJECT_MAX:
            raise Invalid(f"Keep the object under {OBJECT_MAX:,} characters of JSON.")
        if has_secret_key(value):
            raise Invalid(DENIED_KEYS_MESSAGE)
        return value
    raise Invalid("This field's type isn't known.")


def _unknown(org, kind, key) -> str:
    other = next((cf for cf in definitions(org) if cf.name == key), None)
    if other is not None:
        return f"{other.label} doesn't apply to {KIND_NOUNS.get(kind, kind)}."
    return "There is no custom field with this name."


def clean(
    org,
    kind: str,
    current: dict | None,
    data: dict,
    errors: dict,
    *,
    creating: bool = False,
    required=True,
    lock=True,
):
    """The record's custom_field_data after this write, or None when the write doesn't touch it (an update whose data
    has no custom_field_data). Problems go into `errors` keyed custom_field_data.<name>. `required=False` skips the
    required check (vendors an import creates by name); `lock=False` reads the cached definitions without FOR SHARE (an
    import's preview, which writes nothing)."""
    if KEY not in data and not creating:
        return None
    applicable = lock_definitions(org) if lock else definitions(org)
    fields = {cf.name: cf for cf in applicable if kind in cf.applies_to}
    given = data.get(KEY)
    if given is not None and not isinstance(given, dict):
        errors[KEY] = ["Send custom fields as an object of field names and values."]
        return None
    given = given or {}
    result = {k: v for k, v in (current or {}).items() if k in fields}
    for key, value in given.items():
        cf = fields.get(key)
        if cf is None:
            errors[PREFIX + str(key)] = [_unknown(org, kind, key)]
            continue
        try:
            cleaned = clean_value(cf, value)
        except Invalid as e:
            errors[PREFIX + key] = [e.message]
            continue
        if cleaned is None:
            result.pop(key, None)
        else:
            result[key] = cleaned
    for cf in fields.values():
        if creating and cf.name not in given and cf.default is not None:
            result[cf.name] = cf.default
        if required and cf.required and cf.name not in result and (creating or cf.name in given):
            errors.setdefault(PREFIX + cf.name, [f"{cf.label} is required."])
    return {name: result[name] for name in fields if name in result}


def values_for(fields: list[CustomField], stored: dict | None) -> dict[str, Any]:
    """{name: value or None} for each field, in field order: what a record response shows."""
    stored = stored or {}
    return {cf.name: shown(cf, stored.get(cf.name)) for cf in fields}


def shown(cf: CustomField, value: Any) -> Any:
    """A stored value as the API and exports show it: an object holding a secret-looking key is never shown (secrets are never shown; such
    values are refused on write, so only a value written behind the service's back can hold one)."""
    if cf.type == T.OBJECT and has_secret_key(value):
        return None
    return value


def audit_values(org, kind: str, stored: dict | None) -> dict[str, Any]:
    """The custom fields of a snapshot or an audit payload: custom_field_data.<name> for every field that applies,
    None when unset. Only declared names; an object holding a secret-looking key is left out."""
    out: dict[str, Any] = {}
    stored = stored or {}
    for cf in for_kind(org, kind):
        value = stored.get(cf.name)
        if cf.type == T.OBJECT and has_secret_key(value):
            continue
        out[PREFIX + cf.name] = value
    return out


# ---- text (CSV and Excel) -----------------------------------------------------------------------------------------


def decimal_text(value) -> str:
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return format(number.normalize(), "f")


def to_text(cf: CustomField, value: Any) -> str:
    """A stored value as a CSV cell; "" for none."""
    value = shown(cf, value)
    if value is None:
        return ""
    if cf.type == T.BOOLEAN:
        return "true" if value else "false"
    if cf.type == T.MULTISELECT:
        return f"{SEPARATOR} ".join(value)
    if cf.type == T.DECIMAL:
        return decimal_text(value)
    if cf.type == T.OBJECT:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return str(value)


YES_NO = {"true": True, "yes": True, "false": False, "no": False}


def parse_text(cf: CustomField, raw: str) -> Any:
    """A CSV cell as the value clean_value takes; None for blank. Raises Invalid."""
    text = (raw or "").strip()
    if not text:
        return None
    if cf.type == T.BOOLEAN:
        if text.lower() not in YES_NO:
            raise Invalid("Use true or false, or leave it empty.")
        return YES_NO[text.lower()]
    if cf.type == T.MULTISELECT:
        return [part.strip() for part in text.split(SEPARATOR) if part.strip()]
    if cf.type == T.OBJECT:
        try:
            return json.loads(text)
        except ValueError:
            raise Invalid('Enter a JSON object, such as {"key": "value"}.') from None
    return text


def excel_value(cf: CustomField, value: Any) -> Any:
    """A stored value for a typed Excel cell: numbers, yes/no and dates as themselves, the rest as text."""
    value = shown(cf, value)
    if value is None:
        return None
    if cf.type in (T.INTEGER, T.DECIMAL, T.BOOLEAN, T.DATE):
        return value
    return to_text(cf, value)


def excel_sheet(org, kind: str, rows: list[Any]):
    """The workbook's "Custom fields" sheet (imports/xlsxio.EXTRA_SHEETS): one row per field
    that applies to the exported kind, naming its column, label, type and rules. None when the kind has none."""
    from imports import xlsxio

    fields = for_kind(org, kind)
    if not fields:
        return None
    columns = [
        xlsxio.Column("column"),
        xlsxio.Column("label"),
        xlsxio.Column("type"),
        xlsxio.Column("required", xlsxio.BOOLEAN),
        xlsxio.Column("choices"),
        xlsxio.Column("default"),
        xlsxio.Column("applies_to"),
        xlsxio.Column("description"),
    ]
    return xlsxio.Sheet(
        "Custom fields",
        columns,
        [
            {
                "column": column(cf),
                "label": cf.label,
                "type": T(cf.type).label,
                "required": cf.required,
                "choices": f"{SEPARATOR} ".join(cf.choices),
                "default": to_text(cf, cf.default),
                "applies_to": ", ".join(KIND_NOUNS[k] for k in cf.applies_to),
                "description": cf.description,
            }
            for cf in fields
        ],
    )


# ---- definitions: validation and writes ---------------------------------------------------------------------------

DEFINITION_FIELDS = (
    "name",
    "label",
    "type",
    "description",
    "choices",
    "required",
    "default",
    "applies_to",
    "weight",
    "is_reminder_source",
)


def holders() -> list[tuple[str, Any]]:
    """(kind, model) for each model other than Resource that holds custom field values: vendors, and
    contracts and licence pools (money/, which depends on catalog, so read through the app registry)."""
    from django.apps import apps

    return [
        ("vendor", Vendor),
        ("contract", apps.get_model("money", "Contract")),
        ("license_pool", apps.get_model("money", "LicensePool")),
    ]


def value_counts(org, name: str) -> dict[str, int]:
    """{kind: records holding a value for the field}, vendors under "vendor", contracts and licence pools under theirs."""
    rows = Resource.objects.filter(has_key(name), organization=org).values("kind").order_by().annotate(n=Count("id"))
    counts = {row["kind"]: row["n"] for row in rows}
    for kind, model in holders():
        n = model.objects.filter(has_key(name), organization=org).count()
        if n:
            counts[kind] = n
    return counts


def usage(org) -> dict[str, int]:
    """{field name: records, vendors, contracts and licence pools holding a value}, for every field of the
    organization, in one query."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT k, SUM(n)::int FROM (
                SELECT k, COUNT(*) AS n FROM catalog_resource, jsonb_object_keys(custom_field_data) AS k
                WHERE organization_id = %(org)s GROUP BY k
                UNION ALL
                SELECT k, COUNT(*) AS n FROM catalog_vendor, jsonb_object_keys(custom_field_data) AS k
                WHERE organization_id = %(org)s GROUP BY k
                UNION ALL
                SELECT k, COUNT(*) AS n FROM money_contract, jsonb_object_keys(custom_field_data) AS k
                WHERE organization_id = %(org)s GROUP BY k
                UNION ALL
                SELECT k, COUNT(*) AS n FROM money_licensepool, jsonb_object_keys(custom_field_data) AS k
                WHERE organization_id = %(org)s GROUP BY k
            ) AS counts GROUP BY k
            """,
            {"org": _org_id(org)},
        )
        return dict(cursor.fetchall())


def _records(n: int) -> str:
    return f"{n} record{'s' if n != 1 else ''}"


def _text_in(data, field, errors, limit, required_message=None) -> str:
    raw = data.get(field)
    if raw not in (None, "") and not isinstance(raw, str):
        errors[field] = ["Send text."]
        return ""
    value = (raw or "").strip()
    if required_message and not value:
        errors[field] = [required_message]
    elif len(value) > limit:
        errors[field] = [f"Use {limit:,} characters or fewer."]
    return value


def _bool_in(data, field, errors) -> bool:
    value = data.get(field)
    if not isinstance(value, bool):
        errors[field] = ["Use true or false."]
        return False
    return value


def _choices_in(value, errors) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        errors["choices"] = ["Send the choices as a list of text."]
        return []
    found: list[str] = []
    for raw in value:
        choice = raw.strip()
        if not choice:
            errors["choices"] = ["A choice can't be empty."]
        elif len(choice) > CHOICE_MAX:
            errors["choices"] = [f"Keep each choice to {CHOICE_MAX} characters or fewer."]
        elif "," in choice or SEPARATOR in choice:
            errors["choices"] = [
                "A choice can't contain a comma or a semicolon (they separate choices in filters and CSV)."
            ]
        elif choice.lower() in {c.lower() for c in found}:
            errors["choices"] = [f'"{choice}" is listed twice.']
        else:
            found.append(choice)
    if len(found) > CHOICES_MAX:
        errors["choices"] = [f"Use at most {CHOICES_MAX} choices."]
    return found


def _applies_in(value, errors) -> list[str]:
    allowed = ", ".join(CUSTOM_FIELD_KINDS)
    if not isinstance(value, list) or not all(isinstance(k, str) for k in value):
        errors["applies_to"] = [f"Send a list of any of: {allowed}."]
        return []
    if any(k not in CUSTOM_FIELD_KINDS for k in value):
        errors["applies_to"] = [f"Use any of: {allowed}."]
        return []
    if not value:
        errors["applies_to"] = ["Choose at least one kind of record."]
    return [k for k in CUSTOM_FIELD_KINDS if k in value]


def _definition(org, data, field=None) -> dict[str, Any]:
    """The definition after this write, validated as a whole; raises FieldErrors. A PATCH changes only what it names."""
    if not isinstance(data, dict):
        raise FieldErrors({"non_field": ["Send a JSON object."]})
    errors: dict[str, list[str]] = {}
    current = {f: getattr(field, f) for f in DEFINITION_FIELDS} if field is not None else {}
    values = dict(current)
    others = CustomField.objects.filter(organization=org)
    if field is not None:
        others = others.exclude(id=field.id)
    if field is None:
        name = _text_in(data, "name", errors, NAME_MAX, "Enter a name: lowercase letters, digits and underscores.")
        if name and "name" not in errors:
            if not NAME_PATTERN.match(name):
                errors["name"] = [
                    "Use lowercase letters, digits and single underscores, starting with a letter (business_case)."
                ]
            elif secret_like(name, SNAPSHOT_DENYLIST):
                errors["name"] = ["Custom fields can't hold secrets; choose a name without password, token, key..."]
            elif others.filter(name=name).exists():
                errors["name"] = [f"A custom field named {name} already exists."]
        values["name"] = name
    elif "name" in data and data["name"] != field.name:
        errors["name"] = ["A field's name can't change after it is created; change its label instead."]
    if "label" in data or field is None:
        values["label"] = _text_in(data, "label", errors, LABEL_MAX, "Enter a label.")
        clash = others.filter(label__iexact=values["label"]).first() if values["label"] else None
        if clash is not None and "label" not in errors:
            errors["label"] = [f"The field {clash.name} already has the label {clash.label}."]
    if "description" in data:
        values["description"] = _text_in(data, "description", errors, DESCRIPTION_MAX)
    if "type" in data or field is None:
        if data.get("type") not in T.values:
            errors["type"] = [f"Choose one of: {', '.join(T.values)}."]
        else:
            values["type"] = data["type"]
    kind = values.get("type")
    if "choices" in data:
        values["choices"] = _choices_in(data["choices"], errors)
    elif kind not in CHOICE_TYPES:
        values["choices"] = []
    if kind in CHOICE_TYPES and not values.get("choices") and "choices" not in errors and "type" not in errors:
        errors["choices"] = ["A choice field needs at least one choice."]
    elif kind not in CHOICE_TYPES and values.get("choices") and "type" not in errors:
        errors["choices"] = ["Only choice fields take choices."]
    for flag in ("required", "is_reminder_source"):
        if flag in data:
            values[flag] = _bool_in(data, flag, errors)
        elif field is None:
            values[flag] = False
    if values.get("is_reminder_source") and kind != T.DATE and "type" not in errors:
        errors["is_reminder_source"] = ["Only date fields can be reminder sources."]
    if "applies_to" in data or field is None:
        values["applies_to"] = _applies_in(data.get("applies_to"), errors)
        had = list(field.applies_to) if field is not None else []
        gated = [k for k in values["applies_to"] if k in CONTRACT_KINDS and k not in had]
        if gated and "applies_to" not in errors and not has_capability(org, CONTRACTS):
            errors["applies_to"] = ["Contracts and license pools need the contracts capability."]
    if "weight" in data:
        weight = data["weight"]
        if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= WEIGHT_MAX:
            errors["weight"] = [f"Use a whole number from 0 to {WEIGHT_MAX}."]
        else:
            values["weight"] = weight
    elif field is None:
        values["weight"] = 100
    if ("default" in data or field is None or "type" in data or "choices" in data) and not errors:
        probe = CustomField(**{**values, "default": None})
        raw = data["default"] if "default" in data else values.get("default")
        try:
            values["default"] = clean_value(probe, raw)
        except Invalid as e:
            errors["default"] = [e.message if "default" in data else f"The default no longer fits: {e.message}"]
    if field is not None and not errors and _touches_values(field, values):
        _check_values(org, field, values, errors)
    if errors:
        raise FieldErrors(errors)
    return values


def _in_use(org, name: str) -> list[Any]:
    """Every stored value of the field: records, vendors, contracts and licence pools."""
    found = list(Resource.objects.filter(has_key(name), organization=org).values_list(KEY, flat=True))
    for _kind, model in holders():
        found += list(model.objects.filter(has_key(name), organization=org).values_list(KEY, flat=True))
    return [data[name] for data in found]


def _touches_values(field, values) -> bool:
    """Whether the change could strand stored values: the type, the choices or the kinds. A label, description, weight,
    required, default or reminder change can't, so it needs no scan of the records."""
    return (
        values["type"] != field.type
        or list(values["choices"]) != list(field.choices)
        or list(values["applies_to"]) != list(field.applies_to)
    )


def _check_values(org, field, values, errors) -> None:
    """The rules for changing a field that records already use (module docstring)."""
    counts = value_counts(org, field.name)
    total = sum(counts.values())
    if not total:
        return
    if values["type"] != field.type:
        errors["type"] = [
            f"{_records(total)} hold a value for {field.label}, so its type can't change. Clear the values first, or "
            "add a new field."
        ]
        return
    gone = [k for k in field.applies_to if k not in values["applies_to"] and counts.get(k)]
    if gone:
        errors["applies_to"] = [
            "Values exist for "
            + ", ".join(f"{counts[k]} {KIND_NOUNS[k]}" if counts[k] != 1 else f"1 of the {KIND_NOUNS[k]}" for k in gone)
            + ". Clear them before removing the kind."
        ]
    removed = [c for c in field.choices if c not in values["choices"]]
    if removed and field.type in CHOICE_TYPES:
        used: dict[str, int] = {}
        for value in _in_use(org, field.name):
            for choice in value if isinstance(value, list) else [value]:
                if choice in removed:
                    used[choice] = used.get(choice, 0) + 1
        if used:
            errors["choices"] = [
                "Still in use: "
                + ", ".join(f"{c} ({_records(n)})" for c, n in used.items())
                + ". Change those records before removing the choice."
            ]


def _audited(field) -> dict[str, Any]:
    return {f: getattr(field, f) for f in DEFINITION_FIELDS if f != "name"}


def _save(field) -> None:
    """Save, with a concurrent request that took the same name or label reported as the field error it would have
    met had it come second (the database constraints decide)."""
    try:
        with transaction.atomic():
            field.save()
    except IntegrityError as e:
        if "catalog_customfield_unique_label" in str(e):
            raise FieldErrors({"label": ["Another custom field already has this label."]}) from None
        if "catalog_customfield_unique_name" in str(e):
            raise FieldErrors({"name": [f"A custom field named {field.name} already exists."]}) from None
        raise


class _RemoveKey(Func):
    """jsonb - text: the object without one key."""

    arg_joiner = " - "
    template = "(%(expressions)s)"
    output_field = JSONField()


def _clear_values(org, name: str) -> int:
    """Remove the key `name` from every record, vendor, contract and licence pool of the organization; returns how
    many held it."""
    removed = _RemoveKey(F(KEY), Cast(Value(name), TextField()))
    now = timezone.now()
    cleared = 0
    for model in (Resource, *(m for _kind, m in holders())):
        cleared += model.objects.filter(has_key(name), organization=org).update(
            custom_field_data=removed, updated_at=now
        )
    return cleared


SUFFIXES = ("", "__gte", "__lte", "__set")


def _prune_saved_filters(org, name: str, *, kinds=None, choices=()) -> int:
    """Keep saved filters valid when a field changes: without `choices`, drop every cf_<name> parameter (of the saved
    filters of `kinds`, or all); with `choices`, drop only those values from cf_<name> lists (and the parameter once
    it is empty). Saved filters are queries, not records: they aren't audited one by one, the field's event counts
    them. Returns how many changed."""
    from .models import SavedFilter

    params = [COLUMN + name + suffix for suffix in SUFFIXES]
    rows = SavedFilter.objects.filter(organization=org, query__has_any_keys=params)
    if kinds is not None:
        rows = rows.filter(kind__in=list(kinds))
    changed = 0
    for saved in rows.select_for_update().only("id", "query"):
        query = saved.query if isinstance(saved.query, dict) else {}
        kept: dict[str, Any] = {}
        for key, value in query.items():
            if key not in params:
                kept[key] = value
            elif choices and key == COLUMN + name and isinstance(value, str):
                left = [v for v in (part.strip() for part in value.split(",")) if v and v not in choices]
                if left:
                    kept[key] = ",".join(left)
            elif choices:
                kept[key] = value
        if kept != query:
            SavedFilter.objects.filter(id=saved.id).update(query=kept, updated_at=timezone.now())
            changed += 1
    return changed


def _prune_saved_view_columns(org, name: str, *, kinds=None) -> int:
    """Drop the cf_<name> column from saved views (of `kinds`, or all) when the field is deleted or stops applying to
    a kind. Returns how many views changed; like the query pruning, not audited one by one."""
    from .models import SavedFilter

    column = COLUMN + name
    rows = SavedFilter.objects.filter(organization=org, columns__contains=[column])
    if kinds is not None:
        rows = rows.filter(kind__in=list(kinds))
    changed = 0
    for saved in rows.select_for_update().only("id", "columns"):
        SavedFilter.objects.filter(id=saved.id).update(
            columns=[c for c in saved.columns if c != column], updated_at=timezone.now()
        )
        changed += 1
    return changed


@transaction.atomic
def create_field(org, actor, data):
    """A new field starts with no values: any key of the same name left in records (written behind the service's
    back) is cleared first, so a deleted field's values can't come back under a new definition."""
    from .services import audit

    field = CustomField(organization=org, **_definition(org, data))
    limits.check_room(org, limits.OTHER_RECORDS)
    _clear_values(org, field.name)
    _save(field)
    forget(org)
    audit(org, actor, "custom_field.created", field, {"name": field.name, "label": field.label, "type": field.type})
    return field


@transaction.atomic
def update_field(org, actor, field, data):
    from .services import audit

    field = CustomField.objects.select_for_update().get(id=field.id)
    before = _audited(field)
    values = _definition(org, data, field)
    # What saved filters lose: every parameter after a type change, a removed kind's parameters, removed choices.
    pruned = 0
    if values["type"] != field.type:
        pruned += _prune_saved_filters(org, field.name)
    else:
        gone = [k for k in field.applies_to if k not in values["applies_to"]]
        if gone:
            pruned += _prune_saved_filters(org, field.name, kinds=gone)
            pruned += _prune_saved_view_columns(org, field.name, kinds=gone)
        removed = [c for c in field.choices if c not in values["choices"]]
        if removed and field.type in CHOICE_TYPES:
            pruned += _prune_saved_filters(org, field.name, choices=removed)
    for name, value in values.items():
        setattr(field, name, value)
    _save(field)
    forget(org)
    after = _audited(field)
    changes: dict[str, Any] = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if pruned:
        changes["saved_filters_changed"] = pruned
    audit(org, actor, "custom_field.updated", field, {"name": field.name, **changes})
    return field


@transaction.atomic
def delete_field(org, actor, field):
    """Removes the field's values from every record and vendor (their ETags change) and its parameters from every
    saved filter, then the field; one audit event records how many of each. The records' own histories keep the
    values they had."""
    from .services import audit

    field = CustomField.objects.select_for_update().get(id=field.id)
    cleared = _clear_values(org, field.name)
    pruned = _prune_saved_filters(org, field.name) + _prune_saved_view_columns(org, field.name)
    audit(
        org,
        actor,
        "custom_field.deleted",
        field,
        {"name": field.name, "label": field.label, "cleared": cleared, "saved_filters_changed": pruned},
    )
    field.delete()
    forget(org)


# ---- Reminders (reads this) ---------------------------------------------------------------------------------------


class ReminderSource(NamedTuple):
    record: Any  # a Resource, a Vendor, a Contract or a LicensePool
    field: CustomField
    due: datetime.date


def reminder_sources(org, ids=None) -> list[ReminderSource]:
    """Every date held by a field flagged `is_reminder_source`, on unarchived records and on vendors, soonest first:
    what the reminder generator (notify/reminders.py) turns into reminders. One query per flagged field and model.
    `ids`: only these records, vendors, contracts and licence pools (the generator's on-save run for one source)."""
    found: list[ReminderSource] = []
    for cf in definitions(org):
        if not cf.is_reminder_source or cf.type != T.DATE:
            continue
        others = dict(holders())
        if not has_capability(org, CONTRACTS):  # contracts and pools are out of reach without the capability
            others = {k: m for k, m in others.items() if k not in CONTRACT_KINDS}
        kinds = [k for k in cf.applies_to if k not in dict(holders())]
        sources: list[Any] = []
        only = {} if ids is None else {"id__in": list(ids)}
        if kinds:
            sources += Resource.objects.filter(
                has_key(cf.name), organization=org, kind__in=kinds, archived_at__isnull=True, **only
            )
        for kind, model in others.items():
            if kind in cf.applies_to:
                held = model.objects.filter(has_key(cf.name), organization=org, **only)
                if kind == "contract":  # archived contracts remind nobody, as archived records don't
                    held = held.filter(archived_at__isnull=True)
                sources += held
        for record in sources:
            try:
                due = datetime.date.fromisoformat(record.custom_field_data[cf.name])
            except (TypeError, ValueError):
                continue
            found.append(ReminderSource(record, cf, due))
    return sorted(found, key=lambda s: (s.due, s.field.name, str(s.record.pk)))
