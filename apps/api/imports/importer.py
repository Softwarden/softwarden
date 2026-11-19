"""CSV import: sources, upload, validate, match, preview, commit, cancel, error report, purge.

A run keeps the uploaded bytes and each row's raw cells in PostgreSQL until the purge. Every preview, and the
commit itself, is one pure planning pass over those rows (`plan_run`): the preview saves what the pass found; the
commit plans again against the database as it is then, refuses if that differs from what the user saw, and only
then writes, through the same services as the record API. Names never match records or People.

`run.decisions` holds the user's choices ({"people"|"teams"|"vendors": {key: "create" | "gap"}, "matches":
{row number: "create" | resource id}}) and, under "derived", what the last preview found (the panels and the IDs of
People the file mentions, which the anonymize hook needs because an anonymized Person has lost its email and name)."""

import logging
import uuid
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from catalog import admin_services, compliance, customfields, services, tags
from catalog.models import (
    Application,
    AuthenticationMethod,
    BuiltBy,
    Component,
    ComponentType,
    Criticality,
    DataClassification,
    Department,
    DeploymentModel,
    Environment,
    Hosting,
    Kind,
    LicenseModel,
    Lifecycle,
    Person,
    RecordReview,
    Resource,
    RiskTier,
    SoftwareCategory,
    Team,
    ValidationStatus,
    Vendor,
)
from catalog.state import CONFIRMED_FIELDS, confirmed_snapshot
from core import capabilities, limits
from core.models import Membership
from core.problems import Conflict, FieldErrors
from money.models import Contract, LicensePool

from . import csvio
from .models import (
    KINDS,
    MONEY_KINDS,
    ExternalIdentifier,
    ImportRow,
    ImportRun,
    ImportSource,
    ImportStatus,
    RowOutcome,
)

log = logging.getLogger("softwarden.imports")

KEEP_DAYS = 30
REFUSED = "/problems/import-refused"
PURGED_MESSAGE = "File purged after 30 days"
# Columns the error report adds in front; an upload of a fixed report ignores them.
REPORT_COLUMNS = csvio.REPORT_COLUMNS
CHOICES: dict[str, Any] = {
    "lifecycle": Lifecycle,
    "criticality": Criticality,
    "built_by": BuiltBy,
    "license_model": LicenseModel,
    "component_type": ComponentType,
    "data_classification": DataClassification,
    "deployment_model": DeploymentModel,
    "authentication_method": AuthenticationMethod,
    "hosting": Hosting,
    "environment": Environment,
    "validation_status": ValidationStatus,
    "risk_tier": RiskTier,
}
VALUE_FIELDS = [
    "name",
    "lifecycle",
    "criticality",
    "description",
    "purpose",
    "component_type",
    "host_runtime",
    "repository_url",
    "documentation_url",
    "support_url",
    "built_by",
    "license_model",
    "cost_center",
    "data_classification",
    "deployment_model",
    "version",
    "primary_url",
    "admin_url",
    "authentication_method",
    "sso_enabled",
    "scim_provisioning",
    "entra_app_id",
    "service_principal_id",
    "hosting",
    "runbook_url",
    "schedule_or_trigger",
    "environment",
    "azure_resource_id",
    "pp_environment_id",
    "external_id",
    *compliance.FIELDS,  # only in the template while the profile is on
]
# Fields on the shared record (Resource) rather than the kind's extension row.
RESOURCE_FIELDS = {"name", "lifecycle", "criticality", "description", "cost_center", "data_classification"}
RESOURCE_FIELDS |= set(compliance.FIELDS)
GUID_COLUMNS = services.GUID_FIELDS  # stored lower-case
ROLES = ("business_owner", "technical_owner")
GAP_LABELS = {
    "business_owner": "No business owner",
    "technical_owner": "No technical owner",
    "support_team": "No support team",
}


class Gone(APIException):
    status_code = 410
    default_detail = PURGED_MESSAGE


class FileRejected(FieldErrors):
    """The whole upload is refused; the message is the problem's detail."""

    def __init__(self, message):
        super().__init__({"file": [message]})
        self.detail = message


KIND_MESSAGE = "Choose application, component, contract or license_pool."


def require_kind(org, kind):
    """Contracts and licence pools need the organization's `contracts` capability (403 problem otherwise)."""
    if kind in MONEY_KINDS:
        capabilities.require_capability(org, capabilities.CONTRACTS)


class Refused(Exception):
    def __init__(self, reason, extra=None):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra or {}


# ---- Import sources ----------------------------------------------------------------------------------------------


def _source_fields(org, data, source=None):
    errors: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        raise FieldErrors({"non_field": ["Send a JSON object."]})
    name = data.get("name", source.name if source else "")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        errors["name"] = ["Enter a name."]
    elif len(name) > 200:
        errors["name"] = ["Use 200 characters or fewer."]
    else:
        clash = ImportSource.objects.filter(organization=org, name__iexact=name)
        if source is not None:
            clash = clash.exclude(id=source.id)
        twin = clash.first()
        if twin is not None:
            errors["name"] = [f"An import source named {twin.name} already exists."]
    kind = data.get("kind", source.kind if source else None)
    if source is None and kind not in KINDS:
        errors["kind"] = [KIND_MESSAGE]
    elif source is not None and kind != source.kind:
        errors["kind"] = ["An import source's kind can't change; create another source."]
    if errors:
        raise FieldErrors(errors)
    if source is None:
        require_kind(org, kind)
    return name, kind


@transaction.atomic
def create_source(org, actor, data):
    name, kind = _source_fields(org, data)
    limits.check_room(org, limits.OTHER_RECORDS)
    source = ImportSource.objects.create(organization=org, name=name, kind=kind)
    services.audit(org, actor, "import_source.created", source, {"name": name, "kind": kind})
    return source


@transaction.atomic
def update_source(org, actor, source, data):
    """Rename only; the column mapping is saved through its own endpoint."""
    before = source.name
    name, _ = _source_fields(org, data, source)
    if name != before:
        source.name = name
        source.save(update_fields=["name", "updated_at"])
        services.audit(org, actor, "import_source.updated", source, {"name": [before, name]})
    return source


@transaction.atomic
def delete_source(org, actor, source):
    keys = ExternalIdentifier.objects.filter(source=source).count()
    services.refuse_while_referenced(
        source,
        {"external identifier": keys} if keys else {},
        "Its keys are how re-imports find the same records.",
    )
    services.audit(org, actor, "import_source.deleted", source, {"name": source.name, "kind": source.kind})
    source.delete()


# ---- Upload ------------------------------------------------------------------------------------------------------


def parse_upload(kind, data, with_compliance=False, custom=()):
    """(headers, rows, is_template) of an upload. Template headings give lower-cased headers and rows keyed by template
    column; any other headings are kept as in the file, for the user to map. The compliance columns are template
    columns only while the organization's profile is on; the cf_<name> columns of the custom fields that apply
    (`custom`) are template columns too. Raises FileRejected."""
    try:
        headers, rows = csvio.read_csv(data)
    except csvio.CsvRejected as e:
        raise FileRejected(str(e)) from None
    headers = [h for h in headers if h and h.lower() not in REPORT_COLUMNS]
    if not headers:
        raise FileRejected("The file has no column headings.")
    if csvio.is_template(headers, kind, with_compliance, custom):
        allowed = set(csvio.columns(kind, with_compliance, custom))
        cleaned = [{k.lower(): v for k, v in row.items() if k.lower() in allowed} for row in rows]
        return [h.lower() for h in headers], cleaned, True
    kept = set(headers)
    return headers, [{k: v for k, v in row.items() if k in kept} for row in rows], False


def parse_run(org, kind, data, source=None):
    """Check and parse an upload before anything is written: (headers, rows, is_template). Raises FileRejected,
    FieldErrors. The upload view calls it before opening its transaction, then create_run with it."""
    if kind not in KINDS:
        raise FieldErrors({"kind": [KIND_MESSAGE]})
    require_kind(org, kind)
    if source is not None and source.kind != kind:
        raise FieldErrors({"source": [f"This source is for {source.kind}s; choose another or none."]})
    return parse_upload(kind, data, compliance.enabled(org), customfields.for_kind(org, kind))


def create_run(org, actor, kind, filename, data, source=None, parsed=None):
    """Store an upload and parse it (or take parse_run's result as `parsed`). Template headings are previewed at
    once; other headings leave the run `uploaded` until its columns are mapped (`map_run`). Raises FileRejected,
    FieldErrors."""
    headers, rows, is_template = parsed if parsed is not None else parse_run(org, kind, data, source)
    with transaction.atomic():
        run = ImportRun.objects.create(
            organization=org,
            kind=kind,
            source=source,
            uploaded_by=actor,
            filename=(filename or "import.csv")[:255],
            file_bytes=bytes(data),
            headers=headers,
            purge_after=services.org_today(org) + timedelta(days=KEEP_DAYS),
        )
        ImportRow.objects.bulk_create(
            [
                ImportRow(organization=org, run=run, row_number=n, raw=raw, outcome=RowOutcome.CREATE)
                for n, raw in enumerate(rows, start=2)  # row 1 is the headings, as a spreadsheet numbers them
            ]
        )
        if is_template:
            preview(run, actor)
        services.audit(org, actor, "import.uploaded", run, {"kind": kind, "rows": len(rows)})
    return run


# ---- Column mapping ------------------------------------------------------------------------------------------------

IGNORE = "ignore"
FIELD_LABELS = {"softwarden_id": "Softwarden ID", "it_steward_email": "IT steward email"}


def _norm(text):
    return "".join(ch for ch in text.lower() if ch not in " _-\t")


def mapping_fields(kind, with_compliance=False, custom=()):
    """The targets a column can map to (besides ignore): the kind's template columns, custom fields by their label."""
    labels = {customfields.column(cf): cf.label for cf in custom}
    return [
        {
            "value": c,
            "label": labels.get(c) or FIELD_LABELS.get(c) or _column_label(c),
            "required": c == csvio.name_column(kind),
        }
        for c in csvio.columns(kind, with_compliance, custom)
    ]


def suggest_mapping(kind, headers, source=None, with_compliance=False, custom=()):
    """{header: target}: the source's saved mapping where it has the header, else a name match (a custom field's
    column or its label), else ignore."""
    targets = set(csvio.columns(kind, with_compliance, custom))
    saved = (source.column_mapping if source is not None else None) or {}
    out: dict[str, str] = {}
    taken: set[str] = set()
    for h in headers:
        t = saved.get(h)
        if isinstance(t, str) and (t in targets or t == IGNORE) and (t == IGNORE or t not in taken):
            out[h] = t
            taken.add(t)
    by_norm = {_norm(c): c for c in csvio.columns(kind, with_compliance, custom)}
    for cf in custom:
        by_norm.setdefault(_norm(cf.label), customfields.column(cf))
    for h in headers:
        if h in out:
            continue
        t = by_norm.get(_norm(h))
        if t == "external_key" and source is None:
            t = None
        if t is not None and t not in taken:
            out[h] = t
            taken.add(t)
        else:
            out[h] = IGNORE
    return {h: out[h] for h in headers}


def needs_mapping(run):
    return run.status == ImportStatus.UPLOADED and run.purged_at is None


@transaction.atomic
def map_run(org, actor, run, data):
    """Apply a column mapping to an uploaded run and preview it. data: {"mapping": {header: target}, "save_mapping": bool}."""
    require_kind(org, run.kind)
    run = ImportRun.objects.select_for_update().get(id=run.id)
    _require_file(run)
    if run.status == ImportStatus.PREVIEWED:
        raise Conflict("Upload the file again to change the mapping.")
    if run.status != ImportStatus.UPLOADED:
        raise Conflict(f"This import is {run.status}; only an uploaded import can be mapped.")
    given = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(given, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in given.items()):
        raise FieldErrors({"mapping": ["Send the mapping as {heading: column}."]})
    custom = customfields.for_kind(org, run.kind)
    labels = {f["value"]: f["label"] for f in mapping_fields(run.kind, compliance.enabled(org), custom)}
    problems: list[str] = []
    mapping: dict[str, str] = {}
    users: dict[str, list[str]] = defaultdict(list)
    for h in run.headers:
        target = given.get(h)
        if target is None:
            problems.append(f'Choose a column for "{h}", or ignore it.')
        elif target != IGNORE and target not in labels:
            problems.append(f'"{target}" isn\'t a column you can map "{h}" to.')
        else:
            mapping[h] = target
            if target != IGNORE:
                users[target].append(h)
    for target, hs in users.items():
        if len(hs) > 1:
            problems.append(f'"{hs[0]}" and "{hs[1]}" are both mapped to {labels[target]}.')
    name_column = csvio.name_column(run.kind)
    if name_column not in users:
        problems.append(f"Map a column to {labels.get(name_column, 'Name')} to continue.")
    if "external_key" in users and run.source_id is None:
        problems.append("Choose an import source to use external keys.")
    errors: dict[str, list[str]] = {}
    if problems:
        errors["mapping"] = problems
    save = data.get("save_mapping", False)
    if not isinstance(save, bool):
        errors["save_mapping"] = ["Send true or false."]
    elif save and run.source_id is None:
        errors["save_mapping"] = ["Choose an import source to save the mapping."]
    if errors:
        raise FieldErrors(errors)

    rows = list(run.rows.all())
    for row in rows:
        row.raw = {t: row.raw.get(h, "") for h, t in mapping.items() if t != IGNORE}
    ImportRow.objects.bulk_update(rows, ["raw"], batch_size=500)
    run.mapping = mapping
    run.save(update_fields=["mapping", "updated_at"])
    if save:
        source = run.source
        source.column_mapping = {**source.column_mapping, **mapping}
        source.save(update_fields=["column_mapping", "updated_at"])
        services.audit(org, actor, "import_source.mapping_saved", source, {"name": source.name, "mapping": mapping})
    preview(run, actor)
    return run


@transaction.atomic
def rerun(org, actor, run):
    """A new run from the same file, with the same choices, for 'Rerun preview' after a refusal."""
    require_kind(org, run.kind)
    _require_file(run)
    new = ImportRun.objects.create(
        organization=org,
        kind=run.kind,
        source=run.source,
        uploaded_by=actor,
        filename=run.filename,
        file_bytes=run.file_bytes,
        headers=run.headers,
        mapping=run.mapping,
        decisions={k: v for k, v in run.decisions.items() if k != "derived"},
        purge_after=services.org_today(org) + timedelta(days=KEEP_DAYS),
    )
    rows = list(run.rows.all())
    ImportRow.objects.bulk_create(
        [
            ImportRow(
                organization=org,
                run=new,
                row_number=r.row_number,
                raw=r.raw,
                outcome=RowOutcome.CREATE,
                excluded=r.excluded,
            )
            for r in rows
        ]
    )
    if run.status != ImportStatus.UPLOADED:  # an unmapped run stays unmapped
        preview(new, actor)
    services.audit(org, actor, "import.uploaded", new, {"kind": new.kind, "rows": len(rows), "rerun_of": str(run.id)})
    return new


def _require_file(run):
    if run.purged_at is not None:
        raise Conflict(PURGED_MESSAGE + ". Upload the file again.")


def _require_open(run):
    _require_file(run)
    if run.status not in (ImportStatus.UPLOADED, ImportStatus.PREVIEWED):
        raise Conflict(f"This import is {run.status}; only a previewed import can change.")


def _require_mapped(run):
    if run.status == ImportStatus.UPLOADED:
        raise Conflict("Map the columns first.")


# ---- planning: validate, match, compare ----------------------------------------------------------------------------


class NewRef:
    """A Person, Team or Vendor the commit creates; replaced by its ID when the record's data is built."""

    def __init__(self, table, key):
        self.table, self.key = table, key


class RowPlan:
    def __init__(self, row):
        self.row = row
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.gaps: list[str] = []
        self.resource: Resource | None = None
        # Contracts and licence pools (imports/money_import.py): the contract or pool matched, and what the preview
        # worked out for the row.
        self.target: Any = None
        self.computed: dict[str, Any] = {}
        self.natural_key: Any = None  # contracts: the number; pools: (application id, SKU name), lower-cased
        self.match: str | None = None  # id | key | approved | number | natural
        self.possible: Any = None  # a Resource, or a Contract for contracts
        self.match_decision: str | None = None
        self.new_key: str | None = None
        self.data: dict[str, Any] = {}
        self.changes: dict[str, list] = {}
        self.refs: list[dict] = []  # people/teams/vendors the row needs a decision about
        self.needs_confirmation = False
        self.outcome = RowOutcome.CREATE
        self.pending: list[dict] = []

    def error(self, column, message):
        self.errors.append({"level": "error", "column": column, "message": message})

    def warn(self, column, message):
        self.warnings.append({"level": "warning", "column": column, "message": message})

    @property
    def eligible(self):
        return not self.errors and not self.row.excluded

    @property
    def matched(self):
        """The record, contract or pool this row updates (None: it creates one)."""
        return self.resource if self.resource is not None else self.target


class Plan:
    def __init__(self):
        self.rows: list[RowPlan] = []
        self.panels: dict[str, Any] = {}
        self.counts: dict[str, int] = {}
        self.person_ids: set = set()
        self.panel_entries: dict[str, dict[str, dict]] = {}


class _Context:
    def __init__(self, run):
        self.run = run
        self.org = run.organization
        self.kind = run.kind
        org = self.org
        related = ["business_owner", "technical_owner", "support_team", "it_steward", "department", self.kind]
        if self.kind == Kind.APPLICATION:
            related += ["application__vendor", "application__category"]
        self.resources = {
            r.id: r for r in Resource.objects.filter(organization=org, kind=self.kind).select_related(*related)
        }
        self.tag_names = tags.names_for(self.resources)
        self.twins: dict[str, Resource] = {}
        for r in sorted(self.resources.values(), key=lambda r: r.created_at):
            if r.archived_at is None:
                self.twins.setdefault(r.name.lower(), r)
        self.people = {p.email.lower(): p for p in Person.objects.filter(organization=org).exclude(email="")}
        self.teams = {t.name.lower(): t for t in Team.objects.filter(organization=org)}
        self.vendors = {v.name.lower(): v for v in Vendor.objects.filter(organization=org)}
        self.departments = {d.name.lower(): d for d in Department.objects.filter(organization=org)}
        self.categories = {c.name.lower(): c for c in SoftwareCategory.objects.filter(organization=org)}
        self.compliance = compliance.enabled(org)
        self.custom = customfields.for_kind(org, self.kind)
        self.columns = csvio.columns(self.kind, self.compliance, self.custom)
        users = get_user_model().objects.filter(
            is_active=True, membership__organization=org, membership__role__in=services.WRITE_ROLES
        )
        self.stewards = {u.email.lower(): u for u in users if u.email}
        self.key_to_resource: dict[str, Any] = {}
        self.key_of_resource: dict[Any, str] = {}
        if run.source_id:
            for ident in ExternalIdentifier.objects.filter(source_id=run.source_id):
                self.key_to_resource[ident.external_key] = ident.resource_id
                self.key_of_resource[ident.resource_id] = ident.external_key
        others = ExternalIdentifier.objects.filter(organization=org, resource__kind=self.kind)
        if run.source_id:
            others = others.exclude(source_id=run.source_id)
        self.keyed_elsewhere = set(others.values_list("resource_id", flat=True))
        self.other_kind: set = set()
        self.person_ids: set = set()

    def load_other_kinds(self, ids):
        valid = [i for i in ids if i]
        self.other_kind = set(
            Resource.objects.filter(organization=self.org, id__in=valid)
            .exclude(kind=self.kind)
            .values_list("id", flat=True)
        )

    def latest_review(self, resource_id):
        return (
            RecordReview.objects.filter(organization=self.org, resource_id=resource_id)
            .order_by("-confirmation_date", "-created_at")
            .first()
        )


def _uuid(text):
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _choice(field, cell):
    cls = CHOICES[field]
    lookup = {}
    for value, label in cls.choices:
        lookup[value] = value
        lookup[label.lower()] = value
        lookup[value.replace("_", " ")] = value
    return lookup.get(cell.strip().lower())


def _allowed(field):
    return ", ".join(CHOICES[field].values)


COLUMN_LABEL = {
    "built_by": "Built by",
    "license_model": "License model",
    "component_type": "Component type",
    "sso_enabled": "SSO enabled",
    "scim_provisioning": "SCIM provisioning",
    "entra_app_id": "Entra app ID",
    "service_principal_id": "Service principal ID",
    "primary_url": "Primary URL",
    "admin_url": "Admin URL",
    "runbook_url": "Runbook URL",
    "azure_resource_id": "Azure resource ID",
    "pp_environment_id": "Power Platform environment ID",
    "external_id": "External ID",
    "is_gxp": "GxP",
    "validation_doc_url": "Validation document URL",
    # contracts and licence pools
    "po_number": "PO number",
    "sku_name": "SKU name",
    "sku_id": "SKU ID",
}
YES_NO = {"true": "true", "yes": "true", "false": "false", "no": "false"}


def _column_label(field):
    return COLUMN_LABEL.get(field, field.replace("_", " ").capitalize())


def _same_person(person, email, name):
    """Whether the cells describe the person the record already has (an export re-imports as unchanged)."""
    if person is None:
        return False
    if email:
        return (person.email or "").lower() == email.lower()
    return not person.email and person.name == name


def _plan_row(ctx, row, decisions, id_rows, key_rows):
    p = RowPlan(row)
    kind, org, run = ctx.kind, ctx.org, ctx.run
    cells = {c: str(row.raw.get(c) or "").strip() for c in ctx.columns}
    id_cell, key_cell = cells["softwarden_id"], cells["external_key"]

    # -- identifiers: Softwarden ID, then the source's external key
    if id_cell and len(id_rows[id_cell.lower()]) > 1:
        others = ", ".join(str(n) for n in id_rows[id_cell.lower()] if n != row.row_number)
        p.error("softwarden_id", f"The same Softwarden ID is on row {others}.")
    if key_cell and len(key_rows[key_cell]) > 1:
        others = ", ".join(str(n) for n in key_rows[key_cell] if n != row.row_number)
        p.error("external_key", f"The same external key is on row {others}.")
    if key_cell and run.source_id is None:
        p.error("external_key", "Choose an import source to use external keys.")
    resource = None
    if id_cell:
        rid = _uuid(id_cell)
        if rid is None:
            p.error("softwarden_id", "That isn't a Softwarden ID.")
        elif rid in ctx.resources:
            resource = ctx.resources[rid]
            p.match = "id"
        elif rid in ctx.other_kind:
            p.error("softwarden_id", f"That Softwarden ID belongs to a different kind than these {kind}s.")
        else:
            p.error("softwarden_id", "No record in this organization has that Softwarden ID.")
    elif key_cell and run.source_id:
        rid = ctx.key_to_resource.get(key_cell)
        if rid is not None:
            resource = ctx.resources.get(rid)
            p.match = "key"
    if resource is not None and resource.archived_at is not None:
        p.error("softwarden_id" if p.match == "id" else "external_key", "That record is archived. Restore it first.")
        resource = None
    name = cells["name"]
    if resource is None and not id_cell and name and not p.errors:
        twin = ctx.twins.get(name.lower())
        if twin is not None:
            p.possible = twin
            choice = decisions.get("matches", {}).get(str(row.row_number))
            if choice == "create":
                p.match_decision = "create"
            elif choice and str(twin.id) == choice:
                p.match_decision = "match"
                resource, p.match = twin, "approved"
            else:
                p.pending.append(
                    {
                        "level": "decision",
                        "column": "name",
                        "message": f'A record named "{twin.name}" exists. Create a new record or match it.',
                    }
                )
    if resource is not None and key_cell and run.source_id and not any(e["column"] == "external_key" for e in p.errors):
        owner = ctx.key_to_resource.get(key_cell)
        known = ctx.key_of_resource.get(resource.id)
        if owner is not None and owner != resource.id:
            p.error("external_key", "That external key belongs to another record.")
        elif known is not None and known != key_cell:
            p.error("external_key", f"This record already has the key {known} for this source.")
        elif known is None:
            p.new_key = key_cell
    elif resource is None and key_cell and run.source_id:
        p.new_key = key_cell  # a new record is known to the source by this key from now on
    p.resource = resource

    # -- values
    candidate: dict[str, str] = {}
    fields = [f for f in VALUE_FIELDS if f in cells]
    for field in fields:
        cell = cells[field]
        if field in CHOICES and cell:
            value = _choice(field, cell)
            if value is None:
                p.error(field, f'{_column_label(field)} "{cell}" isn\'t valid. Use {_allowed(field)}.')
                continue
            cell = value
        elif field in csvio.BOOLEAN_COLUMNS and cell:
            if cell.lower() not in YES_NO:
                p.error(field, f'{_column_label(field)} "{cell}" isn\'t valid. Use true or false, or leave it empty.')
                continue
            cell = YES_NO[cell.lower()]
        elif field in GUID_COLUMNS:
            cell = cell.lower()
        if not cell:
            if field == "name":
                p.error("name", "Enter a name.")
            elif field == "lifecycle" and resource is None:
                p.error("lifecycle", "Enter a lifecycle for new records.")
            continue  # blank on an update leaves the value
        candidate[field] = cell
    if len(candidate.get("name", "")) > services.NAME_MAX:
        p.error("name", f"Use {services.NAME_MAX} characters or fewer.")
        candidate.pop("name")
    probe: dict[str, list[str]] = {}
    shared = {k: v for k, v in candidate.items() if k in RESOURCE_FIELDS - {"name", "lifecycle"}}
    services._apply(org, Resource(kind=kind), shared, probe)
    extension = Application(organization=org) if kind == Kind.APPLICATION else Component(organization=org)
    ext_data = {k: v for k, v in candidate.items() if k not in RESOURCE_FIELDS}
    services._apply_extension(org, extension, ext_data, probe)
    for column, messages in probe.items():
        p.error(column, messages[0])
        candidate.pop(column, None)

    tag_names = None
    if cells.get("tags"):
        problems: dict[str, list[str]] = {}
        tag_names = tags.clean(csvio.split_tags(cells["tags"]), problems)
        if tag_names is None:
            p.error("tags", problems["tags"][0])

    current = (
        csvio.record_values(resource, ctx.tag_names.get(resource.id, []), ctx.compliance)
        if resource is not None
        else None
    )
    for field, value in candidate.items():
        before = current[field] if current else ""
        if current is None or value != before:
            p.data[field] = value
            p.changes[field] = [before, value]
    if ctx.custom:
        _plan_custom(ctx, p, resource, cells)
    if tag_names:
        before_tags = ctx.tag_names.get(resource.id, []) if resource is not None else []
        if resource is None or {t.lower() for t in before_tags} != {t.lower() for t in tag_names}:
            p.data["tags"] = tag_names
            p.changes["tags"] = [csvio.tags_text(before_tags), csvio.tags_text(tag_names)]

    # -- owners, team, vendor, steward
    for role in ROLES:
        _plan_person(ctx, p, resource, role, cells, decisions)
    if cells["support_team"]:
        _plan_named(ctx, p, resource, "teams", "support_team", cells["support_team"], decisions)
    if cells.get("vendor"):
        _plan_named(ctx, p, resource, "vendors", "vendor", cells["vendor"], decisions)
    for field, pool, where in (
        ("department", ctx.departments, "People & Teams"),
        ("category", ctx.categories, "Categories"),
    ):
        if cells.get(field):
            _plan_existing(p, resource, field, pool, cells[field], where)
    if cells["it_steward_email"]:
        email = cells["it_steward_email"]
        steward = resource.it_steward if resource is not None else None
        if steward is not None and (steward.email or "").lower() == email.lower():
            pass
        elif email.lower() in ctx.stewards:
            user = ctx.stewards[email.lower()]
            p.data["it_steward"] = user.id
            p.changes["it_steward"] = [steward.email if steward else "", user.email]
        else:
            p.error(
                "it_steward_email",
                "The IT steward must be an active Admin or Editor in this organization, by email. Import doesn't create accounts.",
            )

    # -- the retirement rule is the record page's to acknowledge
    if (
        p.data.get("lifecycle") == Lifecycle.RETIRED
        and resource is not None
        and resource.lifecycle != Lifecycle.RETIRED
    ):
        if any(why is None for _, why in services.dependents_of(org, resource)):
            p.error(
                "lifecycle",
                "Active records depend on this one. Retire it from its own page, where you can acknowledge them.",
            )

    for field, label in GAP_LABELS.items():
        has = field in p.data or (resource is not None and getattr(resource, field + "_id") is not None)
        if not has:
            p.gaps.append(label)
    return p


def _plan_custom(ctx, p, resource, cells):
    """The cf_<name> columns: each cell typed (customfields.parse_text) and checked as the record API checks the value,
    so a row error names the column. A blank cell leaves the value; a new record gets the field's default for a
    blank cell and needs a value for a required field. Only changed values go into the row's data (merged on
    commit)."""
    given: dict[str, Any] = {}
    by_name = {cf.name: cf for cf in ctx.custom}
    for cf in ctx.custom:
        col = customfields.column(cf)
        try:
            value = customfields.parse_text(cf, cells.get(col, ""))
        except customfields.Invalid as e:
            p.error(col, f"{cf.label}: {e.message}")
            continue
        if value is not None:
            given[cf.name] = value
    current = (resource.custom_field_data or {}) if resource is not None else {}
    problems: dict[str, list[str]] = {}
    after = customfields.clean(  # a plan writes nothing: no FOR SHARE (the commit's writes take it)
        ctx.org, ctx.kind, current, {customfields.KEY: given}, problems, creating=resource is None, lock=False
    )
    for key, messages in problems.items():
        cf = by_name[key.removeprefix(customfields.PREFIX)]
        message = messages[0]
        p.error(customfields.column(cf), message if message.startswith(cf.label) else f"{cf.label}: {message}")
    if problems or after is None:
        return
    changed = {}
    for name in given:
        if after.get(name) != current.get(name):
            changed[name] = after[name]
            cf = by_name[name]
            p.changes[customfields.column(cf)] = [
                customfields.to_text(cf, current.get(name)),
                customfields.to_text(cf, after[name]),
            ]
    if changed:
        p.data[customfields.KEY] = changed


def _plan_person(ctx, p, resource, role, cells, decisions):
    email, name = cells[f"{role}_email"], cells[f"{role}_name"]
    if not email and not name:
        return
    current = getattr(resource, role) if resource is not None else None
    if _same_person(current, email, name):
        return
    shown = current.name if current else ""
    if email:
        try:
            EmailValidator()(email)
        except ValidationError:
            p.error(f"{role}_email", "Enter an email address such as name@example.org, or leave it blank.")
            return
        person = ctx.people.get(email.lower())
        if person is not None:
            ctx.person_ids.add(person.id)
            if current is not None and person.id == current.id:
                return
            if not person.active:
                p.warn(
                    f"{role}_email",
                    f"{person.name} is inactive, so the {role.replace('_', ' ')} isn't set (Inactive owner).",
                )
                p.gaps.append("Inactive owner")
                return
            p.data[role] = str(person.id)
            p.changes[role] = [shown, person.name]
            return
        key = email.lower()
    else:
        key = name.lower()
    if len(name or email) > 200:
        p.error(f"{role}_name", "Use 200 characters or fewer for a name.")
        return
    p.refs.append({"table": "people", "key": key, "email": email, "name": name, "field": role, "shown": shown})


def _plan_named(ctx, p, resource, table, field, cell, decisions):
    pool = ctx.teams if table == "teams" else ctx.vendors
    if field == "support_team":
        current = resource.support_team if resource is not None else None
    else:
        ext = resource.extension if resource is not None else None
        current = ext.vendor if ext is not None and ext.vendor_id else None
    if current is not None and current.name.lower() == cell.lower():
        return
    found = pool.get(cell.lower())
    shown = current.name if current else ""
    if found is not None:
        p.data[field] = str(found.id)
        p.changes[field] = [shown, found.name]
        return
    if len(cell) > 200:
        p.error(field, "Use 200 characters or fewer.")
        return
    p.refs.append({"table": table, "key": cell.lower(), "email": "", "name": cell, "field": field, "shown": shown})


def _plan_existing(p, resource, field, pool, cell, where):
    """A department or category named by the file: it must exist already (the import doesn't create them; they are
    short lists an Admin keeps). Blank leaves the record's value."""
    if resource is None:
        current = None
    elif field == "department":
        current = resource.department
    else:
        current = resource.application.category if resource.kind == Kind.APPLICATION else None
    if current is not None and current.name.lower() == cell.lower():
        return
    found = pool.get(cell.lower())
    if found is None:
        p.error(
            field,
            f'No {_column_label(field).lower()} is named "{cell}". Add it under {where} first, or leave it empty.',
        )
        return
    p.data[field] = str(found.id)
    p.changes[field] = [current.name if current else "", found.name]


def plan_run(run, simulate=False, actor=None):
    """What importing this run would do now. Reads only; nothing is saved. Contracts and licence pools are planned by
    imports/money_import.py; `simulate` (the preview) also tries their writes and rolls them back."""
    if run.kind in MONEY_KINDS:
        from . import money_import

        return money_import.plan_run(run, simulate, actor)
    ctx = _Context(run)
    decisions = run.decisions or {}
    rows = list(run.rows.order_by("row_number"))
    id_rows: dict[str, list[int]] = defaultdict(list)
    key_rows: dict[str, list[int]] = defaultdict(list)
    ids = []
    for r in rows:
        i, k = str(r.raw.get("softwarden_id") or "").strip(), str(r.raw.get("external_key") or "").strip()
        if i:
            id_rows[i.lower()].append(r.row_number)
            ids.append(_uuid(i))
        if k:
            key_rows[k].append(r.row_number)
    ctx.load_other_kinds(ids)
    plans = [_plan_row(ctx, r, decisions, id_rows, key_rows) for r in rows]
    return assemble(
        plans,
        decisions,
        person_ids=ctx.person_ids,
        keyed_elsewhere=ctx.keyed_elsewhere,
        latest_review=ctx.latest_review,
    )


def same_target_errors(plans, noun="record"):
    """Rows that all match one record (or contract, or pool) are errors."""
    by_target: dict = defaultdict(list)
    for p in plans:
        if p.matched is not None and not p.errors:
            by_target[p.matched.id].append(p)
    for group in by_target.values():
        if len(group) > 1:
            numbers = ", ".join(str(g.row.row_number) for g in group)
            for p in group:
                p.error("softwarden_id", f"Rows {numbers} all match the same {noun}.")


def assemble(plans, decisions, *, person_ids, keyed_elsewhere, latest_review=None, noun="record"):
    """The planned rows put together: rows matching one target are errors, confirmations needed (records only:
    `latest_review`), the panels of people, teams and vendors to decide on and of possible matches, each row's
    outcome, and the counts."""
    plan = Plan()
    same_target_errors(plans, noun)
    panel_entries: dict[str, dict[str, dict]] = {"people": {}, "teams": {}, "vendors": {}}
    matches = []
    for p in plans:
        if (
            latest_review is not None
            and p.resource is not None
            and p.eligible
            and any(f in p.data for f, _ in CONFIRMED_FIELDS)
        ):
            review = latest_review(p.resource.id)  # a confirmed field changing, on a record that was reviewed
            if review is not None:
                snap = confirmed_snapshot(p.resource)
                for f, _ in CONFIRMED_FIELDS:
                    if f in p.data:
                        snap[f] = p.data[f] or None
                p.needs_confirmation = any(review.confirmed_values.get(f) != snap[f] for f, _ in CONFIRMED_FIELDS)
        if p.eligible:
            for ref in p.refs:
                entry = panel_entries[ref["table"]].setdefault(
                    ref["key"],
                    {"key": ref["key"], "email": ref["email"], "name": ref["name"], "rows": [], "fields": []},
                )
                if not entry["name"] and ref["name"]:
                    entry["name"] = ref["name"]
                entry["rows"].append(p.row.row_number)
                if ref["field"] not in entry["fields"]:
                    entry["fields"].append(ref["field"])
                choice = decisions.get(ref["table"], {}).get(ref["key"])
                entry["decision"] = choice if choice in ("create", "gap") else None
                if choice == "create":
                    p.data[ref["field"]] = NewRef(ref["table"], ref["key"])
                    after = entry["name"] or entry["email"]
                    p.changes[ref["field"]] = [ref["shown"], f"{after} (new)"]
                elif choice != "gap":
                    p.pending.append(
                        {
                            "level": "decision",
                            "column": ref["field"],
                            "message": f'Choose what to do with "{ref["name"] or ref["email"]}".',
                        }
                    )
            if p.possible is not None:
                matches.append(
                    {
                        "row_number": p.row.row_number,
                        "name": p.row.raw.get("name", ""),
                        "candidate": {
                            "id": str(p.possible.id),
                            "name": p.possible.name,
                            "known_to_other_source": p.possible.id in keyed_elsewhere,
                        },
                        "decision": p.match_decision,
                    }
                )
    plan.rows = plans
    plan.person_ids = person_ids
    plan.panel_entries = panel_entries
    plan.panels = {
        "people": list(panel_entries["people"].values()),
        "teams": list(panel_entries["teams"].values()),
        "vendors": list(panel_entries["vendors"].values()),
        "matches": matches,
    }
    settle(plan)
    return plan


def outcome_of(p):
    if p.errors:
        return RowOutcome.EXCLUDED if p.row.excluded else RowOutcome.ERROR
    if p.row.excluded:
        return RowOutcome.EXCLUDED
    if p.matched is None:
        return RowOutcome.CREATE
    if p.data:
        return RowOutcome.UPDATE
    return RowOutcome.UNCHANGED


def settle(plan):
    """Each row's outcome, the panels' "matched" line and the counts, from the rows as planned (again after a
    contracts preview found errors by trying the writes)."""
    plans = plan.rows
    panel_entries = plan.panel_entries
    matches = plan.panels["matches"]
    for p in plans:
        p.outcome = outcome_of(p)
        if p.outcome not in (RowOutcome.CREATE, RowOutcome.UPDATE):
            p.needs_confirmation = False
    matched: dict[str, set] = {"people": set(), "teams": set(), "vendors": set()}
    # People, teams and vendors the file names that already exist (for the "matched" line of the panel).
    for p in plans:
        if p.eligible:
            if "support_team" in p.data and not isinstance(p.data["support_team"], NewRef):
                matched["teams"].add(p.data["support_team"])
            if "vendor" in p.data and not isinstance(p.data["vendor"], NewRef):
                matched["vendors"].add(p.data["vendor"])
    outcomes = Counter(p.outcome for p in plans)
    pending = sum(1 for t in panel_entries.values() for e in t.values() if e["decision"] is None) + sum(
        1 for m in matches if m["decision"] is None
    )
    plan.panels["matched"] = {
        "people": len(plan.person_ids),
        "teams": len(matched["teams"]),
        "vendors": len(matched["vendors"]),
    }
    plan.counts = {
        "total": len(plans),
        **{o.value: outcomes.get(o, 0) for o in RowOutcome},
        "needs_confirmation": sum(1 for p in plans if p.needs_confirmation),
        "new_keys": sum(1 for p in plans if p.eligible and p.new_key),
        "blocking_errors": outcomes.get(RowOutcome.ERROR, 0),
        "pending_decisions": pending,
        "people_to_create": sum(1 for e in panel_entries["people"].values() if e["decision"] == "create"),
        "teams_to_create": sum(1 for e in panel_entries["teams"].values() if e["decision"] == "create"),
        "vendors_to_create": sum(1 for e in panel_entries["vendors"].values() if e["decision"] == "create"),
    }
    return plan


def _row_errors(p):
    return [*p.errors, *p.warnings, *({"level": "gap", "message": g} for g in p.gaps), *p.pending]


def preview(run, actor=None):
    """Plan the run and save what was found (outcomes, changes, the panels, counts); the run is now previewed.
    `actor` is the person asking (a contracts preview rehearses its writes as them)."""
    plan = plan_run(run, simulate=True, actor=actor)
    money = run.kind in MONEY_KINDS
    for p in plan.rows:
        r = p.row
        r.outcome = p.outcome
        r.matched_resource = p.resource
        r.matched_updated_at = p.matched.updated_at if p.matched is not None else None
        if money:
            r.matched_contract = p.target if run.kind == "contract" else None
            r.matched_license_pool = p.target if run.kind == "license_pool" else None
            r.possible_contract = p.possible
            r.computed = p.computed
        else:
            r.possible_match = p.possible
        r.errors = _row_errors(p)
        r.changes = {k: [_plain(v) for v in pair] for k, pair in p.changes.items()}
        r.needs_confirmation = p.needs_confirmation
    fields = ["outcome", "matched_resource", "matched_updated_at", "possible_match", "errors", "changes"]
    fields += ["needs_confirmation"]
    if money:
        fields += ["matched_contract", "matched_license_pool", "possible_contract", "computed"]
    ImportRow.objects.bulk_update([p.row for p in plan.rows], fields, batch_size=500)
    decisions = {k: v for k, v in (run.decisions or {}).items() if k != "derived"}
    decisions["derived"] = {"person_ids": sorted(str(i) for i in plan.person_ids), "panels": plan.panels}
    run.decisions = decisions
    run.counts = plan.counts
    run.status = ImportStatus.PREVIEWED
    run.previewed_at = timezone.now()
    run.save(update_fields=["decisions", "counts", "status", "previewed_at", "updated_at"])
    return plan


def _plain(value):
    return value if isinstance(value, str | list | type(None)) else str(value)


# ---- Decisions and exclusions --------------------------------------------------------------------------------------


@transaction.atomic
def update_run(org, actor, run, data):
    """Record decisions and exclusions, then preview again. data: {"decisions": {...}, "exclude": [...], "include": [...]}."""
    require_kind(org, run.kind)
    run = ImportRun.objects.select_for_update().get(id=run.id)
    _require_open(run)
    _require_mapped(run)
    if not isinstance(data, dict):
        raise FieldErrors({"non_field": ["Send a JSON object."]})
    errors: dict[str, list[str]] = {}
    derived = (run.decisions or {}).get("derived", {}).get("panels", {})
    decisions = {k: dict(v) for k, v in (run.decisions or {}).items() if k != "derived"}
    given = data.get("decisions") or {}
    if not isinstance(given, dict):
        raise FieldErrors({"decisions": ["Send decisions as an object."]})
    for table in ("people", "teams", "vendors"):
        known = {e["key"] for e in derived.get(table, [])}
        for key, choice in (given.get(table) or {}).items():
            if key not in known:
                errors.setdefault("decisions", []).append(f'"{key}" isn\'t waiting for a decision.')
            elif choice is None:
                decisions.get(table, {}).pop(key, None)
            elif choice not in ("create", "gap"):
                errors.setdefault("decisions", []).append("Choose create or gap.")
            else:
                decisions.setdefault(table, {})[key] = choice
    candidates = {str(m["row_number"]): m["candidate"]["id"] for m in derived.get("matches", [])}
    for number, choice in (given.get("matches") or {}).items():
        number = str(number)
        if number not in candidates:
            errors.setdefault("decisions", []).append(f"Row {number} has no possible match to decide.")
        elif choice is None:
            decisions.get("matches", {}).pop(number, None)
        elif choice == "create":
            decisions.setdefault("matches", {})[number] = "create"
        elif choice in ("match", candidates[number]):
            decisions.setdefault("matches", {})[number] = candidates[number]
        else:
            errors.setdefault("decisions", []).append("Choose create, or match the record shown.")
    rows = {r.row_number: r for r in run.rows.all()}
    was_excluded = {n for n, r in rows.items() if r.excluded}
    for key, flag in (("exclude", True), ("include", False)):
        numbers = data.get(key) or []
        if not isinstance(numbers, list) or not all(isinstance(n, int) for n in numbers):
            errors[key] = ["Send row numbers as a list."]
            continue
        for n in numbers:
            row = rows.get(n)
            has_error = row is not None and any(e.get("level") == "error" for e in row.errors)
            if row is None:
                errors.setdefault(key, []).append(f"There is no row {n}.")
            elif flag and not has_error:
                errors.setdefault(key, []).append(f"Row {n} has no error to exclude.")
            else:
                row.excluded = flag
    if errors:
        raise FieldErrors(errors)
    ImportRow.objects.bulk_update(list(rows.values()), ["excluded"])
    before = {k: v for k, v in (run.decisions or {}).items() if k != "derived"}
    run.decisions = {**decisions, "derived": (run.decisions or {}).get("derived", {})}
    preview(run, actor)
    # One event per edit, with counts only; row contents and the decided names never enter the audit log.
    now_excluded = {n for n, r in rows.items() if r.excluded}
    changed = sum(
        1
        for table in set(before) | set(decisions)
        for key in set(before.get(table) or {}) | set(decisions.get(table) or {})
        if (before.get(table) or {}).get(key) != (decisions.get(table) or {}).get(key)
    )
    counts = {
        "run": str(run.id),
        "decisions_changed": changed,
        "rows_excluded": len(now_excluded - was_excluded),
        "rows_included": len(was_excluded - now_excluded),
    }
    services.audit(org, actor, "import.staging_updated", run, counts)
    return run


# ---- Commit ----------------------------------------------------------------------------------------------------


def commit_run(org, actor, run):
    """All or nothing. Refused (the run marked refused, nothing saved) when a row has an error that isn't excluded,
    a choice is missing, or a matched record changed since the preview."""
    require_kind(org, run.kind)
    _require_open(run)
    refused = None
    with transaction.atomic():
        run = ImportRun.objects.select_for_update().get(id=run.id)
        _require_open(run)
        _require_mapped(run)
        try:
            with transaction.atomic():  # a savepoint: a refusal undoes the writes but keeps the run locked
                stored = {r.row_number: r for r in run.rows.all()}
                # The matched records (or contracts, or pools) are locked before planning, in id order, so an edit in
                # the gap between the check and the write waits, and the plan sees it.
                _lock_matched(Resource, "matched_resource_id", stored)
                _lock_matched(Contract, "matched_contract_id", stored)
                _lock_matched(LicensePool, "matched_license_pool_id", stored)
                plan = plan_run(run)
                _check_ready(plan, stored)
                done = _write(org, actor, run, plan)
        except Refused as e:
            # Marked refused while the run is still locked: a concurrent commit waiting on the lock then finds
            # it refused and is turned away by _require_open, instead of refusing and auditing it a second time.
            ImportRun.objects.filter(id=run.id).update(
                status=ImportStatus.REFUSED, refused_reason=e.reason[:500], updated_at=timezone.now()
            )
            services.audit(org, actor, "import.refused", run, {"reason": e.reason[:200]})
            refused = e
    if refused is not None:
        run.refresh_from_db()
        raise Conflict(refused.reason, extra={"type": REFUSED, "run": str(run.id), **refused.extra}) from None
    return done


def _lock_matched(model, field, stored):
    wanted = sorted({getattr(r, field) for r in stored.values() if getattr(r, field) and not r.excluded})
    if wanted:
        list(model.objects.select_for_update().filter(id__in=wanted).order_by("id").values_list("id", flat=True))


def _check_ready(plan, stored):
    blocking = [p.row.row_number for p in plan.rows if p.outcome == RowOutcome.ERROR]
    if blocking:
        raise Refused(
            f"{len(blocking)} row{'s have' if len(blocking) != 1 else ' has'} errors that aren't excluded. Nothing was imported.",
            {"error_rows": blocking[:50]},
        )
    waiting = plan.counts["pending_decisions"]
    if waiting:
        raise Refused(f"{waiting} choice{'s are' if waiting != 1 else ' is'} still needed. Nothing was imported.")
    changed = []
    for p in plan.rows:
        if p.row.excluded:
            continue
        before = stored[p.row.row_number]
        target = p.matched
        now_id = target.id if target is not None else None
        now_at = target.updated_at if target is not None else None
        if (now_id, now_at) != (before.matched_object_id, before.matched_updated_at):
            changed.append({"row": p.row.row_number, "name": target.name if target else _row_name(p.row)})
    if changed:
        names = ", ".join(c["name"] for c in changed[:3]) + (
            f" and {len(changed) - 3} more" if len(changed) > 3 else ""
        )
        raise Refused(
            f"{len(changed)} matched record{'s' if len(changed) != 1 else ''} changed after this preview ({names}). "
            "Nothing was imported. Rerun the preview.",
            {"changed": changed[:50]},
        )


def _row_name(row):
    return str((row.raw or {}).get(csvio.name_column(row.run.kind), ""))


def _resolve(data, made):
    return {k: (made[(v.table, v.key)] if isinstance(v, NewRef) else v) for k, v in data.items()}


def make_refs(org, actor, plan, run=None):
    """The People, teams and vendors the uploader chose to create: ({(table, key): id}, {table: how many}). Their
    events name `run`."""
    note = {"import_run": str(run.id)} if run is not None else None
    made: dict = {}
    created_counts = {"people": 0, "teams": 0, "vendors": 0}
    for entry in plan.panels["people"]:
        if entry["decision"] == "create":
            person = services.create_person(
                org, actor, {"name": entry["name"] or entry["email"], "email": entry["email"]}, note
            )
            made[("people", entry["key"])] = str(person.id)
            plan.person_ids.add(person.id)
            created_counts["people"] += 1
    for table, model in (("teams", Team), ("vendors", Vendor)):
        for entry in plan.panels[table]:
            if entry["decision"] == "create":
                # By name only, so a vendor's required custom fields aren't enforced here (no column holds them).
                obj = admin_services.create_named(model, org, actor, {"name": entry["name"]}, required=False, note=note)
                made[(table, entry["key"])] = str(obj.id)
                created_counts[table] += 1
    return made, created_counts


def _write(org, actor, run, plan):
    if run.kind in MONEY_KINDS:
        from . import money_import

        return money_import.write(org, actor, run, plan)
    # The whole batch against the records limit before anything is written: an import that would pass it is refused
    # whole (409 /problems/limit-reached), and the lock taken here holds until the commit ends.
    adding = sum(1 for p in plan.rows if not p.row.excluded and p.resource is None)
    limits.check_room(org, limits.RECORDS, adding)
    made, created_counts = make_refs(org, actor, plan, run)
    uploader = run.uploaded_by
    uploader_ok = (
        uploader is not None
        and uploader.is_active
        and Membership.objects.filter(user=uploader, organization=org, role__in=services.WRITE_ROLES).exists()
    )
    totals = {"created": 0, "updated": 0, "unchanged": 0, "excluded": 0, "keys_stored": 0}
    results = []
    for p in plan.rows:
        n = p.row.row_number
        if p.row.excluded:
            totals["excluded"] += 1
            continue
        try:
            data = _resolve(p.data, made)
            if p.resource is None:
                if "it_steward" not in data and uploader_ok:
                    data["it_steward"] = uploader.id
                # The record belongs to this run and its creation event says so (audit events are append-only, so
                # this is set as it is created, not added afterwards).
                resource = services.create_resource(org, actor, {"kind": run.kind, **data}, import_run=run)
                totals["created"] += 1
            elif p.data:
                resource = services.update_resource(
                    org, actor, p.resource, data, audit_extra={"import_run": str(run.id)}
                )
                totals["updated"] += 1
            else:
                resource = p.resource
                totals["unchanged"] += 1
            if p.new_key:
                ExternalIdentifier.objects.create(
                    organization=org, source_id=run.source_id, external_key=p.new_key, resource=resource
                )
                totals["keys_stored"] += 1
        except FieldErrors as e:
            detail = "; ".join(m for msgs in e.errors.values() for m in msgs)
            raise Conflict(f"Row {n} couldn't be saved: {detail} Nothing was imported.") from None
        except Conflict as e:
            raise Conflict(f"Row {n} couldn't be saved: {e.detail} Nothing was imported.") from None
        p.row.result_resource = resource
        results.append(p.row)
    ImportRow.objects.bulk_update(results, ["result_resource"], batch_size=500)
    return finish_commit(org, actor, run, plan, totals, created_counts)


def finish_commit(org, actor, run, plan, totals, created_counts):
    """The run committed: its counts, the People it names, its status, and the import.committed event."""
    run.counts = {
        **run.counts,
        **totals,
        "people_created": created_counts["people"],
        "teams_created": created_counts["teams"],
        "vendors_created": created_counts["vendors"],
        "pending_decisions": 0,
        "blocking_errors": 0,
    }
    derived = dict((run.decisions or {}).get("derived", {}))
    derived["person_ids"] = sorted({*derived.get("person_ids", []), *(str(i) for i in plan.person_ids)})
    run.decisions = {**(run.decisions or {}), "derived": derived}
    run.status = ImportStatus.COMMITTED
    run.committed_at = timezone.now()
    run.refused_reason = ""
    run.save(update_fields=["counts", "decisions", "status", "committed_at", "refused_reason", "updated_at"])
    services.audit(
        org,
        actor,
        "import.committed",
        run,
        {
            "run": str(run.id),
            "kind": run.kind,
            "source": run.source.name if run.source else None,
            **totals,
            "people_created": created_counts["people"],
            "teams_created": created_counts["teams"],
            "vendors_created": created_counts["vendors"],
        },
    )
    return run


@transaction.atomic
def cancel_run(org, actor, run):
    run = ImportRun.objects.select_for_update().get(id=run.id)
    if run.status not in (ImportStatus.UPLOADED, ImportStatus.PREVIEWED):
        raise Conflict(f"This import is {run.status} and can't be canceled.")
    run.status = ImportStatus.CANCELLED
    run.save(update_fields=["status", "updated_at"])
    services.audit(org, actor, "import.cancelled", run, {"run": str(run.id)})
    return run


# ---- Results and the error report ----------------------------------------------------------------------------------


def error_report(run):
    """The rows that have errors or were excluded, as CSV bytes: the template's columns as uploaded, in front of
    which sit the row number, the outcome and the errors. Uploading it back ignores those three columns."""
    if run.purged_at is not None:
        raise Gone(PURGED_MESSAGE)
    columns = [*REPORT_COLUMNS, *run.headers]
    original = None
    if run.mapping:  # mapped rows hold template columns; the report shows the file's own headings and cells
        try:
            _, file_rows = csvio.read_csv(bytes(run.file_bytes))
        except csvio.CsvRejected as e:  # the stored file passed on upload; a limit tightened since is a 4xx, not a 500
            raise FileRejected(str(e)) from None
        original = [{h: v for h, v in r.items() if h in run.headers} for r in file_rows]
    out = []
    for row in run.rows.filter(outcome__in=[RowOutcome.ERROR, RowOutcome.EXCLUDED]).order_by("row_number"):
        problems = "; ".join(
            f"{e['column']}: {e['message']}" if e.get("column") else e["message"]
            for e in row.errors
            if e.get("level") == "error"
        )
        out.append(
            {
                "import_row": row.row_number,
                "import_outcome": row.outcome,
                "import_errors": problems or ("Excluded" if row.excluded else ""),
                **(original[row.row_number - 2] if original is not None else row.raw),
            }
        )
    return csvio.write_csv(columns, out)


def template_file(kind, org=None):
    """The headings-only template; with the compliance columns when the organization's profile is on (records), and
    the custom field columns of the kind."""
    return csvio.write_csv(csvio.columns_for(org, kind) if org is not None else csvio.TEMPLATE[kind], [])


# ---- Purge ---------------------------------------------------------------------------------------------------------


def purge_run(run, reason):
    """Delete the file, the raw rows, errors and changes (they quote cells), and the choices; counts stay."""
    ImportRow.objects.filter(run=run).update(raw={}, errors=[], changes={}, computed={})
    ImportRun.objects.filter(id=run.id).update(
        file_bytes=None, decisions={}, purged_at=timezone.now(), updated_at=timezone.now()
    )
    services.audit(run.organization, None, "import.purged", run, {"reason": reason})


def purge_expired(today=None):
    """The daily job: every unpurged run whose purge date has come. Returns how many were purged."""
    from core.models import Organization

    count = 0
    for org in Organization.objects.all():
        try:
            day = today or services.org_today(org)
            for run in ImportRun.objects.filter(organization=org, purged_at__isnull=True, purge_after__lte=day):
                with transaction.atomic():
                    purge_run(run, "expired")
                count += 1
        except Exception:  # one organization's failure doesn't stop the others; logged with its id only
            log.exception("import purge failed organization=%s", org.id)
    return count


def purge_for_person(org, person):
    """ANONYMIZE_HOOKS: runs whose file mentions this Person are purged now. The Person has already lost their
    email and name, so runs are found by the Person IDs the previews recorded."""
    runs = ImportRun.objects.filter(
        organization=org, purged_at__isnull=True, decisions__derived__person_ids__contains=[str(person.id)]
    )
    n = 0
    for run in runs:
        purge_run(run, "person anonymized")
        n += 1
    return {"import_runs": n}


if purge_for_person not in admin_services.ANONYMIZE_HOOKS:
    admin_services.ANONYMIZE_HOOKS.append(purge_for_person)


def scheduled_purge(now):
    """The `imports.purge` schedule (imports/apps.py): runs whose purge date has come, in each organization's own
    calendar."""
    return {"imports_purged": purge_expired()}
