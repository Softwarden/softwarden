"""django-filter FilterSets for the catalog lists, at parity with what the SPA sends (apps/web/src/catalog-list.tsx,
integrations.tsx, record-picker.tsx, detail.tsx, assessments.tsx, saved filters). They replaced catalog/listing.py;
CSV export and saved filters validate their queries through the same FilterSets (`query_errors`), so a query the
list refuses is refused there too, with the same messages.

Column filters and search run in SQL. `gap` and `review_state` are derived in Python (catalog/state.py), so they are
resolved last, over the whole filtered set, into an ID filter; pagination runs after, so a page is never short."""

from typing import Any

from django import forms
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Collate
from django.http import QueryDict
from django.shortcuts import get_object_or_404

from core.dates import org_today
from core.drf import (
    ParamFilter,
    TenantFilterSet,
    choice,
    choices_csv,
    filter_errors,
    stripped,
    text,
    user_id,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors

from . import compliance, customfields, quality
from .models import (
    CUSTOM_FIELD_KINDS,
    AgentAuthMode,
    AgentChannel,
    AgentModel,
    AgentPlatform,
    AgentRisk,
    AIModel,
    AssessmentSnapshot,
    AssessmentStatus,
    AuthenticationMethod,
    ChangeAssessment,
    ChangeType,
    ComponentType,
    Criticality,
    CustomField,
    CustomFieldType,
    DataCategory,
    DataClassification,
    DeploymentModel,
    Environment,
    Hosting,
    IntegrationAuthMethod,
    IntegrationDirection,
    Kind,
    Lifecycle,
    ModelModality,
    Person,
    RecordReview,
    Relationship,
    RelationshipKind,
    RelationshipType,
    Resource,
    ResourceTag,
    RiskTier,
    ValidationStatus,
    Vendor,
)
from .state import ownership_gaps, review_state

TRUE_FALSE = ("true", "false")
REVIEW_STATES = ("unreviewed", "needs_confirmation", "overdue", "confirmed")
PURPOSE_FIELD = {
    Kind.APPLICATION: "application__purpose",
    Kind.COMPONENT: "component__purpose",
    Kind.INTEGRATION: "integration__data_purpose",
    Kind.AI_AGENT: "ai_agent__purpose",
}


def percent(value: str) -> int:
    number = whole_number(value, 0, 100)
    if number is None:
        raise forms.ValidationError("Use a whole number from 0 to 100.")
    return number


def latest_reviews(org, resource_ids):
    """{resource_id: newest RecordReview}; DISTINCT ON keeps one row per resource instead of the whole history."""
    reviews = (
        RecordReview.objects.filter(organization=org, resource_id__in=resource_ids)
        .only("resource_id", "confirmation_date", "next_review_date", "confirmed_values")
        .order_by("resource_id", "-confirmation_date", "-created_at")
        .distinct("resource_id")
    )
    return {review.resource_id: review for review in reviews}


def search(qs, kind, q):
    """Full-text over name, description and purpose; trigram similarity (whole name and best word) plus substring
    on name for typos and partial words. The caller ranks exact name matches first, then prefix matches."""
    vector = (
        SearchVector("name", weight="A")
        + SearchVector("description", weight="B")
        + SearchVector(PURPOSE_FIELD[kind], weight="B")
    )
    return qs.annotate(search_vector=vector).filter(
        Q(search_vector=SearchQuery(q, search_type="websearch"))
        | Q(name__trigram_similar=q)
        | Q(name__trigram_word_similar=q)
        | Q(name__icontains=q)
    )


def ranked(qs, kind, q):
    """Search when there is a term, annotating `rank_bucket` (0 exact name, 1 name prefix, 2 the rest); without
    one every row is bucket 0, so the default ordering (rank_bucket, name) is by name."""
    if not q:
        return qs.annotate(rank_bucket=Value(0, output_field=IntegerField()))
    return search(qs, kind, q).annotate(
        rank_bucket=Case(
            When(name__iexact=q, then=Value(0)),
            When(name__istartswith=q, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    )


def apply_derived_filters(qs, f, today, latest_for):
    """gap and review_state, resolved over every matching row (not one page) into an ID filter.

    gap uses ownership_gaps (missing owner/team, inactive owner), the same notion Needs attention uses.
    latest_for(ids) returns {resource_id: latest RecordReview}."""
    if "gap" not in f and "review_state" not in f:
        return qs
    rows = list(qs.select_related("business_owner", "technical_owner"))
    latest = latest_for([r.id for r in rows]) if "review_state" in f else {}
    keep = []
    for r in rows:
        if "gap" in f and bool(ownership_gaps(r)) != (f["gap"] == "true"):
            continue
        if "review_state" in f and review_state(r, latest.get(r.id), today)[0] != f["review_state"]:
            continue
        keep.append(r.id)
    return qs.filter(id__in=keep)


def query_errors(filterset_class, params, allowed=(), organization=None):
    """{parameter: [messages]} for a query that the list using `filterset_class` would refuse (export, saved
    filters). Validation needs no request, only the organization (its custom field filters): only filtering does."""
    data = QueryDict(mutable=True)
    for name, value in params.items():
        data[name] = value
    filterset = filterset_class(data, queryset=Resource.objects.none(), organization=organization)
    return filter_errors(filterset, params, allowed)


def in_org_or_404(model, org, value):
    """A filter naming another organization's record answers 404, as if it didn't exist."""
    get_object_or_404(model, organization=org, id=value)
    return value


# ---- custom field filters (catalog/customfields.py) -------------------------------------------------------

CF_KEY = customfields.KEY


def _parsed(cf):
    """A parser that cleans a filter value as the field cleans a stored one, with the field's message."""

    def parse(value: str):
        try:
            return customfields.clean_value(cf, value)
        except customfields.Invalid as e:
            raise forms.ValidationError(e.message) from None

    return parse


def _any_of(name, values):
    match = Q()
    for value in values:
        match |= Q(**{f"{CF_KEY}__contains": {name: value}})
    return match


def custom_filters(cf) -> dict[str, ParamFilter]:
    """The cf_<name> filters of one field (catalog/customfields.FILTERS_DOC). Lookups go through expressions and
    containment, never through `custom_field_data__<name>` paths, so a field named like a lookup (contains, exact)
    can't change what a filter means; containment uses the GIN index."""
    name, param, alias = cf.name, customfields.column(cf), f"_cf_{cf.name}"
    T = CustomFieldType
    kind = cf.type
    found: dict[str, ParamFilter] = {}

    def exact(qs, _n, value):
        return qs.filter(**{f"{CF_KEY}__contains": {name: value}})

    def has(qs, _n, value):
        present = customfields.has_key(name)  # @? jsonpath: served by the GIN index, unlike ?
        return qs.filter(present) if value == "true" else qs.exclude(present)

    if kind in (T.TEXT, T.LONGTEXT, T.URL):

        def contains(qs, _n, value):
            return qs.alias(**{alias: KeyTextTransform(name, CF_KEY)}).filter(**{f"{alias}__icontains": value})

        found[param] = ParamFilter(parse=stripped, method=contains, label=cf.label)
    elif kind == T.SELECT:
        found[param] = ParamFilter(
            parse=choices_csv(cf.choices), method=lambda qs, _n, v: qs.filter(_any_of(name, v)), label=cf.label
        )
    elif kind == T.MULTISELECT:
        found[param] = ParamFilter(
            parse=choices_csv(cf.choices),
            method=lambda qs, _n, v: qs.filter(_any_of(name, [[c] for c in v])),
            label=cf.label,
        )
    elif kind == T.BOOLEAN:
        found[param] = ParamFilter(
            parse=choice(TRUE_FALSE), method=lambda qs, _n, v: exact(qs, _n, v == "true"), label=cf.label
        )
    elif kind in (T.INTEGER, T.DECIMAL, T.DATE):
        # Numbers compare as JSON numbers; dates as their YYYY-MM-DD text in byte order. No cast, so another
        # organization's values under the same name can never make the query fail.
        def bound(lookup):
            def run(qs, _n, value):
                expression: Any
                if kind == T.DATE:
                    expression = Collate(KeyTextTransform(name, CF_KEY), "C")
                else:
                    expression = KeyTransform(name, CF_KEY)
                return qs.alias(**{alias: expression}).filter(**{f"{alias}__{lookup}": value})

            return run

        found[param] = ParamFilter(parse=_parsed(cf), method=exact, label=cf.label)
        found[f"{param}__gte"] = ParamFilter(parse=_parsed(cf), method=bound("gte"), label=f"{cf.label} from")
        found[f"{param}__lte"] = ParamFilter(parse=_parsed(cf), method=bound("lte"), label=f"{cf.label} to")
    found[f"{param}__set"] = ParamFilter(parse=choice(TRUE_FALSE), method=has, label=f"{cf.label} is set")
    return found


class CustomFieldFilters(TenantFilterSet):
    """A FilterSet that adds the cf_<name> filters of the organization's custom fields for `custom_kind` (a record
    kind, or vendor) to its declared ones. The organization comes from the request, or is passed in when a query is
    only validated (saved filters). The fields are read once per request (catalog/customfields.definitions)."""

    custom_kind: str = ""

    def __init__(self, data=None, queryset=None, *, request=None, prefix=None, organization=None):
        super().__init__(data, queryset, request=request, prefix=prefix)
        if organization is None and request is not None:
            organization = self.membership.organization
        if organization is None:
            return
        for cf in customfields.for_kind(organization, self.custom_kind):
            for param, flt in custom_filters(cf).items():
                flt.parent = self
                self.filters[param] = flt


class VendorFilterSet(CustomFieldFilters):
    """Vendors take their custom field filters only."""

    custom_kind = "vendor"

    class Meta:
        model = Vendor
        fields: list[str] = []


class CustomFieldDefinitionFilterSet(TenantFilterSet):
    """Custom field definitions by the kind they apply to and by type."""

    applies_to = ParamFilter(
        parse=choice(CUSTOM_FIELD_KINDS),
        method="filter_applies_to",
        help_text="Only the fields that apply to this kind: application, component, integration, ai_agent, vendor, "
        "contract or license_pool.",
    )
    type = ParamFilter(parse=choice(CustomFieldType.values), field_name="type", choices=CustomFieldType.choices)

    class Meta:
        model = CustomField
        fields: list[str] = []

    def filter_applies_to(self, qs, name, value):
        return qs.filter(applies_to__contains=[value])


class ResourceFilterSet(CustomFieldFilters):
    """What applications, components and integrations share. `kind` is set by the subclass, and is also the kind
    whose custom field filters it takes."""

    kind: str = ""

    lifecycle = ParamFilter(
        parse=choices_csv(Lifecycle.values),
        field_name="lifecycle",
        lookup_expr="in",
        help_text="Comma-separated lifecycle values, e.g. active,deprecated.",
        choices=Lifecycle.choices,
    )
    support_team = ParamFilter(parse=uuid_value, field_name="support_team_id", help_text="Team ID.")
    it_steward = ParamFilter(parse=user_id, field_name="it_steward_id", help_text="User ID of the IT steward.")
    archived = ParamFilter(
        parse=choice(("true", "false", "all")),
        method="derived",
        help_text="Default false (unarchived only); true for archived only; all for both.",
    )
    q = ParamFilter(parse=stripped, method="derived", help_text="Search name, description and purpose.")

    class Meta:
        model = Resource
        fields: list[str] = []

    def __init__(self, *args, **kwargs):
        self.custom_kind = self.kind
        super().__init__(*args, **kwargs)

    def derived(self, qs, name, value):
        """Applied in filter_queryset after every column filter (archived default, search ranking, derived states)."""
        return qs

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        data = self.form.cleaned_data
        archived = data.get("archived") or "false"
        if archived != "all":
            qs = qs.filter(archived_at__isnull=archived == "false")
        return ranked(qs, self.kind, data.get("q"))


class RecordFilterSet(ResourceFilterSet):
    """Applications and components."""

    criticality = ParamFilter(parse=choice(Criticality.values), field_name="criticality", choices=Criticality.choices)
    owner = ParamFilter(parse=uuid_value, method="filter_owner", help_text="Person ID as business or technical owner.")
    gap = ParamFilter(
        parse=choice(TRUE_FALSE),
        method="derived",
        help_text="true: only records with an ownership gap (missing owner or team, inactive owner).",
    )
    review_state = ParamFilter(parse=choice(REVIEW_STATES), method="derived", choices=[(s, s) for s in REVIEW_STATES])
    imported = ParamFilter(
        parse=choice(TRUE_FALSE), method="filter_imported", help_text="true: created by an import run."
    )
    tag = ParamFilter(parse=text, method="filter_tag", help_text="A tag name (case-insensitive).")
    department = ParamFilter(parse=uuid_value, field_name="department_id", help_text="Department ID.")
    data_classification = ParamFilter(
        parse=choices_csv(DataClassification.values),
        field_name="data_classification",
        lookup_expr="in",
        help_text="Comma-separated data classifications.",
        choices=DataClassification.choices,
    )
    # The compliance profile: refused with a field error while the organization has it off (catalog/compliance.py).
    is_gxp = ParamFilter(
        parse=choice(TRUE_FALSE), method="filter_compliance", help_text="Compliance profile only. true or false."
    )
    validation_status = ParamFilter(
        parse=choices_csv(ValidationStatus.values),
        method="filter_compliance",
        help_text="Compliance profile only. Comma-separated validation statuses.",
        choices=ValidationStatus.choices,
    )
    risk_tier = ParamFilter(
        parse=choices_csv(RiskTier.values),
        method="filter_compliance",
        help_text="Compliance profile only. Comma-separated risk tiers.",
        choices=RiskTier.choices,
    )
    periodic_review_due = ParamFilter(
        parse=choice(TRUE_FALSE),
        method="filter_compliance",
        help_text="Compliance profile only. true: the next periodic review is today or past.",
    )
    # Data quality (catalog/quality.py): the completeness score, 0 to 100.
    completeness__lt = ParamFilter(
        parse=percent,
        method="filter_completeness",
        help_text="Completeness score below this (0 to 100): records with failing data-quality checks.",
    )
    completeness__gte = ParamFilter(
        parse=percent, method="filter_completeness", help_text="Completeness score of at least this (0 to 100)."
    )
    quality_rule = ParamFilter(
        parse=text,
        method="filter_quality_rule",
        help_text="A data-quality rule id: records failing it (after exceptions).",
    )

    def filter_completeness(self, qs, name, value):
        return qs.filter(quality.score_q(self.membership.organization, name.split("__")[1], value))

    def filter_quality_rule(self, qs, name, value):
        org = self.membership.organization
        rule = quality.BY_ID.get(value)
        if rule is None or quality.status(rule, org)[0] != quality.ON:
            raise FieldErrors({name: ["Choose a data-quality rule that is on (GET /data-quality/rules)."]})
        return qs.filter(quality.failing_q(org, rule))

    def filter_compliance(self, qs, name, value):
        if not compliance.enabled(self.membership.organization):
            raise FieldErrors({name: [compliance.OFF]})
        if name == "is_gxp":
            return qs.filter(is_gxp=value == "true")
        if name == "periodic_review_due":
            due = Q(next_periodic_review__lte=org_today(self.membership.organization))
            return qs.filter(due) if value == "true" else qs.exclude(due)
        return qs.filter(**{f"{name}__in": value})

    def filter_owner(self, qs, name, value):
        return qs.filter(Q(business_owner_id=value) | Q(technical_owner_id=value))

    def filter_imported(self, qs, name, value):
        return qs.filter(created_by_import__isnull=value == "false")

    def filter_tag(self, qs, name, value):
        return qs.filter(Exists(ResourceTag.objects.filter(resource_id=OuterRef("pk"), tag__name__iexact=value)))

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        data = self.form.cleaned_data
        derived = {k: data[k] for k in ("gap", "review_state") if data.get(k)}
        if derived:
            org = self.membership.organization
            qs = apply_derived_filters(qs, derived, org_today(org), lambda ids: latest_reviews(org, ids))
        return qs


class ApplicationFilterSet(RecordFilterSet):
    kind = Kind.APPLICATION
    vendor = ParamFilter(parse=uuid_value, field_name="application__vendor_id", help_text="Vendor ID.")
    category = ParamFilter(parse=uuid_value, field_name="application__category_id", help_text="Software category ID.")
    deployment_model = ParamFilter(
        parse=choices_csv(DeploymentModel.values),
        field_name="application__deployment_model",
        lookup_expr="in",
        help_text="Comma-separated deployment models.",
        choices=DeploymentModel.choices,
    )
    authentication_method = ParamFilter(
        parse=choices_csv(AuthenticationMethod.values),
        field_name="application__authentication_method",
        lookup_expr="in",
        help_text="Comma-separated authentication methods.",
        choices=AuthenticationMethod.choices,
    )
    sso_enabled = ParamFilter(
        parse=choice((*TRUE_FALSE, "unknown")),
        method="filter_yes_no",
        help_text="true, false, or unknown (not recorded).",
    )
    scim_provisioning = ParamFilter(
        parse=choice((*TRUE_FALSE, "unknown")),
        method="filter_yes_no",
        help_text="true, false, or unknown (not recorded).",
    )

    def filter_yes_no(self, qs, name, value):
        column = f"application__{name}"
        if value == "unknown":
            return qs.filter(**{f"{column}__isnull": True})
        return qs.filter(**{column: value == "true"})


class ComponentFilterSet(RecordFilterSet):
    kind = Kind.COMPONENT
    component_type = ParamFilter(
        parse=choice(ComponentType.values), field_name="component__component_type", choices=ComponentType.choices
    )
    hosting = ParamFilter(
        parse=choices_csv(Hosting.values),
        field_name="component__hosting",
        lookup_expr="in",
        help_text="Comma-separated hosting values.",
        choices=Hosting.choices,
    )
    environment = ParamFilter(
        parse=choices_csv(Environment.values),
        field_name="component__environment",
        lookup_expr="in",
        help_text="Comma-separated environments.",
        choices=Environment.choices,
    )


YES_NO_UNKNOWN = (*TRUE_FALSE, "unknown")


class AIAgentFilterSet(RecordFilterSet):
    """AI agents: the shared record filters plus the agent's own."""

    kind = Kind.AI_AGENT
    platform = ParamFilter(
        parse=choices_csv(AgentPlatform.values),
        field_name="ai_agent__platform",
        lookup_expr="in",
        help_text="Comma-separated platforms.",
        choices=AgentPlatform.choices,
    )
    channel = ParamFilter(
        parse=choices_csv(AgentChannel.values),
        field_name="ai_agent__channels",
        lookup_expr="overlap",
        help_text="Comma-separated channels: agents reachable on any of them.",
        choices=AgentChannel.choices,
    )
    risk_classification = ParamFilter(
        parse=choices_csv(AgentRisk.values),
        field_name="ai_agent__risk_classification",
        lookup_expr="in",
        help_text="Comma-separated risk classes.",
        choices=AgentRisk.choices,
    )
    auth_mode = ParamFilter(
        parse=choices_csv(AgentAuthMode.values),
        field_name="ai_agent__auth_mode",
        lookup_expr="in",
        help_text="Comma-separated authentication modes.",
        choices=AgentAuthMode.choices,
    )
    environment = ParamFilter(
        parse=choices_csv(Environment.values),
        field_name="ai_agent__environment",
        lookup_expr="in",
        help_text="Comma-separated environments.",
        choices=Environment.choices,
    )
    human_in_loop = ParamFilter(
        parse=choice(YES_NO_UNKNOWN),
        method="filter_yes_no",
        help_text="true, false, or unknown (no decision recorded).",
    )
    actions_can_write = ParamFilter(
        parse=choice(YES_NO_UNKNOWN), method="filter_yes_no", help_text="true, false, or unknown (not recorded)."
    )
    model = ParamFilter(parse=uuid_value, method="filter_model", help_text="AI model ID: agents that use it.")
    provider = ParamFilter(
        parse=uuid_value, method="filter_provider", help_text="AI provider ID: agents that use any of its models."
    )

    def filter_yes_no(self, qs, name, value):
        column = f"ai_agent__{name}"
        if value == "unknown":
            return qs.filter(**{f"{column}__isnull": True})
        return qs.filter(**{column: value == "true"})

    def filter_model(self, qs, name, value):
        # Another organization's model ID matches nothing here: the link rows are this organization's.
        return qs.filter(Exists(AgentModel.objects.filter(agent_id=OuterRef("pk"), model_id=value)))

    def filter_provider(self, qs, name, value):
        return qs.filter(Exists(AgentModel.objects.filter(agent_id=OuterRef("pk"), model__provider_id=value)))


class AIModelFilterSet(TenantFilterSet):
    """AI models: by provider, modality and hosting region, or a name search."""

    provider = ParamFilter(parse=uuid_value, field_name="provider_id", help_text="AI provider ID.")
    modality = ParamFilter(
        parse=choices_csv(ModelModality.values),
        field_name="modality",
        lookup_expr="in",
        help_text="Comma-separated modalities.",
        choices=ModelModality.choices,
    )
    hosting_region = ParamFilter(
        parse=stripped, field_name="hosting_region", lookup_expr="iexact", help_text="Exact region, ignoring case."
    )
    in_use = ParamFilter(
        parse=choice(TRUE_FALSE), method="filter_in_use", help_text="true: used by at least one agent; false: unused."
    )
    q = ParamFilter(parse=stripped, method="filter_q", help_text="Name or version contains this text.")

    class Meta:
        model = AIModel
        fields: list[str] = []

    def filter_in_use(self, qs, name, value):
        used = Exists(AgentModel.objects.filter(model_id=OuterRef("pk")))
        return qs.filter(used if value == "true" else ~used)

    def filter_q(self, qs, name, value):
        return qs.filter(Q(name__icontains=value) | Q(version__icontains=value))


def nonblank(value: str) -> str | None:
    return value.strip() or None


class IntegrationFilterSet(ResourceFilterSet):
    """Integrations have no criticality, ownership gap or record review, and only a technical
    owner."""

    kind = Kind.INTEGRATION
    owner = ParamFilter(
        parse=uuid_value, field_name="technical_owner_id", help_text="Person ID of the technical owner."
    )
    source = ParamFilter(parse=uuid_value, method="filter_end", help_text="Record ID at the source end.")
    target = ParamFilter(parse=uuid_value, method="filter_end", help_text="Record ID at the target end.")
    protocol = ParamFilter(parse=nonblank, method="filter_protocol", help_text="Exact protocol, ignoring case.")
    # Integration fields
    direction = ParamFilter(
        parse=choices_csv(IntegrationDirection.values),
        field_name="integration__direction",
        lookup_expr="in",
        help_text="Comma-separated directions.",
        choices=IntegrationDirection.choices,
    )
    auth_method = ParamFilter(
        parse=choices_csv(IntegrationAuthMethod.values),
        field_name="integration__auth_method",
        lookup_expr="in",
        help_text="Comma-separated authentication methods.",
        choices=IntegrationAuthMethod.choices,
    )
    data_classification = ParamFilter(
        parse=choices_csv(DataClassification.values),
        field_name="data_classification",
        lookup_expr="in",
        help_text="Comma-separated data classifications.",
        choices=DataClassification.choices,
    )
    data_category = ParamFilter(  # data-flow map
        parse=choices_csv(DataCategory.values),
        field_name="integration__data_categories",
        lookup_expr="overlap",
        help_text="Comma-separated data categories: integrations carrying any of them.",
        choices=DataCategory.choices,
    )
    implemented = ParamFilter(
        parse=choice(TRUE_FALSE),
        method="filter_implemented",
        help_text="true: a component implements it; false: vendor-native.",
    )

    def filter_end(self, qs, name, value):
        in_org_or_404(Resource, self.membership.organization, value)
        return qs.filter(**{f"integration__{name}_resource_id": value})

    def filter_protocol(self, qs, name, value):
        return qs.filter(integration__protocol__iexact=value)

    def filter_implemented(self, qs, name, value):
        # Vendor-native is derived: nothing implements it (any `implements` link, even to an archived component).
        implemented = Exists(
            Relationship.objects.filter(type__slug=RelationshipKind.IMPLEMENTS, target_id=OuterRef("pk"))
        )
        return qs.filter(implemented if value == "true" else ~implemented)


# ---- people, relationships, assessments ---------------------------------------------------------------------------


def blank_is_none(parse):
    """An empty value is no filter (relationships?type=, people?active=), as the function views treated it."""
    return lambda value: None if value == "" else parse(value)


def true_false(value: str) -> bool:
    return choice(TRUE_FALSE)(value) == "true"


PEOPLE_LIMIT_MAX = 50


def people_limit(value: str) -> int | None:
    if value == "":
        return None
    number = whole_number(value, 1, PEOPLE_LIMIT_MAX)
    if number is None:
        raise forms.ValidationError(f"Use a whole number from 1 to {PEOPLE_LIMIT_MAX}.")
    return number


class PersonFilterSet(TenantFilterSet):
    active = ParamFilter(
        parse=blank_is_none(true_false),
        field_name="active",
        help_text="true: active people only (owner pickers); false: inactive.",
    )
    q = ParamFilter(parse=stripped, method="filter_q", help_text="Name or email contains this text (case-insensitive).")
    # Declared last: django-filter applies filters in declaration order, so the limit counts the filtered people.
    limit = ParamFilter(
        parse=people_limit,
        method="filter_limit",
        help_text=f"At most this many people (1 to {PEOPLE_LIMIT_MAX}), the first by name; for search boxes.",
    )

    def filter_q(self, qs, name, value):
        return qs.filter(Q(name__icontains=value) | Q(email__icontains=value))

    def filter_limit(self, qs, name, value):
        first = qs.order_by("name", "id").values("id")[:value]
        return qs.filter(id__in=first)

    class Meta:
        model = Person
        fields: list[str] = []


def record_id(value: str) -> str | None:
    if value == "":
        return None
    try:
        return uuid_value(value)
    except forms.ValidationError:
        raise forms.ValidationError("Enter a record ID.") from None


def slugs_csv(value: str) -> list[str]:
    """Comma-separated relationship type slugs, deduplicated in order; the filter checks them against the organization."""
    wanted = [v.strip() for v in value.split(",")]
    if not all(v and len(v) <= 50 and v.replace("_", "").isalnum() and v == v.lower() for v in wanted):
        raise forms.ValidationError("Use one or more relationship type slugs, such as depends_on (comma-separated).")
    return list(dict.fromkeys(wanted))


RELATIONSHIP_LIMIT_MAX = 500


def relationship_limit(value: str) -> int:
    """`?limit=` on GET relationships: ASCII digits, 1 to RELATIONSHIP_LIMIT_MAX. Opts in to the `{results, count,
    has_more}` shape (catalog/viewsets.RelationshipViewSet.list)."""
    number = whole_number(value, 1, RELATIONSHIP_LIMIT_MAX)
    if number is None:
        raise forms.ValidationError(f"Use a whole number from 1 to {RELATIONSHIP_LIMIT_MAX}.")
    return number


class RelationshipFilterSet(TenantFilterSet):
    resource = ParamFilter(
        parse=record_id, method="filter_resource", help_text="Only links where this record is the source or the target."
    )
    type = ParamFilter(
        parse=blank_is_none(slugs_csv),
        method="filter_type",
        help_text="Comma-separated relationship type slugs (depends_on, implements, replaces, or a type an Admin "
        "added). A slug the organization doesn't have answers 400.",
    )
    limit = ParamFilter(
        parse=relationship_limit,
        method="filter_limit",
        help_text=f"At most this many links (1 to {RELATIONSHIP_LIMIT_MAX}), in the list's order. With limit the "
        "response is an object {results, count, has_more} (count: every link the filters match); without it, the "
        "plain array of every matching link.",
    )

    class Meta:
        model = Relationship
        fields: list[str] = []

    def filter_type(self, qs, name, value):
        org = self.membership.organization
        known = set(RelationshipType.objects.filter(organization=org, slug__in=value).values_list("slug", flat=True))
        unknown = [v for v in value if v not in known]
        if unknown:
            raise FieldErrors({"type": [f"No relationship type {', '.join(unknown)} in this organization."]})
        return qs.filter(type__slug__in=value)

    def filter_limit(self, qs, name, value):
        return qs  # applied by the view after ordering

    def filter_resource(self, qs, name, value):
        in_org_or_404(Resource, self.membership.organization, value)
        return qs.filter(Q(source_id=value) | Q(target_id=value))


def worded(parse, message):
    """A parser whose refusal is `message`; an empty value is no filter."""

    def run(value: str):
        if value == "":
            return None
        try:
            return parse(value)
        except (forms.ValidationError, ValueError):
            raise forms.ValidationError(message) from None

    return run


def _statuses(value: str) -> list[str]:
    wanted = value.split(",")
    if not set(wanted) <= set(AssessmentStatus.values):
        raise ValueError
    return wanted


def _one_of(values):
    def parse(value: str) -> str:
        if value not in values:
            raise ValueError
        return value

    return parse


def _digits(value: str) -> int:
    number = whole_number(value)
    if number is None:
        raise ValueError
    return number


class AssessmentFilterSet(TenantFilterSet):
    status = ParamFilter(
        parse=worded(_statuses, "Choose draft, in_review, ready, closed or canceled."),
        field_name="status",
        lookup_expr="in",
        help_text="Comma-separated: draft, in_review, ready, closed, canceled.",
    )
    change_type = ParamFilter(
        parse=worded(_one_of(ChangeType.values), "Choose change or retirement."),
        field_name="change_type",
        choices=ChangeType.choices,
    )
    stale = ParamFilter(
        parse=worded(_one_of(TRUE_FALSE), "Use true or false."),
        method="filter_stale",
        help_text="true: only assessments whose current snapshot is stale.",
    )
    owner = ParamFilter(parse=worded(_digits, "Enter a user ID."), field_name="owner_id", help_text="User ID.")
    target = ParamFilter(parse=worded(uuid_value, "Enter a record ID."), method="filter_target", help_text="Record ID.")
    q = ParamFilter(parse=lambda v: v or None, method="filter_q", help_text="Search title and target name.")

    class Meta:
        model = ChangeAssessment
        fields: list[str] = []

    def filter_stale(self, qs, name, value):
        stale = Exists(
            AssessmentSnapshot.objects.filter(assessment=OuterRef("pk"), is_current=True, stale_since__isnull=False)
        )
        return qs.filter(stale if value == "true" else ~stale)

    def filter_target(self, qs, name, value):
        in_org_or_404(Resource, self.membership.organization, value)
        return qs.filter(target_id=value)

    def filter_q(self, qs, name, value):
        return qs.filter(Q(title__icontains=value) | Q(target__name__icontains=value))
