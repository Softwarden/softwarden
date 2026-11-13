"""Data quality.

A registry of rules (RULES). Each rule is data: a stable id, a label, the record kinds it covers, a severity, what it
needs to be on (a capability; the compliance profile), and two conditions written as plain values (`applies` and
`fails`, trees of Check / AllOf / AnyOf over the named FIELDS). Nothing in a rule is Python code, so the same shape can
be exported, imported and written by an Admin later (custom rules and YAML packs); `Rule.spec()` is that
shape. The conditions compile to SQL (Q objects): one CASE per rule gives each record's outcome (not applicable,
passing, failing, excepted) as a column of a derived table over the organization's records (`_codes_sql`, compiled
once per organization, day and set of rules and cached, since compiling costs the ORM more than the database spends).
Everything else reads that table, so every count, list, filter and score runs in the database:

- a record's results (`evaluate`): one query for a page of records, for the record serializers' `quality`;
- the completeness score: a semi-join for `?completeness__lt=` / `__gte=` (`score_q`), a correlated subquery for
  `?ordering=completeness` (`annotate_score`);
- the organization's counts (`summary`): one query over the live records, counted here, for the summary, the rules
  list and the dashboard panel;
- the Needs attention "Data quality" queue (`attention_rows`): one grouped query.

Relation to the record gaps (catalog/state.py). The ownership and AI agent gaps came first
and drive the `gaps` chips, the Ownership gaps and AI agent gaps queues and the `gap` filter. Rules don't replace them;
the rules that check the same things carry the same labels (imported from state.py, so the wording has one home) and
`covered_by` names the queue that already lists those records, so the Data quality queue never repeats them.
tests/test_data_quality.py checks the two agree on every record of a mixed fixture. Those rules can't be turned off
or excepted, since the gap chips and queues would still show them.

Scope. A rule's `applies` decides which records it checks; the completeness score of any record (archived and retired
included) is over the rules that apply to it. Organization counts, failures and the queue look only at live records:
not archived and not retired. AI agents are left out entirely when the organization lacks the `ai_agents` capability.
Integrations are checked only by the data-flow rules, and left out without the `data_flows` capability; they
count in the summary as their own kind.

Exceptions (QualityException). A reasoned "not applicable" for one exceptable rule on one record, with an accountable
Person and an expiry at most a year ahead; it applies up to and including `expires_on`, judged in the organization's
timezone at query time, so nothing has to run when it lapses. An excepted check is left out of the score (neither
passing nor failing); an exception on a record that passes anyway changes nothing.

Score. For one record: passing checks * 100 // (passing + failing), rounded down so a record with any failing check
never shows 100; 100 when no rule applies. Unweighted: severity orders the lists, it doesn't weigh the score."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db import IntegrityError, connection, transaction
from django.db.models import (
    Case,
    DateField,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    When,
)
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce
from django.db.models.lookups import LessThan

from core.capabilities import (
    AI_AGENTS,
    COMPLIANCE_PROFILE,
    CONTRACTS,
    DATA_FLOWS,
    DATA_QUALITY,
    DISCOVERY_ENTRA,
    EOL,
    REVIEWS,
    VENDOR_RISK,
    has_capability,
)
from core.dates import org_today
from core.problems import Conflict, FieldErrors

from . import compliance
from .models import (
    RECORD_KINDS,
    AttachmentCategory,
    BuiltBy,
    Kind,
    LicenseModel,
    Lifecycle,
    Person,
    QualityException,
    RecordReview,
    Resource,
)
from .state import AGENT_GAP_LABELS, COVERAGE_LABELS, OWNERSHIP_LABELS

SEVERITIES = ("high", "medium", "low")
SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}
SETTING = "data_quality_disabled_rules"  # Organization.settings key (audited: snapshots.ORG_SETTINGS_AUDITED)
EXCEPTION_DAYS = 365  # an exception lasts at most this long; the default when no expiry is given
REASON_MAX = 1000
LIVE_LIFECYCLES = (Lifecycle.PROPOSED, Lifecycle.EVALUATING, Lifecycle.ACTIVE, Lifecycle.DEPRECATED)
DOC_CATEGORIES = (AttachmentCategory.SOP_RUNBOOK, AttachmentCategory.ARCHITECTURE, AttachmentCategory.VENDOR_DOCS)

# Text that is only whitespace counts as blank, by Python's definition (str.isspace, as catalog/state.agent_gaps reads
# it with str.strip): Postgres's \s depends on the locale and misses no-break spaces, so the characters are listed.
WHITESPACE = "".join(chr(c) for c in range(0x3001) if chr(c).isspace())  # none lies above U+3000
BLANK_PATTERN = "^[" + WHITESPACE + "]*$"

# Always true and always false, as conditions that survive negation and OR (an empty Q() doesn't).
TRUE = Q(pk__isnull=False)
FALSE = Q(pk__isnull=True)


# ---- conditions: plain data ----------------------------------------------------------------------------------------

TESTS = ("missing", "is", "in", "not_in", "before_today", "none_in")


@dataclass(frozen=True)
class Check:
    """One test of one field (FIELDS): `missing` (null, or blank text), `is` / `in` / `not_in` a value,
    `before_today` (a date earlier than the organization's today) or `none_in` (no attachment in these categories)."""

    field: str
    test: str
    value: Any = None

    def spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.field, "test": self.test}
        if self.value is not None:
            out["value"] = list(self.value) if isinstance(self.value, tuple) else self.value
        return out


@dataclass(frozen=True)
class AllOf:
    checks: tuple[Any, ...]

    def spec(self) -> dict[str, Any]:
        return {"all": [c.spec() for c in self.checks]}


@dataclass(frozen=True)
class AnyOf:
    checks: tuple[Any, ...]

    def spec(self) -> dict[str, Any]:
        return {"any": [c.spec() for c in self.checks]}


Condition = Check | AllOf | AnyOf


@dataclass(frozen=True)
class Context:
    """What compiling a condition needs: the organization (its review interval and timezone) and its today."""

    org: Any
    today: date


@dataclass(frozen=True)
class Field:
    """A field a condition may test: its ORM path from Resource per record kind (a kind without one doesn't have the
    field, and counts as missing it), and how `missing` reads: `ref` and `null` are null, `text` is null or empty,
    `trimmed` also whitespace only, `array` null or an empty list. A `special` field compiles its own tests."""

    label: str
    paths: Mapping[str, str] = field(default_factory=dict)
    blank: str = "null"  # ref, null, text, trimmed, array
    special: str = ""

    def path(self, kind: str) -> str | None:
        return self.paths.get(kind)


def _all_kinds(path: str) -> dict[str, str]:
    return {k: path for k in RECORD_KINDS}


FIELDS: dict[str, Field] = {
    "lifecycle": Field("Lifecycle", _all_kinds("lifecycle"), "text"),
    "business_owner": Field("Business owner", _all_kinds("business_owner"), "ref"),
    "business_owner.active": Field("Business owner is active", _all_kinds("business_owner__active")),
    "technical_owner": Field("Technical owner", _all_kinds("technical_owner"), "ref"),
    "technical_owner.active": Field("Technical owner is active", _all_kinds("technical_owner__active")),
    "support_team": Field("Support team", _all_kinds("support_team"), "ref"),
    "criticality": Field("Criticality", _all_kinds("criticality"), "text"),
    "data_classification": Field(
        "Data classification", {**_all_kinds("data_classification"), Kind.INTEGRATION: "data_classification"}, "text"
    ),
    # Data-flow map: what kinds of data an integration carries (missing: none recorded).
    "data_categories": Field("Data categories", {Kind.INTEGRATION: "integration__data_categories"}, "array"),
    "vendor": Field("Vendor", {Kind.APPLICATION: "application__vendor"}, "ref"),
    "built_by": Field("Built by", {Kind.APPLICATION: "application__built_by", Kind.COMPONENT: "component__built_by"}),
    "documentation_url": Field(
        "Documentation URL",
        {Kind.APPLICATION: "application__documentation_url", Kind.COMPONENT: "component__documentation_url"},
        "text",
    ),
    "runbook_url": Field("Runbook URL", {Kind.COMPONENT: "component__runbook_url"}, "text"),
    "data_access_scope": Field("Data access scope", {Kind.AI_AGENT: "ai_agent__data_access_scope"}, "trimmed"),
    "human_in_loop": Field("Human in the loop", {Kind.AI_AGENT: "ai_agent__human_in_loop"}),
    "is_gxp": Field("GxP", _all_kinds("is_gxp")),
    "validation_status": Field("Validation status", _all_kinds("validation_status"), "text"),
    "risk_tier": Field("Risk tier", _all_kinds("risk_tier"), "text"),
    "next_periodic_review": Field("Next periodic review", _all_kinds("next_periodic_review")),
    # The record review's due date: the newest review's next review date; a record never reviewed is due one review
    # interval after it was created.
    "review.due": Field("Record review due", special="review_due"),
    # Attachments on the record, by category.
    "attachments": Field("Attachments", special="attachments"),
    "license_model": Field(
        "License model",
        {Kind.APPLICATION: "application__license_model", Kind.COMPONENT: "component__license_model"},
        "text",
    ),
    # The record's lines on contracts that are live or signed and not archived (money/models.py).
    "contract": Field("Contract", special="contract"),
    # The application's vendor's risk file (catalog/vendor_risk.py): its tier and personal-data flag, and
    # whether it has valid assurance and a signed DPA, by the same definitions as the vendor's gaps.
    "vendor.risk_tier": Field(
        "Vendor risk tier", {Kind.APPLICATION: "application__vendor__risk_profile__risk_tier"}, "text"
    ),
    "vendor.personal_data": Field(
        "Vendor processes personal data",
        {Kind.APPLICATION: "application__vendor__risk_profile__processes_personal_data"},
    ),
    "vendor.assurance": Field("Vendor assurance report", special="vendor_assurance"),
    "vendor.dpa": Field("Vendor DPA", special="vendor_dpa"),
    # End-of-life tracking (eol/models.py): whether the record is tracked on endoflife.date (`is` true), and
    # its release cycle, missing when a tracked version's cycle isn't one the product's cached releases list (no
    # version, a version that matches no cycle, a cycle endoflife.date dropped). A product whose releases aren't cached
    # yet is never failing: that is the cache's gap, not the record's.
    "end_of_life": Field("End-of-life tracking", special="eol_tracked"),
    "end_of_life.cycle": Field("End-of-life release cycle", special="eol_cycle"),
    # SSO and MFA coverage (discovery/coverage.py): what the application's Entra app's coverage row says
    # (saml, oidc, password, none; on, off; all_users, some_users, none), "unknown" without one. `is`, `in`, `not_in`.
    "coverage.sso": Field("Single sign-on (Entra)", special="coverage"),
    "coverage.scim": Field("SCIM provisioning (Entra)", special="coverage"),
    "coverage.mfa": Field("Conditional access MFA (Entra)", special="coverage"),
}


def _missing(f: Field, path: str) -> Q:
    if f.blank == "text":
        return Q(**{f"{path}__isnull": True}) | Q(**{path: ""})
    if f.blank == "trimmed":
        return Q(**{f"{path}__isnull": True}) | Q(**{f"{path}__regex": BLANK_PATTERN})
    if f.blank == "array":
        return Q(**{f"{path}__isnull": True}) | Q(**{path: []})
    return Q(**{f"{path}__isnull": True})


def _special(check: Check, ctx: Context) -> Q:
    f = FIELDS[check.field]
    if f.special == "attachments" and check.test == "none_in":
        from .models import Attachment

        return ~Q(Exists(Attachment.objects.filter(resource_id=OuterRef("pk"), category__in=list(check.value))))
    if f.special == "contract" and check.test == "missing":
        from money.models import OBLIGATION_STATUSES, ContractItem

        lines = ContractItem.objects.filter(
            resource_id=OuterRef("pk"), contract__status__in=OBLIGATION_STATUSES, contract__archived_at__isnull=True
        )
        return ~Q(Exists(lines))
    if f.special in ("vendor_assurance", "vendor_dpa") and check.test == "missing":
        from . import vendor_risk

        found = vendor_risk.assured_q if f.special == "vendor_assurance" else vendor_risk.dpa_q
        return ~found("application__vendor_id", ctx.today)
    if f.special == "eol_tracked" and check.test == "is":
        from eol.models import TrackedVersion

        tracked = Q(Exists(TrackedVersion.objects.filter(resource_id=OuterRef("pk"))))
        return tracked if check.value else ~tracked
    if f.special == "eol_cycle" and check.test == "missing":
        from eol.models import EolProduct, EolRelease, TrackedVersion

        cached = EolProduct.objects.filter(name=OuterRef("product"), releases_fetched_at__isnull=False)
        known = EolRelease.objects.filter(product_id=OuterRef("product"), name=OuterRef("cycle"))
        unknown = (
            TrackedVersion.objects.filter(resource_id=OuterRef("pk")).filter(Exists(cached)).exclude(Exists(known))
        )
        return Q(Exists(unknown))
    if f.special == "coverage" and check.test in ("is", "in", "not_in"):
        from discovery import coverage

        dimension = check.field.split(".", 1)[1]
        values = [check.value] if check.test == "is" else list(check.value)
        row = coverage.annotate(Resource.objects.filter(pk=OuterRef("pk"))).filter(**{f"{dimension}__in": values})
        covered = Q(Exists(row))
        return ~covered if check.test == "not_in" else covered
    if f.special == "review_due" and check.test == "before_today":
        latest = (
            RecordReview.objects.filter(resource_id=OuterRef("pk"))
            .order_by("-confirmation_date", "-created_at")
            .values("next_review_date")[:1]
        )
        start = datetime.combine(
            ctx.today - timedelta(days=ctx.org.review_interval_days), time.min, tzinfo=ZoneInfo(ctx.org.timezone)
        )
        never = ~Q(Exists(RecordReview.objects.filter(resource_id=OuterRef("pk")))) & Q(created_at__lt=start)
        # Coalesced, so a record never reviewed compares false rather than null (a null would make `not failing`
        # null too, and the check would count as neither passing nor failing).
        due = Coalesce(Subquery(latest, output_field=DateField()), Value(date.max, output_field=DateField()))
        return Q(LessThan(due, ctx.today)) | never
    raise ValueError(f"{check.field} doesn't support the test {check.test!r}.")


def compile_condition(condition: Condition, kind: str, ctx: Context) -> Q:
    """The condition for records of `kind`, as a Q over Resource."""
    if isinstance(condition, AllOf):
        out = TRUE
        for c in condition.checks:
            out &= compile_condition(c, kind, ctx)
        return out
    if isinstance(condition, AnyOf):
        out = FALSE
        for c in condition.checks:
            out |= compile_condition(c, kind, ctx)
        return out
    check = condition
    f = FIELDS[check.field]
    if f.special:
        return _special(check, ctx)
    path = f.path(kind)
    if path is None:  # the kind doesn't have this field: it is missing, and is no value
        return TRUE if check.test in ("missing", "not_in") else FALSE
    if check.test == "missing":
        return _missing(f, path)
    if check.test == "is":
        return Q(**{path: check.value})
    if check.test == "in":
        return Q(**{f"{path}__in": list(check.value)})
    if check.test == "not_in":
        return ~Q(**{f"{path}__in": list(check.value)})
    if check.test == "before_today":
        return Q(**{f"{path}__lt": ctx.today})
    raise ValueError(f"Unknown test {check.test!r}.")


# ---- rules ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    id: str  # stable: stored on exceptions and snoozes, and in the disabled-rules setting
    label: str
    description: str
    kinds: tuple[str, ...]
    severity: str
    fails: Condition
    applies: Condition | None = None  # None: every record of its kinds
    capability: str | None = None  # the capability it needs (besides data_quality)
    compliance: bool = False  # needs the compliance profile on
    exceptable: bool = False
    covered_by: str = ""  # the Needs attention queue that already lists these records
    planned: str = ""  # registered but not evaluated yet: why

    def spec(self) -> dict[str, Any]:
        """The rule as plain data (what a YAML export or a custom rule holds)."""
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "kinds": list(self.kinds),
            "severity": self.severity,
            "applies": self.applies.spec() if self.applies else None,
            "fails": self.fails.spec(),
            "capability": self.capability,
            "compliance_profile": self.compliance,
            "exceptable": self.exceptable,
        }

    def scope(self, ctx: Context) -> Q:
        out = FALSE
        for kind in self.kinds:
            out |= Q(kind=kind) & (compile_condition(self.applies, kind, ctx) if self.applies else TRUE)
        return out

    def failing(self, ctx: Context) -> Q:
        """The records it applies to that fail it, before exceptions."""
        out = FALSE
        for kind in self.kinds:
            applies = compile_condition(self.applies, kind, ctx) if self.applies else TRUE
            out |= Q(kind=kind) & applies & compile_condition(self.fails, kind, ctx)
        return out

    def excepted(self, ctx: Context) -> Q:
        """Records with an exception for this rule that applies today."""
        if not self.exceptable:
            return FALSE
        return Q(
            Exists(QualityException.objects.filter(resource_id=OuterRef("pk"), rule=self.id, expires_on__gte=ctx.today))
        )


OWNER_KINDS = tuple(RECORD_KINDS)
DOC_KINDS = (Kind.APPLICATION, Kind.COMPONENT)
FLOW_KINDS = (Kind.INTEGRATION,)
# Every kind a rule may check: the record kinds, and integrations for the data-flow rules.
QUALITY_KINDS = [*RECORD_KINDS, Kind.INTEGRATION]

RULES: tuple[Rule, ...] = (
    Rule(
        "business_owner",
        OWNERSHIP_LABELS["business_owner"],
        "Nobody is named to decide on it for the business.",
        OWNER_KINDS,
        "high",
        Check("business_owner", "missing"),
        covered_by="ownership_gaps",
    ),
    Rule(
        "technical_owner",
        OWNERSHIP_LABELS["technical_owner"],
        "Nobody is named to keep it running.",
        OWNER_KINDS,
        "high",
        Check("technical_owner", "missing"),
        covered_by="ownership_gaps",
    ),
    Rule(
        "support_team",
        OWNERSHIP_LABELS["support_team"],
        "No team takes its support calls.",
        OWNER_KINDS,
        "medium",
        Check("support_team", "missing"),
        covered_by="ownership_gaps",
    ),
    Rule(
        "inactive_owner",
        OWNERSHIP_LABELS["inactive_owner"],
        "Its business or technical owner has left or is marked inactive.",
        OWNER_KINDS,
        "high",
        AnyOf((Check("business_owner.active", "is", False), Check("technical_owner.active", "is", False))),
        covered_by="ownership_gaps",
    ),
    Rule(
        "criticality",
        OWNERSHIP_LABELS["criticality"],
        "How much the business depends on it isn't recorded.",
        OWNER_KINDS,
        "medium",
        Check("criticality", "missing"),
    ),
    Rule(
        "data_classification",
        "No data classification",
        "How sensitive its data is isn't recorded.",
        OWNER_KINDS,
        "medium",
        Check("data_classification", "missing"),
    ),
    Rule(
        "documentation",
        "No documentation",
        "No documentation or runbook URL, and no SOP, architecture or vendor documentation attachment.",
        DOC_KINDS,
        "low",
        AllOf(
            (
                Check("documentation_url", "missing"),
                Check("runbook_url", "missing"),
                Check("attachments", "none_in", DOC_CATEGORIES),
            )
        ),
        exceptable=True,
    ),
    Rule(
        "vendor",
        "No vendor",
        "An application not built in-house with no vendor recorded.",
        (Kind.APPLICATION,),
        "medium",
        Check("vendor", "missing"),
        applies=Check("built_by", "not_in", (BuiltBy.INTERNAL,)),
        exceptable=True,
    ),
    Rule(
        "contract",
        "Not on a contract",
        "Paid software with no line on a live or signed contract. Mark bundled software not applicable.",
        (Kind.APPLICATION,),
        "low",
        Check("contract", "missing"),
        applies=AllOf(
            (
                Check("built_by", "not_in", (BuiltBy.INTERNAL,)),
                Check("license_model", "not_in", (LicenseModel.OPEN_SOURCE, LicenseModel.FREE, LicenseModel.INTERNAL)),
            )
        ),
        capability=CONTRACTS,
        exceptable=True,
    ),
    Rule(
        "review_overdue",
        "Review overdue",
        "The record review is past its due date (never reviewed: one review interval after it was added).",
        OWNER_KINDS,
        "medium",
        Check("review.due", "before_today"),
        applies=Check("lifecycle", "in", (Lifecycle.ACTIVE, Lifecycle.DEPRECATED)),
        capability=REVIEWS,
        covered_by="reviews",
    ),
    Rule(
        "agent_data_access_scope",
        AGENT_GAP_LABELS["scope"],
        "What data the agent can reach isn't recorded.",
        (Kind.AI_AGENT,),
        "high",
        Check("data_access_scope", "missing"),
        capability=AI_AGENTS,
        covered_by="agent_gaps",
    ),
    Rule(
        "agent_human_in_loop",
        AGENT_GAP_LABELS["hitl"],
        "Whether a person approves its actions isn't decided (no is a decision).",
        (Kind.AI_AGENT,),
        "high",
        Check("human_in_loop", "missing"),
        capability=AI_AGENTS,
        covered_by="agent_gaps",
    ),
    # The vendor risk file's gaps, judged on the vendor's applications. The Vendor risk queue already lists
    # the vendors (one row each), so the Data quality queue leaves these out.
    Rule(
        "vendor_assurance",
        "Vendor has no current SOC 2 or equivalent",
        "Its vendor is high or critical risk with no current SOC 2 report or equivalent.",
        (Kind.APPLICATION,),
        "high",
        Check("vendor.assurance", "missing"),
        applies=Check("vendor.risk_tier", "in", ("high", "critical")),
        capability=VENDOR_RISK,
        covered_by="vendor_risk",
    ),
    Rule(
        "vendor_dpa",
        "Vendor has no signed DPA",
        "Its vendor processes personal data with no signed data processing agreement.",
        (Kind.APPLICATION,),
        "high",
        Check("vendor.dpa", "missing"),
        applies=Check("vendor.personal_data", "is", True),
        capability=VENDOR_RISK,
        covered_by="vendor_risk",
    ),
    Rule(
        "eol_cycle_unknown",
        "End-of-life cycle unknown",
        "Tracked on endoflife.date, but no release cycle matches: set the version or the cycle.",
        DOC_KINDS,
        "low",
        Check("end_of_life.cycle", "missing"),
        applies=Check("end_of_life", "is", True),
        capability=EOL,
    ),
    # Data-flow map (catalog/data_flows.py): an integration is a data flow, and the map needs to know how
    # sensitive its data is and, for confidential data and up, what kinds of data it is.
    Rule(
        "flow_classification",
        "Data flow not classified",
        "How sensitive the data this integration moves is isn't recorded.",
        FLOW_KINDS,
        "medium",
        Check("data_classification", "missing"),
        capability=DATA_FLOWS,
    ),
    Rule(
        "flow_categories",
        "No data categories",
        "A confidential or restricted data flow with no data categories (personal, financial, credentials and so on).",
        FLOW_KINDS,
        "low",
        Check("data_categories", "missing"),
        applies=Check("data_classification", "in", ("confidential", "restricted")),
        capability=DATA_FLOWS,
    ),
    # SSO and MFA coverage gaps, from an `entra_coverage` connector's last run. They apply only where the
    # coverage is known: an app nobody linked to Entra, or whose answer is unknown, is neither passing nor failing.
    Rule(
        "sso_missing",
        COVERAGE_LABELS["no_sso"],
        "Microsoft Entra ID says single sign-on isn't set up for its enterprise application.",
        (Kind.APPLICATION,),
        "medium",
        Check("coverage.sso", "is", "none"),
        applies=Check("coverage.sso", "not_in", ("unknown",)),
        capability=DISCOVERY_ENTRA,
        exceptable=True,
    ),
    Rule(
        "mfa_missing",
        COVERAGE_LABELS["no_mfa"],
        "No enabled conditional access policy requires multifactor authentication to sign in to it.",
        (Kind.APPLICATION,),
        "high",
        Check("coverage.mfa", "is", "none"),
        applies=Check("coverage.mfa", "not_in", ("unknown",)),
        capability=DISCOVERY_ENTRA,
        exceptable=True,
    ),
    Rule(
        "scim_missing",
        COVERAGE_LABELS["no_scim"],
        "Its enterprise application has no provisioning (SCIM) running, so accounts are added and removed by hand.",
        (Kind.APPLICATION,),
        "low",
        Check("coverage.scim", "is", "off"),
        applies=Check("coverage.scim", "not_in", ("unknown",)),
        capability=DISCOVERY_ENTRA,
        exceptable=True,
    ),
    Rule(
        "validation_status",
        "No validation status",
        "Compliance profile: the validation status isn't recorded (Not required is a status).",
        OWNER_KINDS,
        "medium",
        Check("validation_status", "missing"),
        capability=COMPLIANCE_PROFILE,
        compliance=True,
    ),
    Rule(
        "risk_tier",
        "No risk tier",
        "Compliance profile: the risk tier isn't recorded.",
        OWNER_KINDS,
        "medium",
        Check("risk_tier", "missing"),
        capability=COMPLIANCE_PROFILE,
        compliance=True,
    ),
    Rule(
        "periodic_review_date",
        "No periodic review date",
        "Compliance profile: GxP or high risk, with no next periodic review date.",
        OWNER_KINDS,
        "medium",
        Check("next_periodic_review", "missing"),
        applies=AnyOf((Check("is_gxp", "is", True), Check("risk_tier", "is", "high"))),
        capability=COMPLIANCE_PROFILE,
        compliance=True,
        exceptable=True,
    ),
    Rule(
        "periodic_review_overdue",
        "Periodic review overdue",
        "Compliance profile: the next periodic review date has passed.",
        OWNER_KINDS,
        "high",
        Check("next_periodic_review", "before_today"),
        capability=COMPLIANCE_PROFILE,
        compliance=True,
    ),
)
BY_ID: dict[str, Rule] = {r.id: r for r in RULES}


# ---- which rules are on for an organization ------------------------------------------------------------------------

ON, OFF, UNAVAILABLE = "on", "off", "unavailable"


def disabled_ids(org) -> list[str]:
    value = (org.settings or {}).get(SETTING) or []
    return [v for v in value if isinstance(v, str)]


def status(rule: Rule, org) -> tuple[str, str]:
    """(on | off | unavailable, why) for one rule in one organization."""
    if rule.planned:
        return UNAVAILABLE, rule.planned
    if not has_capability(org, DATA_QUALITY):
        return UNAVAILABLE, "Needs the data_quality capability, which this organization's edition doesn't have."
    if rule.capability and not has_capability(org, rule.capability):
        return UNAVAILABLE, f"Needs the {rule.capability} capability, which this organization's edition doesn't have."
    if rule.compliance and not compliance.enabled(org):
        return UNAVAILABLE, "The compliance profile is off. An Admin can turn it on in Organization settings."
    if rule.id in disabled_ids(org):
        return OFF, "Turned off in Organization settings."
    return ON, ""


def active_rules(org) -> list[Rule]:
    return [r for r in RULES if status(r, org)[0] == ON]


def checked_kinds(org) -> list[str]:
    """The kinds data quality looks at: AI agents only with the ai_agents capability, integrations (the data-flow
    rules) only with data_flows."""
    gated = {Kind.AI_AGENT: AI_AGENTS, Kind.INTEGRATION: DATA_FLOWS}
    return [k for k in QUALITY_KINDS if k not in gated or has_capability(org, gated[k])]


def live(org) -> Q:
    return Q(organization=org, kind__in=checked_kinds(org), archived_at__isnull=True, lifecycle__in=LIVE_LIFECYCLES)


def clean_disabled(data: dict[str, Any], errors: dict[str, list[str]]) -> list[str] | None:
    """The disabled-rules setting from an Organization settings PATCH (catalog/admin_services.py), or None when not
    sent. A list of rule ids; rules that mirror ownership, agent or review gaps can't be turned off (their chips and
    queues would still show them)."""
    if SETTING not in data:
        return None
    value = data[SETTING]
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors[SETTING] = ["Send a list of rule ids."]
        return None
    unknown = [v for v in value if v not in BY_ID]
    fixed = [v for v in value if v in BY_ID and BY_ID[v].covered_by]
    if unknown:
        errors[SETTING] = [f"Unknown rule: {', '.join(sorted(set(unknown)))}."]
    elif fixed:
        errors[SETTING] = [
            f"These checks are also gaps in Needs attention and can't be turned off: {', '.join(sorted(set(fixed)))}."
        ]
    return sorted(set(value))


# ---- evaluation ----------------------------------------------------------------------------------------------------


@dataclass
class Result:
    """One record's results: the rules it fails and the ones excepted for it, and its score."""

    failing: list[Rule] = field(default_factory=list)
    excepted: list[Rule] = field(default_factory=list)
    passing: int = 0

    @property
    def applicable(self) -> int:
        return self.passing + len(self.failing)

    @property
    def score(self) -> int:
        return 100 if self.applicable == 0 else self.passing * 100 // self.applicable


# Each rule's outcome for a record, as one integer column: one CASE per rule, so the database evaluates its condition
# once per row, whatever the caller then counts.
NA, PASS, FAIL, EXCEPTED = 0, 1, 2, 3


def _code(rule: Rule, ctx: Context) -> Case:
    whens = [When(~rule.scope(ctx), then=Value(NA)), When(~rule.failing(ctx), then=Value(PASS))]
    if rule.exceptable:
        whens.append(When(rule.excepted(ctx), then=Value(EXCEPTED)))
    return Case(*whens, default=Value(FAIL), output_field=IntegerField())


def context(org, today: date | None = None) -> Context:
    return Context(org, today or org_today(org))


# Compiled SQL per organization, day and set of rules. Building and compiling a dozen rule conditions costs the ORM
# about 15 ms, several times what the database then spends, and every record list page, the summary and Needs
# attention ask for them; the SQL only changes with the key below (the organization's settings that rules read,
# today, and which rules are on), so it is compiled once per process and reused. Bounded: a full cache is emptied.
_SQL_CACHE: dict[tuple[Any, ...], tuple[str, tuple[Any, ...]]] = {}
_SQL_CACHE_MAX = 256


def _codes_sql(ctx: Context, rules: list[Rule]) -> tuple[str, tuple[Any, ...]]:
    """SELECT id, kind, lifecycle, archived_at, it_steward_id, c0..cN over the organization's records, where c<i> is
    rule i's outcome (NA, PASS, FAIL or EXCEPTED). Used as a derived table: the database pushes the caller's WHERE
    down into it, so only the rows asked for are evaluated."""
    org = ctx.org
    key = (org.pk, ctx.today, org.review_interval_days, org.timezone, tuple(r.id for r in rules))
    found = _SQL_CACHE.get(key)
    if found is None:
        codes = {f"c{i}": _code(rule, ctx) for i, rule in enumerate(rules)}
        qs = (
            Resource.objects.filter(organization=org)
            .annotate(**codes)
            .values("id", "kind", "lifecycle", "archived_at", "it_steward_id", *codes)
            .order_by()
        )
        sql, params = qs.query.sql_with_params()
        if len(_SQL_CACHE) >= _SQL_CACHE_MAX:
            _SQL_CACHE.clear()
        found = _SQL_CACHE[key] = (sql, tuple(params))
    return found


def _columns(rules: list[Rule]) -> str:
    return ", ".join(f"t.c{i}" for i in range(len(rules)))


def _live_where(org, active_only: bool = False) -> tuple[str, list[Any]]:
    lifecycles = [Lifecycle.ACTIVE, Lifecycle.DEPRECATED] if active_only else list(LIVE_LIFECYCLES)
    return (
        "t.kind = ANY(%s) AND t.archived_at IS NULL AND t.lifecycle = ANY(%s)",
        [[str(k) for k in checked_kinds(org)], [str(v) for v in lifecycles]],
    )


def _fetch(ctx: Context, rules: list[Rule], select: str, where: str, params: list[Any]) -> list[tuple[Any, ...]]:
    inner, inner_params = _codes_sql(ctx, rules)
    sql = f"SELECT {select} FROM ({inner}) t WHERE {where}"  # fixed fragments; values are parameters
    with connection.cursor() as cursor:
        cursor.execute(sql, [*inner_params, *params])
        return cursor.fetchall()


def evaluate(org, ids: Iterable[Any], today: date | None = None, rules: list[Rule] | None = None) -> dict[Any, Result]:
    """{record id: Result} for these records of the organization, in one query."""
    ids = list(ids)
    if not ids:
        return {}
    ctx = context(org, today)
    rules = active_rules(org) if rules is None else rules
    found: dict[Any, Result] = {i: Result() for i in ids}
    if not rules:
        return found
    for rid, *codes in _fetch(ctx, rules, f"t.id, {_columns(rules)}", "t.id = ANY(%s)", [ids]):
        result = found[rid]
        for rule, code in zip(rules, codes, strict=True):
            if code == FAIL:
                result.failing.append(rule)
            elif code == EXCEPTED:
                result.excepted.append(rule)
            elif code == PASS:
                result.passing += 1
    return found


def score_expression(org, today: date | None = None):
    """The completeness score of each record as an SQL expression (the same arithmetic as Result.score): a correlated
    subquery over the record's rule outcomes."""
    rules = active_rules(org)
    if not rules:
        return Value(100, output_field=IntegerField())
    inner, params = _codes_sql(context(org, today), rules)
    # Correlated with the outer query's Resource table by its name: a queryset of Resource always aliases its base table
    # "catalog_resource" (only record lists and failures call this, through annotate_score).
    sql = (
        f"SELECT COALESCE(100 * count(*) FILTER (WHERE u.code = {PASS}) / "
        f"NULLIF(count(*) FILTER (WHERE u.code IN ({PASS}, {FAIL})), 0), 100) "
        f"FROM ({inner}) t, unnest(ARRAY[{_columns(rules)}]) AS u(code) "
        f'WHERE t.id = "{Resource._meta.db_table}"."id"'
    )
    return RawSQL(sql, params, output_field=IntegerField())


def _score_of_row(rules: list[Rule]) -> str:
    """The score of derived-table row t from its outcome columns, each read once."""
    return (
        f"(SELECT COALESCE(100 * count(*) FILTER (WHERE u.code = {PASS}) / "
        f"NULLIF(count(*) FILTER (WHERE u.code IN ({PASS}, {FAIL})), 0), 100) "
        f"FROM unnest(ARRAY[{_columns(rules)}]) AS u(code))"
    )


SCORE_LOOKUPS = {"lt": "<", "gte": ">="}


def score_q(org, lookup: str, value: int, today: date | None = None) -> Q:
    """Records whose completeness is below (lt) or at least (gte) `value`, as one pass over the organization's records
    (a semi-join), rather than a subquery per row of the list."""
    rules = active_rules(org)
    if not rules:
        return TRUE if (100 < value if lookup == "lt" else 100 >= value) else FALSE
    inner, params = _codes_sql(context(org, today), rules)
    sql = f"SELECT t.id FROM ({inner}) t WHERE {_score_of_row(rules)} {SCORE_LOOKUPS[lookup]} %s"
    return Q(id__in=RawSQL(sql, (*params, value)))


def annotate_score(qs, org, today: date | None = None):
    """`completeness` on each row (filters and ordering use it). The organization is the request's."""
    if "completeness" in qs.query.annotations:
        return qs
    return qs.annotate(completeness=score_expression(org, today))


def failing_q(org, rule: Rule | None = None, today: date | None = None) -> Q:
    """Records failing `rule` (or any active rule), after exceptions, whatever their lifecycle."""
    rules = active_rules(org)
    if rule is not None:
        if rule not in rules:
            return FALSE
        test = f"t.c{rules.index(rule)} = {FAIL}"
    elif rules:
        test = f"{FAIL} IN ({_columns(rules)})"
    else:
        return FALSE
    inner, params = _codes_sql(context(org, today), rules)
    return Q(id__in=RawSQL(f"SELECT t.id FROM ({inner}) t WHERE {test}", params))


def failing_records(org, rule: Rule | None = None, today: date | None = None) -> Q:
    """Live records failing `rule` (or any active rule), after exceptions."""
    return live(org) & failing_q(org, rule, today)


# ---- organization counts -------------------------------------------------------------------------------------------


@dataclass
class Counts:
    records: int = 0
    complete: int = 0
    passing: int = 0
    failing: int = 0
    excepted: int = 0

    @property
    def score(self) -> int | None:
        applicable = self.passing + self.failing
        return None if applicable == 0 else self.passing * 100 // applicable

    def add(self, code: int) -> None:
        if code == PASS:
            self.passing += 1
        elif code == FAIL:
            self.failing += 1
        elif code == EXCEPTED:
            self.excepted += 1


def summary(org, today: date | None = None) -> dict[str, Any]:
    """The organization's live records, overall and by kind and by rule, from one query (each live record's rule
    outcomes, counted here): {"total": Counts, "kinds": {kind: Counts}, "rules": {rule id: Counts}, "active": [Rule],
    "today": date}."""
    ctx = context(org, today)
    rules = active_rules(org)
    total = Counts()
    kinds = {k: Counts() for k in checked_kinds(org)}
    per_rule = {r.id: Counts() for r in rules}
    rows: list[tuple[Any, ...]]
    if rules:
        where, params = _live_where(org)
        rows = _fetch(ctx, rules, f"t.kind, {_columns(rules)}", where, params)
    else:
        rows = [(kind,) for kind in Resource.objects.filter(live(org)).values_list("kind", flat=True)]
    for kind, *codes in rows:
        k = kinds.setdefault(kind, Counts())
        k.records += 1
        if FAIL not in codes:
            k.complete += 1
        for rule, code in zip(rules, codes, strict=True):
            k.add(code)
            c = per_rule[rule.id]
            c.add(code)
            c.records += code != NA
    for k in kinds.values():
        total.records += k.records
        total.complete += k.complete
        total.passing += k.passing
        total.failing += k.failing
        total.excepted += k.excepted
    return {"total": total, "kinds": kinds, "rules": per_rule, "active": rules, "today": ctx.today}


# ---- Needs attention ------------------------------------------------------------------------------------------------


def attention_rows(org, today: date) -> list[dict[str, Any]]:
    """The Data quality queue: one row per active rule and IT steward with live (active or deprecated) records failing
    it, routed to that steward (records without one go to the coordinator). Rules whose records another queue already
    lists (covered_by) are left out, so no record is listed twice for the same reason. A row, not a record per row:
    a new field such as data classification can fail hundreds of records at once."""
    ctx = context(org, today)
    rules = [r for r in active_rules(org) if not r.covered_by]
    if not rules:
        return []
    where, params = _live_where(org, active_only=True)
    # Summed in the database (each outcome column read once), only for the rules this queue lists.
    sums = ", ".join(f"SUM(CASE WHEN t.c{i} = {FAIL} THEN 1 ELSE 0 END)" for i in range(len(rules)))
    counts: dict[tuple[Any, int], int] = {}
    for steward, *found in _fetch(ctx, rules, f"t.it_steward_id, {sums}", where + " GROUP BY t.it_steward_id", params):
        for i, count in enumerate(found):
            if count:
                counts[steward, i] = count
    out: list[dict[str, Any]] = []
    for (steward, i), count in counts.items():
        rule = rules[i]
        noun = "record" if count == 1 else "records"
        out.append(
            {
                "id": f"{rule.id}:{steward if steward is not None else 'none'}",
                "kind": "data_quality",
                "rule": rule.id,
                "label": rule.label,
                "severity": rule.severity,
                "count": count,
                "it_steward": steward,
                "why": f"{count} {noun}: {rule.description}",
                "steward_id": steward,
            }
        )
    out.sort(key=lambda r: (SEVERITY_ORDER[r["severity"]], -r["count"], r["label"].lower(), r["id"]))
    return out


# ---- Exceptions (writes: validate; scope; audit) ---------------------------------------------------------------------


def exception_audit(e: QualityException) -> dict[str, Any]:
    rule = BY_ID.get(e.rule)
    return {
        "resource": str(e.resource_id),
        "rule": e.rule,
        "rule_label": rule.label if rule else e.rule,
        "reason": e.reason,
        "accountable": str(e.owner_id),
        "expires_on": e.expires_on.isoformat(),
    }


def _reason(data, errors) -> str | None:
    value = data.get("reason")
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        errors["reason"] = ["Say why this check doesn't apply."]
        return None
    if len(text) > REASON_MAX:
        errors["reason"] = [f"Use {REASON_MAX:,} characters or fewer."]
        return None
    return text


def _owner(org, data, errors) -> Person | None:
    from .services import is_uuid

    value = data.get("owner")
    if value in (None, ""):
        errors["owner"] = ["Choose who answers for this decision."]
        return None
    person = Person.objects.filter(organization=org, id=value).first() if is_uuid(value) else None
    if person is None:
        errors["owner"] = ["Choose an existing person."]
    elif not person.active:
        errors["owner"] = ["Choose an active person."]
    return person


def _expires(org, data, errors) -> date | None:
    today = org_today(org)
    latest = today + timedelta(days=EXCEPTION_DAYS)
    value = data.get("expires_on")
    if value in (None, ""):
        return latest
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        day = None
    if day is None:
        errors["expires_on"] = ["Enter a date as YYYY-MM-DD."]
    elif day < today:
        errors["expires_on"] = ["Choose today or a later date."]
    elif day > latest:
        errors["expires_on"] = [f"Choose a date at most {EXCEPTION_DAYS} days ahead ({latest.isoformat()})."]
    else:
        return day
    return None


def _duplicate(record, rule: Rule, existing: QualityException) -> Conflict:
    return Conflict(
        f"{record.name} already has an exception for “{rule.label}” (until {existing.expires_on.isoformat()}). "
        "Change that one instead.",
        extra={"existing": str(existing.id)},
    )


@transaction.atomic
def create_exception(org, actor, data: dict[str, Any]) -> QualityException:
    """Mark one rule not applicable to one record. The rule must be on and exceptable and apply to the record's kind;
    one exception per record and rule (an expired one is extended by changing its expiry)."""
    from .services import audit, is_uuid

    errors: dict[str, list[str]] = {}
    record_id = data.get("record")
    record = None
    if record_id in (None, ""):
        errors["record"] = ["Choose a record."]
    else:
        record = (
            Resource.objects.filter(organization=org, id=record_id, kind__in=checked_kinds(org)).first()
            if is_uuid(record_id)
            else None
        )
        if record is None:
            errors["record"] = ["Choose an existing record."]
    wanted = data.get("rule")
    rule = BY_ID.get(wanted) if isinstance(wanted, str) else None
    if rule is None:
        errors["rule"] = ["Choose a data-quality rule."]
    elif not rule.exceptable:
        errors["rule"] = ["This check can't be marked not applicable; record the value instead."]
    elif status(rule, org)[0] != ON:
        errors["rule"] = [f"This check isn't on: {status(rule, org)[1]}"]
    elif record is not None and record.kind not in rule.kinds:
        errors["rule"] = ["This check doesn't apply to this kind of record."]
    reason = _reason(data, errors)
    owner = _owner(org, data, errors)
    expires = _expires(org, data, errors)
    if errors:
        raise FieldErrors(errors)
    assert record is not None and rule is not None and reason is not None and owner is not None and expires is not None
    existing = QualityException.objects.filter(organization=org, resource=record, rule=rule.id).first()
    if existing is not None:
        raise _duplicate(record, rule, existing)
    exception = QualityException(
        organization=org,
        resource=record,
        rule=rule.id,
        reason=reason,
        owner=owner,
        expires_on=expires,
        created_by=actor,
    )
    try:
        with transaction.atomic():  # a savepoint: a concurrent duplicate fails here, not the whole request
            exception.save()
    except IntegrityError as error:
        if "catalog_qualityexception_unique" not in str(error):
            raise
        existing = QualityException.objects.get(organization=org, resource=record, rule=rule.id)
        raise _duplicate(record, rule, existing) from None
    audit(org, actor, "quality_exception.created", exception, exception_audit(exception))
    return exception


def _plain(value: Any) -> Any:
    if isinstance(value, Person):
        return str(value.id)
    return value.isoformat() if isinstance(value, date) else value


@transaction.atomic
def update_exception(org, actor, exception: QualityException, data: dict[str, Any]) -> QualityException:
    """Change the reason, the owner or the expiry (extending one that lapsed). The record and the rule never change."""
    errors: dict[str, list[str]] = {}
    for fixed in ("record", "rule"):
        if fixed in data and str(data[fixed]) != str(exception.resource_id if fixed == "record" else exception.rule):
            errors[fixed] = ["An exception can't move; remove it and add a new one."]
    values: dict[str, Any] = {}
    if "reason" in data:
        values["reason"] = _reason(data, errors)
    if "owner" in data:
        values["owner"] = _owner(org, data, errors)
    if "expires_on" in data:
        expires = _expires(org, data, errors)
        if expires is not None and expires < exception.expires_on:
            errors["expires_on"] = ["Choose a date on or after the current expiry; remove the exception to end it."]
        values["expires_on"] = expires
        # Extending keeps someone accountable: an owner who has left or was anonymized can't carry it on.
        owner = values.get("owner", exception.owner)
        changed = expires is not None and expires != exception.expires_on
        if changed and owner is not None and not owner.active and "owner" not in errors:
            errors["owner"] = ["Choose an active person to extend it."]
    if errors:
        raise FieldErrors(errors)
    from .services import audit

    changes: dict[str, Any] = {}
    for key, value in values.items():
        before = getattr(exception, key)
        if before != value:
            changes["accountable" if key == "owner" else key] = [_plain(before), _plain(value)]
            setattr(exception, key, value)
    if changes:
        exception.save()
        audit(
            org,
            actor,
            "quality_exception.updated",
            exception,
            {"resource": str(exception.resource_id), "rule": exception.rule, **changes},
        )
    return exception


@transaction.atomic
def delete_exception(org, actor, exception: QualityException) -> None:
    from .services import audit

    audit(org, actor, "quality_exception.deleted", exception, exception_audit(exception))
    exception.delete()


def remove_for(org, actor, resource) -> int:
    """Remove a record's exceptions as it is deleted (inside the delete's transaction), each audited."""
    rows = list(QualityException.objects.filter(organization=org, resource=resource))
    for row in rows:
        delete_exception(org, actor, row)
    return len(rows)


# ---- API shapes (catalog/quality_viewsets.py; described by catalog/serializers.Quality*) ----------------------------


def _counts_json(c: Counts) -> dict[str, Any]:
    return {"records": c.records, "passing": c.passing, "failing": c.failing, "excepted": c.excepted, "score": c.score}


def rules_json(org, today: date | None = None) -> list[dict[str, Any]]:
    """Every registered rule with its status here and, while on, its counts over the live records."""
    counts = summary(org, today)["rules"]
    out = []
    for rule in RULES:
        state, why = status(rule, org)
        out.append(
            {
                "id": rule.id,
                "label": rule.label,
                "description": rule.description,
                "kinds": list(rule.kinds),
                "severity": rule.severity,
                "exceptable": rule.exceptable,
                "covered_by": rule.covered_by or None,
                "status": state,
                "status_reason": why,
                "spec": rule.spec(),
                "counts": _counts_json(counts[rule.id]) if rule.id in counts else None,
            }
        )
    return out


def summary_json(org, today: date | None = None) -> dict[str, Any]:
    found = summary(org, today)
    total: Counts = found["total"]
    states = [status(r, org)[0] for r in RULES]
    by_rule = [
        {"id": r.id, "label": r.label, "severity": r.severity, **_counts_json(found["rules"][r.id])}
        for r in sorted(found["active"], key=lambda r: (SEVERITY_ORDER[r.severity], r.label))
    ]
    return {
        "as_of": found["today"].isoformat(),
        "score": total.score,
        "records": total.records,
        "complete": total.complete,
        "passing": total.passing,
        "failing": total.failing,
        "excepted": total.excepted,
        "by_kind": [
            {
                "kind": kind,
                "records": c.records,
                "complete": c.complete,
                "passing": c.passing,
                "failing": c.failing,
                "excepted": c.excepted,
                "score": c.score,
            }
            for kind, c in found["kinds"].items()
        ],
        "by_rule": by_rule,
        "rules": {s: states.count(s) for s in (ON, OFF, UNAVAILABLE)},
    }
