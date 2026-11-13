"""End-of-life tracking via endoflife.date. Two kinds of table:

- The **cache** (EolProduct, EolRelease, EolSyncState): endoflife.date's public data, installation-wide and not any
  organization's (NOT_TENANT in tests/test_tenancy.py). The `eol.refresh` job (eol/cache.py) fills it daily from the v1
  API, or `manage.py eol_import` from a downloaded snapshot on an air-gapped install. Nothing in a request reads the
  network: every screen reads these rows.
- **TrackedVersion** (tenant): which endoflife.date product an application or component is, and which release cycle
  it runs, for the record as a whole or for one of an application's instances (one application often runs several
  versions across its deployments). Written by eol/services.py, audited.

The status (supported, security only, end of life, unknown) is never stored: eol/status.py derives it from the cached
release and the organization's today, so it moves on its own as dates pass."""

from django.db import models

from catalog.models import ApplicationInstance, Resource
from core.models import OrgScoped

PRODUCT_MAX = 100
CYCLE_MAX = 100
VERSION_MAX = 50


class EolProduct(models.Model):
    """One endoflife.date product (`/api/v1/products`: name, label, aliases, category, tags). Its releases are fetched
    only while some organization tracks it (`releases_fetched_at` set once they are). `listed` turns false when the
    product list no longer carries it. `releases_etag` is the ETag of the product document the releases came from, sent
    back as If-None-Match so an unchanged product costs a 304."""

    name = models.CharField(primary_key=True, max_length=PRODUCT_MAX)
    label = models.CharField(max_length=200, blank=True)
    category = models.CharField(max_length=60, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    listed = models.BooleanField(default=True)
    link = models.URLField(max_length=500, blank=True)  # its page on endoflife.date
    release_policy = models.URLField(max_length=500, blank=True)
    releases_etag = models.CharField(max_length=200, blank=True)
    releases_fetched_at = models.DateTimeField(null=True, blank=True)  # the releases were last replaced (200, import)
    releases_checked_at = models.DateTimeField(null=True, blank=True)  # last 200 or 304
    source_modified_at = models.DateTimeField(null=True, blank=True)  # the API's `last_modified` for the product
    error = models.CharField(max_length=300, blank=True)  # the last fetch's failure, safe to show; "" after a success
    # The name endoflife.date now uses when the product's address redirected there (renamed); "" otherwise. The
    # releases stay under `name`, the one organizations track, so the screen can offer to re-map (eol 0002).
    renamed_to = models.CharField(max_length=PRODUCT_MAX, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class EolRelease(models.Model):
    """One release cycle of a product (v1 `ProductRelease`). The v1 API splits each of v0's union-typed fields into a
    boolean and a date: `eol` into isEol / eolFrom, `support` into isEoas / eoasFrom (end of active support),
    `extendedSupport` into isEoes / eoesFrom, `lts` into isLts / ltsFrom. `has_eoas` and `has_eoes` record whether the
    product has those phases at all (the fields are absent when it doesn't)."""

    product = models.ForeignKey(EolProduct, on_delete=models.CASCADE, related_name="releases")
    name = models.CharField(max_length=CYCLE_MAX)  # the cycle, e.g. "16" or "22.04"
    label = models.CharField(max_length=200, blank=True)
    codename = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)  # the API's order: newest first
    release_date = models.DateField(null=True, blank=True)
    is_lts = models.BooleanField(default=False)
    lts_from = models.DateField(null=True, blank=True)
    has_eoas = models.BooleanField(default=False)
    is_eoas = models.BooleanField(null=True, blank=True)
    eoas_from = models.DateField(null=True, blank=True)
    is_eol = models.BooleanField(default=False)
    eol_from = models.DateField(null=True, blank=True)
    has_eoes = models.BooleanField(default=False)
    is_eoes = models.BooleanField(null=True, blank=True)  # null: not eligible for extended support
    eoes_from = models.DateField(null=True, blank=True)
    is_maintained = models.BooleanField(default=False)
    latest = models.CharField(max_length=100, blank=True)
    latest_date = models.DateField(null=True, blank=True)
    link = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ["product", "position"]
        constraints = [models.UniqueConstraint(fields=["product", "name"], name="eol_release_unique")]

    def __str__(self) -> str:
        return f"{self.product_id} {self.name}"


class EolSyncState(models.Model):
    """The cache's own bookkeeping: one row (pk 1). The product list's ETag (sent as If-None-Match), when the list and
    the last run happened, and the last run's failure. `source` is where the data last came from: the API (`api`) or an
    offline snapshot (`import`)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    list_etag = models.CharField(max_length=200, blank=True)
    list_fetched_at = models.DateTimeField(null=True, blank=True)
    list_checked_at = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    source = models.CharField(max_length=10, blank=True)  # api, import
    imported_at = models.DateTimeField(null=True, blank=True)


class CycleSource(models.TextChoices):
    EXPLICIT = "explicit", "Set explicitly"
    DERIVED = "derived", "Derived from the version"


class TrackedVersion(OrgScoped):
    """An application's or component's endoflife.date product and release cycle (eol/services.py). One row for the
    record as a whole (`instance` null) and, for an application, optionally one per instance that runs a different
    version. Instance rows carry their record's product (kept equal by the services).

    `cycle` is the release cycle the status is read from: set explicitly (`cycle_explicit`), or derived from the
    version: this row's `version`, else (the record row of an application) the application's own `version` field. A
    derived cycle is the longest release name of the product that the version equals or starts with followed by a dot,
    dash, plus or space (16.4 -> 16, 22.04.3 -> 22.04). It is a derived column, re-derived when the version or the
    cached releases change (eol/cache.rederive, like the stored cancellation deadline), not an audited edit.

    eol 0001 adds the composite foreign keys: (organization, resource) to the record and (organization, resource,
    instance) to the application's own instance."""

    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="eol_tracking")
    instance = models.ForeignKey(
        ApplicationInstance, null=True, blank=True, on_delete=models.PROTECT, related_name="eol_tracking"
    )
    product = models.CharField(max_length=PRODUCT_MAX)
    version = models.CharField(max_length=VERSION_MAX, blank=True)
    cycle = models.CharField(max_length=CYCLE_MAX, blank=True)
    cycle_explicit = models.BooleanField(default=False)

    class Meta(OrgScoped.Meta):
        ordering = ["created_at", "id"]
        verbose_name = "tracked version"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                fields=["organization", "resource"],
                condition=models.Q(instance__isnull=True),
                name="eol_trackedversion_record_unique",
            ),
            models.UniqueConstraint(
                fields=["organization", "instance"],
                condition=models.Q(instance__isnull=False),
                name="eol_trackedversion_instance_unique",
            ),
        ]
        indexes = [models.Index(fields=["product", "cycle"], name="eol_trackedversion_product")]

    @property
    def cycle_source(self) -> str:
        if not self.cycle:
            return ""
        return CycleSource.EXPLICIT if self.cycle_explicit else CycleSource.DERIVED
