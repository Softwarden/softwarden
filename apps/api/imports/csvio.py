"""CSV in and out: the fixed template, reading an upload safely, the reversible formula escape, and a
record's current values in template columns. Import and export both go through here, so an export re-imports with
every row unchanged.

Escape: a cell starting with `=`, `+`, `-`, `@`, tab, CR or `'` gets one leading `'` on export; import removes
exactly one leading `'` from every cell. `-Internal` exports as `'-Internal` and `'-Internal` as `''-Internal`; both
come back as they were. Formulas are never evaluated: everything is text."""

import csv
import io

from catalog import compliance, customfields
from catalog.models import Kind

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 300  # headings, and cells on any line: a narrow file can't be made into huge row dicts
# The three columns the import's error report puts in front of the file's own (imports/importer.error_report); a
# file with MAX_COLUMNS of its own plus these still re-uploads, and the importer ignores them.
REPORT_COLUMNS = ["import_row", "import_outcome", "import_errors"]
ESCAPE_START = ("=", "+", "-", "@", "\t", "\r", "'")

COMMON = [
    "softwarden_id",
    "external_key",
    "name",
    "lifecycle",
    "criticality",
    "description",
    "business_owner_email",
    "business_owner_name",
    "technical_owner_email",
    "technical_owner_name",
    "support_team",
    "it_steward_email",
    "department",
    "cost_center",
    "data_classification",
]
TEMPLATE: dict[str, list[str]] = {
    Kind.APPLICATION: [
        *COMMON,
        "purpose",
        "vendor",
        "built_by",
        "license_model",
        "documentation_url",
        "support_url",
        "category",
        "deployment_model",
        "version",
        "primary_url",
        "admin_url",
        "authentication_method",
        "sso_enabled",
        "scim_provisioning",
        "entra_app_id",
        "service_principal_id",
        "tags",
    ],
    Kind.COMPONENT: [
        *COMMON,
        "component_type",
        "purpose",
        "host_runtime",
        "repository_url",
        "documentation_url",
        "built_by",
        "license_model",
        "hosting",
        "runbook_url",
        "schedule_or_trigger",
        "environment",
        "azure_resource_id",
        "pp_environment_id",
        "external_id",
        "tags",
    ],
    # AI agents: exported (CSV and Excel), not imported yet (catalog.models.IMPORT_KINDS). Models are written
    # as "Provider Model version" and channels as values, both separated by TAG_SEPARATOR.
    Kind.AI_AGENT: [
        *COMMON,
        "purpose",
        "platform",
        "models_used",
        "environment",
        "pp_environment_id",
        "channels",
        "knowledge_sources",
        "tools_connectors",
        "auth_mode",
        "run_as_identity",
        "data_access_scope",
        "human_in_loop",
        "actions_can_write",
        "risk_classification",
        "repo_url",
        "external_id",
        "tags",
    ],
    # Contracts and licence pools. The contracts export (money/contract_viewsets.py)
    # writes these columns but external_key and create_renewal_obligation, so an export re-imports; the derived
    # columns (annualized_cost, cancellation_deadline, last_synced) are read back and ignored, never set. Lines are
    # "Name=40; Name" (LINE_SEPARATOR, "="), a record named by name, kind:name (application:, component:, agent:) or
    # its Softwarden ID.
    "contract": [
        "softwarden_id",
        "external_key",
        "name",
        "vendor",
        "contract_number",
        "po_number",
        "amount",
        "currency",
        "billing_frequency",
        "annualized_cost",
        "start_date",
        "end_date",
        "auto_renew",
        "notice_value",
        "notice_unit",
        "cancellation_deadline",
        "renewal_owner_email",
        "renewal_owner_name",
        "status",
        "cost_center",
        "renewal",
        "create_renewal_obligation",
        "items",
        "notes",
    ],
    "license_pool": [
        "softwarden_id",
        "external_key",
        "application",
        "contract",
        "sku_name",
        "sku_id",
        "seats_purchased",
        "seats_assigned",
        "seat_sync",
        "last_synced",
        "notes",
    ],
}
# Columns of a kind that are worked out, not entered: exported, read back and ignored by an import.
DERIVED_COLUMNS = {"annualized_cost", "cancellation_deadline", "last_synced"}
# The column a row's name is in (an import needs it mapped); a licence pool is named by its SKU.
NAME_COLUMN = {"license_pool": "sku_name"}


def name_column(kind):
    return NAME_COLUMN.get(kind, "name")


# The compliance profile's columns (catalog/compliance.py), at the end of the template while the profile is on.
COMPLIANCE_COLUMNS = list(compliance.FIELDS)
# Columns that name something rather than hold a value; the import resolves them (owners, team, vendor, steward,
# department, category).
REFERENCE_COLUMNS = {
    "business_owner_email",
    "business_owner_name",
    "technical_owner_email",
    "technical_owner_name",
    "support_team",
    "it_steward_email",
    "vendor",
    "department",
    "category",
}
BOOLEAN_COLUMNS = {
    "sso_enabled",
    "scim_provisioning",
    "is_gxp",
    "human_in_loop",
    "actions_can_write",
}  # true / false, blank for not recorded


def columns(kind, with_compliance=False, custom=()):
    """The template's columns for a kind, with the compliance columns when the organization's profile is on and one
    cf_<name> column per custom field that applies (`custom`: catalog/customfields.for_kind), last."""
    record = kind in (Kind.APPLICATION, Kind.COMPONENT, Kind.AI_AGENT)
    return [
        *TEMPLATE[kind],
        *(COMPLIANCE_COLUMNS if with_compliance and record else []),
        *(customfields.column(cf) for cf in custom),
    ]


def columns_for(org, kind):
    return columns(kind, compliance.enabled(org), customfields.for_kind(org, kind))


TAG_SEPARATOR = ";"


class CsvRejected(Exception):
    """The whole file is refused. The message is shown as is."""


def escape_cell(value):
    """A cell a spreadsheet would read as a formula gets a leading `'`: one that starts with a formula character, or
    with whitespace before one (" =1+1"; spreadsheets skip the space)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(ESCAPE_START) or text.lstrip().startswith(ESCAPE_START) else text


def unescape_cell(text):
    return text[1:] if text.startswith("'") else text


def write_csv(columns, rows):
    """UTF-8 with a BOM, every cell escaped. `rows` are dicts keyed by column."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([escape_cell(row.get(c, "")) for c in columns])
    return ("﻿" + out.getvalue()).encode("utf-8")


def read_csv(data):
    """(headers, rows) from an upload: rows are dicts of unescaped, stripped text keyed by header, with blank lines
    skipped. Raises CsvRejected for anything that can't be imported at all."""
    if not data:
        raise CsvRejected("The file is empty.")
    if len(data) > MAX_BYTES:
        raise CsvRejected("The file is larger than 5 MB. Split it into smaller files.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CsvRejected(
            f"This file isn't UTF-8 (the byte at position {e.start} can't be read). Save it as 'CSV UTF-8' and try again."
        ) from None
    lines = []
    try:
        for line in csv.reader(io.StringIO(text, newline="")):  # streamed: a wide line is refused as it is read
            while line and not line[-1].strip():  # an Excel "used range" pads lines with empty cells
                line.pop()
            if len(line) > MAX_COLUMNS + len(REPORT_COLUMNS):
                raise CsvRejected(f"A line has more than {MAX_COLUMNS} cells; the limit is {MAX_COLUMNS} columns.")
            if line:
                lines.append(line)
    except csv.Error as e:
        raise CsvRejected(f"The file isn't valid CSV: {e}.") from None
    if not lines:
        raise CsvRejected("The file is empty.")
    headers = [h.strip() for h in lines[0]]
    if len([h for h in headers if h.lower() not in REPORT_COLUMNS]) > MAX_COLUMNS:
        raise CsvRejected(f"The file has more than {MAX_COLUMNS} columns; the limit is {MAX_COLUMNS} columns.")
    if len(set(h.lower() for h in headers if h)) != len([h for h in headers if h]):
        raise CsvRejected("Two columns have the same heading. Rename or remove one.")
    body = lines[1:]
    if not body:
        raise CsvRejected("The file has headings but no rows.")
    if len(body) > MAX_ROWS:
        raise CsvRejected(f"The file has {len(body):,} rows; the limit is 5,000. Split it into smaller files.")
    named = [(i, h) for i, h in enumerate(headers) if h]  # row dicts are built from non-blank headings only
    rows = []
    for number, line in enumerate(body, start=2):  # numbered as the import numbers rows (importer.create_run)
        if any(cell.strip() for cell in line[len(headers) :]):  # never dropped without a word
            raise CsvRejected(
                f"Row {number} has more cells than there are headings ({len(line)} cells, {len(headers)} headings). "
                "Add a heading for the extra column or remove the extra cells."
            )
        cells = line + [""] * (len(headers) - len(line))
        rows.append({h: unescape_cell(cells[i].strip()) for i, h in named})
    return headers, rows


def is_template(headers, kind, with_compliance=False, custom=()):
    """Whether the headings are the fixed template's (in any order, extra blank headings ignored), custom field
    columns included. If not, the run needs a column mapping."""
    named = {h.strip().lower() for h in headers if h.strip()}
    return name_column(kind) in named and named <= set(columns(kind, with_compliance, custom))


def tags_text(names):
    return f"{TAG_SEPARATOR} ".join(sorted(names, key=str.lower))


def split_tags(text):
    seen: dict[str, str] = {}
    for part in (text or "").split(TAG_SEPARATOR):
        name = part.strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def yes_no_text(value):
    return "" if value is None else ("true" if value else "false")


def model_label(model):
    """An AI model in a cell: provider, name and version."""
    return " ".join(part for part in (model.provider.name, model.name, model.version) if part)


def record_values(resource, tag_names=(), with_compliance=False, custom=()):
    """A record's current values in template columns (strings, "" for none), for export and for the import's
    unchanged/update comparison. Owners are written as email and name; the steward as an email; the department and
    category by name; yes/no fields as true or false; custom fields as customfields.to_text writes them."""
    ext = resource.extension
    wanted = columns(resource.kind, with_compliance, custom)
    values = {
        "softwarden_id": str(resource.id),
        "external_key": "",
        "name": resource.name,
        "lifecycle": resource.lifecycle or "",
        "criticality": resource.criticality or "",
        "description": resource.description or "",
        "support_team": resource.support_team.name if resource.support_team_id else "",
        "it_steward_email": resource.it_steward.email if resource.it_steward_id else "",
        "tags": tags_text(tag_names),
        "department": resource.department.name if resource.department_id else "",
        "cost_center": resource.cost_center or "",
        "data_classification": resource.data_classification or "",
    }
    if with_compliance:
        values.update({f: compliance.as_text(getattr(resource, f)) for f in COMPLIANCE_COLUMNS})
    stored = resource.custom_field_data or {}
    values.update({customfields.column(cf): customfields.to_text(cf, stored.get(cf.name)) for cf in custom})
    for role in ("business_owner", "technical_owner"):
        person = getattr(resource, role)
        values[f"{role}_email"] = person.email if person else ""
        values[f"{role}_name"] = person.name if person else ""
    for column in wanted:
        if column not in values and ext is not None:
            if column == "vendor":
                values[column] = ext.vendor.name if ext.vendor_id else ""
            elif column == "category":
                values[column] = ext.category.name if ext.category_id else ""
            elif column == "models_used":  # read from the queryset's prefetch where there is one
                values[column] = f"{TAG_SEPARATOR} ".join(model_label(m) for m in ext.models_used.all())
            elif column == "channels":
                values[column] = f"{TAG_SEPARATOR} ".join(ext.channels)
            elif column in BOOLEAN_COLUMNS:
                values[column] = yes_no_text(getattr(ext, column, None))
            else:
                values[column] = getattr(ext, column, "") or ""
    return {c: values.get(c, "") for c in wanted}
