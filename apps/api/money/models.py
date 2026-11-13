"""Renewals, contracts and licence pools.

The renewal models moved from the catalog app in the split; their tables and constraint names keep their
`catalog_*` names (money/migrations/0001_initial.py). Contract, ContractItem and LicensePool came later and
use this app's prefix (`money_contract`, ...). Writes go through money/renewals.py and money/contracts.py."""

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from catalog.models import Person, Resource, Vendor
from core.models import CATALOG_ORG_KEY, Organization, OrgScoped


class AutoRenew(models.TextChoices):
    YES = "yes", "Yes"
    NO = "no", "No"
    UNKNOWN = "unknown", "Unknown"


class PeriodUnit(models.TextChoices):
    DAYS = "days", "Days"
    MONTHS = "months", "Months"
    YEARS = "years", "Years"


class Decision(models.TextChoices):
    RENEW = "renew", "Renew"
    CANCEL = "cancel", "Cancel"
    RENEGOTIATE = "renegotiate", "Renegotiate"


class RenewalObligation(OrgScoped):
    """Something that renews, covering one or more applications. The long-term anchor: contracts,
    subscriptions and entitlements will attach to an obligation and its cycles later, so nothing here is specific to
    one kind of agreement. Migration catalog 0015 adds composite FKs to the vendor and the responsible Person; the
    follow-up assignee must be an Admin or Editor, which services.py checks."""

    name = models.CharField(max_length=200)
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    responsible = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    followup = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    auto_renew = models.CharField(max_length=10, choices=AutoRenew.choices, default=AutoRenew.UNKNOWN)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        db_table = "catalog_renewalobligation"
        ordering = ["name"]
        verbose_name = "renewal"
        constraints = [CATALOG_ORG_KEY]

    @property
    def archived(self):
        return self.archived_at is not None


class RenewalCycle(OrgScoped):
    """One term of an obligation. Each cycle keeps its own dates, notice period, covered applications,
    decision and completion; the next one is established explicitly and nothing completes by time passing. The
    action deadline is derived (renewal_rules.py), never stored. Only one cycle per obligation is open."""

    obligation = models.ForeignKey(RenewalObligation, on_delete=models.CASCADE, related_name="cycles")
    sequence = models.PositiveIntegerField()
    renewal_date = models.DateField(null=True, blank=True)
    notice_value = models.PositiveIntegerField(null=True, blank=True)
    notice_unit = models.CharField(max_length=10, choices=PeriodUnit.choices, blank=True)
    explicit_deadline = models.DateField(null=True, blank=True)
    term_value = models.PositiveIntegerField(null=True, blank=True)
    term_unit = models.CharField(max_length=10, choices=PeriodUnit.choices, blank=True)
    decision = models.CharField(max_length=20, choices=Decision.choices, blank=True)
    decision_notes = models.TextField(blank=True)
    decided_on = models.DateField(null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)
    completion_evidence = models.TextField(blank=True)
    # What a decision on a contract's term changed,
    # as snapshots of the money and dates before and after, so later edits to either contract don't rewrite what was
    # decided (money 0005). Optional: written by the contract renew action; a renewal without a contract never has
    # them. Currencies are never converted.
    old_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    old_currency = models.CharField(max_length=3, blank=True)
    old_billing_frequency = models.CharField(max_length=20, blank=True)
    old_annualized_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    old_start_date = models.DateField(null=True, blank=True)
    old_end_date = models.DateField(null=True, blank=True)
    new_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    new_currency = models.CharField(max_length=3, blank=True)
    new_billing_frequency = models.CharField(max_length=20, blank=True)
    new_annualized_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    new_start_date = models.DateField(null=True, blank=True)
    new_end_date = models.DateField(null=True, blank=True)
    # The organization-local date the deadline's inputs were last set. Reminder thresholds already passed on that
    # day don't fire (only the next unpassed threshold fires for a late-created cycle).
    deadline_set_on = models.DateField(null=True, blank=True)
    applications = models.ManyToManyField(Resource, through="RenewalCycleApplication", related_name="+")

    class Meta(OrgScoped.Meta):
        db_table = "catalog_renewalcycle"
        ordering = ["obligation", "-sequence"]
        verbose_name = "renewal cycle"
        constraints = [
            CATALOG_ORG_KEY,
            # What a three-column foreign key names to say "this renewal's own cycle" (tickets 0002, money 0008).
            models.UniqueConstraint(
                fields=["organization", "obligation", "id"], name="catalog_renewalcycle_org_obligation_id"
            ),
            models.UniqueConstraint(fields=["obligation", "sequence"], name="catalog_renewalcycle_unique_sequence"),
            models.UniqueConstraint(
                fields=["obligation"], condition=Q(completed_on__isnull=True), name="catalog_renewalcycle_one_open"
            ),
            models.CheckConstraint(
                condition=(Q(notice_value__isnull=True) & Q(notice_unit=""))
                | (Q(notice_value__isnull=False) & ~Q(notice_unit="")),
                name="catalog_renewalcycle_notice_pair",
            ),
            models.CheckConstraint(
                condition=(Q(term_value__isnull=True) & Q(term_unit=""))
                | (Q(term_value__isnull=False) & ~Q(term_unit="")),
                name="catalog_renewalcycle_term_pair",
            ),
            models.CheckConstraint(
                condition=Q(completed_on__isnull=True) | ~Q(decision=""), name="catalog_renewalcycle_completed_decided"
            ),
        ]


class RenewalCycleApplication(models.Model):
    """An application a cycle covers. Migration catalog 0015 adds composite FKs so the cycle and the application are
    in the row's organization; migration money 0002 gives the row a UUID key and its (organization, id) key.
    PROTECT: a covered application can't be hard-deleted (it is archived instead)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    cycle = models.ForeignKey(RenewalCycle, on_delete=models.CASCADE, related_name="coverage")
    application = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="renewal_coverage")

    class Meta:
        db_table = "catalog_renewalcycleapplication"
        verbose_name = "renewal cycle"
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["cycle", "application"], name="catalog_renewalcycleapp_unique"),
        ]
        indexes = [models.Index(fields=["organization", "application"], name="catalog_renewalcycleapp_app")]


# ---- Contracts ----------------------------------------------------------------------


class BillingFrequency(models.TextChoices):
    """How often `amount` is billed; the annualised cost is derived from it (money/contracts.annualize). A multi-year amount is the whole term's, spread over the term's months (from the dates)."""

    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    SEMIANNUAL = "semiannual", "Every six months"
    ANNUAL = "annual", "Annual"
    MULTI_YEAR = "multi_year", "Multi-year"
    ONE_TIME = "one_time", "One-time"
    USAGE_BASED = "usage_based", "Usage-based"


class ContractStatus(models.TextChoices):
    """A contract term's status. `signed` is a term that hasn't started (a future commitment, not current spend)."""

    SIGNED = "signed", "Signed (not started)"
    ACTIVE = "active", "Active"
    PENDING_RENEWAL = "pending_renewal", "Pending renewal"
    RENEWED = "renewed", "Renewed"
    CANCELLED = "cancelled", "Canceled"
    EXPIRED = "expired", "Expired"


# Contracts that cost money now, and those with obligations ahead (live ones plus signed terms not yet started).
LIVE_STATUSES = (ContractStatus.ACTIVE, ContractStatus.PENDING_RENEWAL)
OBLIGATION_STATUSES = (*LIVE_STATUSES, ContractStatus.SIGNED)


class Contract(OrgScoped):
    """An agreement with a vendor for one term. `annualized_cost` and `cancellation_deadline`
    are derived and stored by money/contracts.py on every write, never entered. The deadline is the renewal action
    deadline's computation (money/renewal_rules.action_deadline, with the end date as the renewal date), and a contract
    that points at a RenewalObligation writes its dates into that obligation's open cycle, so the two never disagree
    (money/contracts.py, "One source of truth"). `predecessor` is the term this one renews (set by the renew action,
    never typed in). Composite foreign keys (money 0003) keep every reference in the organization."""

    name = models.CharField(max_length=200)
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="contracts")
    contract_number = models.CharField(max_length=100, blank=True)
    po_number = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    billing_frequency = models.CharField(
        max_length=20, choices=BillingFrequency.choices, default=BillingFrequency.ANNUAL
    )
    annualized_cost = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, editable=False)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    auto_renew = models.CharField(max_length=10, choices=AutoRenew.choices, default=AutoRenew.UNKNOWN)
    notice_value = models.PositiveIntegerField(null=True, blank=True)
    notice_unit = models.CharField(max_length=10, choices=PeriodUnit.choices, blank=True)
    cancellation_deadline = models.DateField(null=True, blank=True, editable=False)
    renewal_owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=20, choices=ContractStatus.choices, default=ContractStatus.ACTIVE)
    cost_center = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    predecessor = models.OneToOneField(
        "self", null=True, blank=True, editable=False, on_delete=models.PROTECT, related_name="successor"
    )
    renewal_obligation = models.ForeignKey(
        RenewalObligation, null=True, blank=True, on_delete=models.PROTECT, related_name="contracts"
    )
    custom_field_data = models.JSONField(default=dict, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        verbose_name = "contract"
        constraints = OrgScoped.Meta.constraints + [
            models.CheckConstraint(
                condition=(Q(notice_value__isnull=True) & Q(notice_unit=""))
                | (Q(notice_value__isnull=False) & ~Q(notice_unit="")),
                name="money_contract_notice_pair",
            ),
            models.CheckConstraint(
                condition=Q(start_date__isnull=True) | Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="money_contract_dates_ordered",
            ),
            models.CheckConstraint(condition=Q(amount__isnull=True) | Q(amount__gte=0), name="money_contract_amount"),
            models.CheckConstraint(condition=Q(currency__regex=r"^[A-Z]{3}$"), name="money_contract_currency"),
        ]
        indexes = [
            models.Index(fields=["organization", "end_date"], name="money_contract_end"),
            models.Index(fields=["organization", "cancellation_deadline"], name="money_contract_deadline"),
            models.Index(fields=["organization", "renewal_obligation"], name="money_contract_obligation"),
            GinIndex(fields=["custom_field_data"], name="money_contract_cf_gin", opclasses=["jsonb_path_ops"]),
        ]

    def __str__(self):
        return self.name

    @property
    def archived(self):
        return self.archived_at is not None


class ContractItem(OrgScoped):
    """A record a contract covers (an application, component or AI agent) and its share of the contract's cost in
    the spend reports. A null `allocation_percent` shares what the fixed percentages leave,
    evenly. A contract's fixed percentages sum to at most 100, checked under a lock on the contract row
    (money/contracts.py). PROTECT on the record: a record on a contract is archived,
    not deleted, until its line is removed."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="items")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="contract_items")
    allocation_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["contract", "created_at", "id"]
        verbose_name = "contract line"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["contract", "resource"], name="money_contractitem_unique_resource"),
            models.CheckConstraint(
                condition=Q(allocation_percent__isnull=True)
                | (Q(allocation_percent__gte=0) & Q(allocation_percent__lte=100)),
                name="money_contractitem_percent_range",
            ),
        ]
        indexes = [models.Index(fields=["organization", "resource"], name="money_contractitem_resource")]


class LicensePool(OrgScoped):
    """Seats of one SKU of an application, optionally bought under a contract. Seats
    assigned may exceed seats purchased: overuse is a real state, shown rather than refused. `seat_sync` lets Entra
    licence discovery overwrite the counts (the one exception to discovery never overwriting curated data); at most one pool per SKU of an organization may sync,
    and only one with a SKU ID. `last_synced` is written by discovery, never by a person."""

    application = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="license_pools")
    contract = models.ForeignKey(
        Contract, null=True, blank=True, on_delete=models.PROTECT, related_name="license_pools"
    )
    sku_name = models.CharField(max_length=200)
    sku_id = models.CharField(max_length=64, blank=True)
    seats_purchased = models.PositiveIntegerField(default=0)
    seats_assigned = models.PositiveIntegerField(default=0)
    seat_sync = models.BooleanField(default=False)
    last_synced = models.DateTimeField(null=True, blank=True, editable=False)
    notes = models.TextField(blank=True)
    custom_field_data = models.JSONField(default=dict, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["sku_name"]
        verbose_name = "license pool"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                F("organization"),
                Lower("sku_id"),
                condition=Q(seat_sync=True),
                name="money_licensepool_one_synced_per_sku",
            ),
            models.CheckConstraint(condition=Q(seat_sync=False) | ~Q(sku_id=""), name="money_licensepool_sync_sku"),
        ]
        indexes = [
            models.Index(fields=["organization", "application"], name="money_licensepool_application"),
            models.Index(fields=["organization", "contract"], name="money_licensepool_contract"),
            GinIndex(fields=["custom_field_data"], name="money_licensepool_cf_gin", opclasses=["jsonb_path_ops"]),
        ]

    def __str__(self):
        return self.sku_name

    @property
    def name(self) -> str:
        """Named by its SKU (a pool has no name of its own); refusals and audit summaries read `name`."""
        return self.sku_name


class SeatReviewDecision(models.TextChoices):
    APPLIED = "applied", "Applied"
    DISMISSED = "dismissed", "Dismissed"


class SeatCountReview(OrgScoped):
    """A person's decision on a proposed seat count: what a licence observation (discovery's `license` kind,
    produced by the Entra licences source) said about a pool's SKU, applied to the pool or dismissed.
    `fingerprint` is the observed counts at decision time, so a dismissed proposal comes back only when the source's
    counts change (as discovery's dismissed candidates do). One current decision per pool and observation; a new
    decision replaces it, and the audit log keeps the history. Composite keys (money 0006) keep both in the row's
    organization. Proposals themselves are derived (money/licences.py), never stored."""

    pool = models.ForeignKey(LicensePool, on_delete=models.CASCADE, related_name="seat_reviews")
    observation = models.ForeignKey("discovery.Observation", on_delete=models.CASCADE, related_name="seat_reviews")
    decision = models.CharField(max_length=10, choices=SeatReviewDecision.choices)
    fingerprint = models.CharField(max_length=64)
    reason = models.CharField(max_length=500, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        verbose_name = "seat count review"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["pool", "observation"], name="money_seatcountreview_one_per_pair"),
        ]
        indexes = [models.Index(fields=["organization", "observation"], name="money_seatcountreview_obs")]


# ---- reports ---------------------------------------------------------------------------------------------


class CalendarFeed(OrgScoped):
    """A person's subscription to the renewals calendar (reports; money/calendar_feeds.py). The feed URL
    carries a secret that is the only thing authenticating the request (calendar apps can't sign in), so only its
    SHA-256 is stored, as for API tokens; `prefix` tells feeds apart. It reads as its owner in this organization, and
    stops working when revoked, when the owner leaves the organization or is deactivated. Rotating revokes the row and
    makes a new one, so a revoked secret can never come back. One live feed per person per organization."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_feeds")
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["-created_at"]
        verbose_name = "calendar feed"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=Q(revoked_at__isnull=True),
                name="money_calendarfeed_one_live_per_user",
            ),
        ]
