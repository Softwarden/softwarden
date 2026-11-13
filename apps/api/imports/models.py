"""CSV import. Moved from the catalog app in the split; tables and constraint
names keep their `catalog_*` names (imports/migrations/0001_initial.py)."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from catalog.models import IMPORT_KINDS, Resource
from core.models import CATALOG_ORG_KEY, Organization, OrgScoped
from money.models import Contract, LicensePool

# Contracts and licence pools are import kinds too (imports/money_import.py). They aren't record kinds
# (catalog.models.Kind), so the import's own list names them.
CONTRACT, LICENSE_POOL = "contract", "license_pool"
MONEY_KINDS = (CONTRACT, LICENSE_POOL)
KIND_CHOICES = [(str(k), k.label) for k in IMPORT_KINDS] + [(CONTRACT, "Contract"), (LICENSE_POOL, "License pool")]
KINDS = [k for k, _label in KIND_CHOICES]


class ImportSource(OrgScoped):
    """Where a CSV comes from. Its external keys map to exactly one record each. `column_mapping`
    is {header: field, "external_key" or "ignore"} saved from a run and pre-filled on the next one."""

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    column_mapping = models.JSONField(default=dict, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_importsource"
        ordering = ["name"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint("organization", Lower("name"), name="catalog_importsource_unique_name"),
        ]


class ExternalIdentifier(OrgScoped):
    """A source's key for one record. One key per record per source, one record per key per source. Migration
    catalog 0018 adds composite keys so the source and the record share the row's organization.

    The key names exactly one of a record, a contract or a licence pool (imports 0003, with composite
    keys). A record's key protects the record (deleting it is refused while the key exists); a contract's or a
    pool's key goes with it when it is deleted (their deletes have their own rules, money/contracts.py)."""

    source = models.ForeignKey(ImportSource, on_delete=models.PROTECT, related_name="identifiers")
    external_key = models.CharField(max_length=200)
    resource = models.ForeignKey(
        Resource, null=True, blank=True, on_delete=models.PROTECT, related_name="external_identifiers"
    )
    contract = models.ForeignKey(
        Contract, null=True, blank=True, on_delete=models.CASCADE, related_name="external_identifiers"
    )
    license_pool = models.ForeignKey(
        LicensePool, null=True, blank=True, on_delete=models.CASCADE, related_name="external_identifiers"
    )

    class Meta(OrgScoped.Meta):
        db_table = "catalog_externalidentifier"
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["source", "external_key"], name="catalog_externalid_unique_key"),
            models.UniqueConstraint(fields=["source", "resource"], name="catalog_externalid_unique_resource"),
            models.UniqueConstraint(fields=["source", "contract"], name="catalog_externalid_unique_contract"),
            models.UniqueConstraint(fields=["source", "license_pool"], name="catalog_externalid_unique_pool"),
            models.CheckConstraint(
                condition=(Q(resource__isnull=False) & Q(contract__isnull=True) & Q(license_pool__isnull=True))
                | (Q(resource__isnull=True) & Q(contract__isnull=False) & Q(license_pool__isnull=True))
                | (Q(resource__isnull=True) & Q(contract__isnull=True) & Q(license_pool__isnull=False)),
                name="catalog_externalid_one_target",
            ),
        ]


class ImportStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    PREVIEWED = "previewed", "Previewed"
    COMMITTED = "committed", "Committed"
    REFUSED = "refused", "Refused"
    CANCELLED = "cancelled", "Canceled"


class ImportRun(OrgScoped):
    """One CSV upload of one kind. The file is kept in the database until the purge: `file_bytes`,
    the rows' `raw` and error details go after 30 days or when a Person in it is anonymized; counts stay. `decisions`
    holds the uploader's choices from the preview: {"people": {email or name: "create" | "gap"}, "teams": {...},
    "vendors": {...}, "matches": {row number: "create" | resource id}}. `mapping` is the column mapping used."""

    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    source = models.ForeignKey(ImportSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    filename = models.CharField(max_length=255)
    file_bytes = models.BinaryField(null=True, blank=True)
    headers = models.JSONField(default=list)
    mapping = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=ImportStatus.choices, default=ImportStatus.UPLOADED)
    decisions = models.JSONField(default=dict, blank=True)
    counts = models.JSONField(default=dict, blank=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    refused_reason = models.CharField(max_length=500, blank=True)
    purge_after = models.DateField()
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_importrun"
        ordering = ["-created_at"]
        constraints = [CATALOG_ORG_KEY]


class RowOutcome(models.TextChoices):
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    UNCHANGED = "unchanged", "Unchanged"
    ERROR = "error", "Error"
    EXCLUDED = "excluded", "Excluded"


class ImportRow(models.Model):
    """One CSV row of a run. `matched_updated_at` is the record's `updated_at` at preview, so a commit is refused if it
    changed since. `changes` is {field: [before, after]}; `possible_match` names an exact-name twin that
    needs Create new or Match. Raw values and errors are purged with the file. Migration imports 0002 gives it
    a UUID key, its own organization and composite foreign keys so the run and every resource it names are in that
    organization.

    Contract and licence pool rows (imports 0003) use the matched/possible/result columns of their own kind,
    with the same composite keys; `computed` holds what the preview worked out for the row (a contract's annualised
    cost, cancellation deadline and allocation; a pool's utilisation), purged with the rest."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw = models.JSONField(default=dict)
    outcome = models.CharField(max_length=10, choices=RowOutcome.choices)
    matched_resource = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    matched_updated_at = models.DateTimeField(null=True, blank=True)
    possible_match = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    errors = models.JSONField(default=list)
    changes = models.JSONField(default=dict)
    needs_confirmation = models.BooleanField(default=False)
    excluded = models.BooleanField(default=False)
    result_resource = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    matched_contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    possible_contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    result_contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    matched_license_pool = models.ForeignKey(
        LicensePool, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    result_license_pool = models.ForeignKey(
        LicensePool, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    computed = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "catalog_importrow"
        ordering = ["run", "row_number"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["run", "row_number"], name="catalog_importrow_unique_number"),
        ]

    @property
    def matched_object(self):
        """The record, contract or licence pool the row matched at preview, whichever its kind uses."""
        return self.matched_resource or self.matched_contract or self.matched_license_pool

    @property
    def matched_object_id(self):
        return self.matched_resource_id or self.matched_contract_id or self.matched_license_pool_id

    @property
    def possible_object(self):
        return self.possible_match or self.possible_contract

    @property
    def result_object(self):
        return self.result_resource or self.result_contract or self.result_license_pool
