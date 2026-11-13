"""Data quality (catalog/quality.py): the rules, the organization's summary (the dashboard's Data quality
panel), the records failing a rule, and exceptions.

Routes:
- `GET /data-quality/rules`: every registered rule, its status here (on, off, unavailable) and, while on, its counts.
- `GET /data-quality/summary`: the compliance percentage overall, by kind and by rule.
- `GET /data-quality/failures?rule=&kind=&it_steward=`: live records failing a rule (or any), keyset pages.
- `/data-quality/exceptions` and `/data-quality/exceptions/{id}`: "not applicable" marks on one record and rule.

Roles: every role reads; Editors and Admins add, change and remove exceptions (as with a record's other child rows).
Turning a rule off is an Organization setting (Admin, PATCH /organization `data_quality_disabled_rules`). Every route
needs the `data_quality` capability. Exceptions on AI agents are hidden, and refused, without `ai_agents`."""

from datetime import date
from typing import Any

from django.db.models import Q
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.response import Response

from core.capabilities import AI_AGENTS, DATA_QUALITY, has_capability, require_capability
from core.dates import org_today
from core.drf import (
    EDITOR,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    choice,
    policy,
    refuse_unknown_params,
    stripped,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors

from . import quality
from .filters import TRUE_FALSE
from .models import Kind, QualityException, Resource
from .serializers import (
    QualityExceptionInputSerializer,
    QualityExceptionSerializer,
    QualityFailureSerializer,
    QualityRuleSerializer,
    QualitySummarySerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}


class RulesView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DATA_QUALITY)

    @extend_schema(
        summary="Data-quality rules",
        description="Every built-in rule, in a fixed order, with its status in this organization and, while it is "
        "on, how many live records (not archived, not retired) pass, fail and are excepted. Any role.",
        responses={200: QualityRuleSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(quality.rules_json(self.organization))


class SummaryView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DATA_QUALITY)

    @extend_schema(
        summary="Data-quality summary",
        description="The dashboard's Data quality panel: the compliance percentage over every live record and rule "
        "that is on, the records failing nothing, and the same by kind and by rule. Any role.",
        responses={200: QualitySummarySerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(quality.summary_json(self.organization))


def _steward(value: str) -> int | str:
    if value == "none":
        return value
    number = whole_number(value, 1)
    if number is None:
        raise FieldErrors({"it_steward": ["Enter a user ID, or none."]})
    return number


class FailureFilterSet(TenantFilterSet):
    rule = ParamFilter(parse=stripped, method="filter_rule", help_text="A rule id that is on; default: any rule.")
    kind = ParamFilter(
        parse=choice(quality.QUALITY_KINDS), field_name="kind", help_text="A record kind, or integration."
    )
    it_steward = ParamFilter(
        parse=_steward, method="filter_steward", help_text="User ID of the IT steward, or none for records without one."
    )

    class Meta:
        model = Resource
        fields: list[str] = []

    def filter_rule(self, qs, name, value):
        return qs  # applied in filter_queryset, so that "no rule" means any rule

    def filter_steward(self, qs, name, value):
        return qs.filter(it_steward__isnull=True) if value == "none" else qs.filter(it_steward_id=value)

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        org = self.membership.organization
        wanted = self.form.cleaned_data.get("rule")
        rule = None
        if wanted:
            rule = quality.BY_ID.get(wanted)
            if rule is None or quality.status(rule, org)[0] != quality.ON:
                raise FieldErrors({"rule": ["Choose a data-quality rule that is on (GET /data-quality/rules)."]})
        return qs.filter(quality.failing_records(org, rule))


@extend_schema_view(
    list=extend_schema(
        summary="Records failing a data-quality check",
        description="Live records (not archived, not retired) failing `rule`, or any rule that is on, after "
        "exceptions; each with its completeness score and failing checks. Any role.",
    )
)
class FailureViewSet(TenantViewSet):
    role_policy = policy(VIEWER)
    required_capability = DATA_QUALITY
    serializer_class = QualityFailureSerializer
    filterset_class = FailureFilterSet
    ordering_fields = ("name", "completeness")
    default_ordering = ("name",)

    def base_queryset(self):
        return Resource.objects.select_related("it_steward")

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        wanted = {p.strip().lstrip("-") for p in self.request.query_params.get("ordering", "").split(",")}
        return quality.annotate_score(qs, self.organization) if "completeness" in wanted else qs

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"quality": quality.evaluate(self.organization, [r.id for r in rows])}


# ---- exceptions ----------------------------------------------------------------------------------------------------


class ExceptionFilterSet(TenantFilterSet):
    rule = ParamFilter(parse=stripped, field_name="rule", help_text="A rule id.")
    record = ParamFilter(parse=uuid_value, field_name="resource_id", help_text="Record ID.")
    owner = ParamFilter(parse=uuid_value, field_name="owner_id", help_text="Person ID of the accountable owner.")
    active = ParamFilter(
        parse=choice(TRUE_FALSE), method="filter_active", help_text="true: not expired; false: expired only."
    )
    owner_active = ParamFilter(
        parse=choice(TRUE_FALSE),
        method="filter_owner_active",
        help_text="false: exceptions whose owner has left or was anonymized (they can't be extended until reassigned).",
    )

    class Meta:
        model = QualityException
        fields: list[str] = []

    def filter_owner_active(self, qs, name, value):
        return qs.filter(owner__active=value == "true")

    def filter_active(self, qs, name, value):
        current = Q(expires_on__gte=org_today(self.membership.organization))
        return qs.filter(current) if value == "true" else qs.exclude(current)


@extend_schema_view(
    list=extend_schema(summary="Data-quality exceptions", description="Soonest expiry first. Any role."),
    create=extend_schema(
        summary="Mark a data-quality check not applicable to a record (Editor, Admin)",
        description="Only exceptable rules that are on, for a record of a kind the rule checks. One per record and "
        "rule (409 names the existing one). A reason and an accountable person are required; the expiry is today to "
        "a year ahead (default a year). Audited on the record.",
    ),
    retrieve=extend_schema(summary="Get a data-quality exception"),
    partial_update=extend_schema(
        summary="Change an exception's reason, owner or expiry (Editor, Admin)",
        description="Extending the expiry brings back an exception that lapsed. The record and the rule never change.",
    ),
    destroy=extend_schema(summary="Remove a data-quality exception (Editor, Admin): the check counts again"),
)
class QualityExceptionViewSet(TenantViewSet):
    role_policy = policy(VIEWER, EDITOR)
    required_capability = DATA_QUALITY
    serializer_class = QualityExceptionSerializer
    input_serializer_class = QualityExceptionInputSerializer
    filterset_class = ExceptionFilterSet
    ordering_fields = ("expires_on", "created_at")
    default_ordering = ("expires_on",)

    def base_queryset(self):
        return QualityException.objects.select_related("resource", "owner__department", "created_by")

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False) or has_capability(self.organization, AI_AGENTS):
            return qs
        return qs.exclude(resource__kind=Kind.AI_AGENT)

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        today: date = org_today(self.organization)
        return {"today": today}

    def create_object(self, data):
        return quality.create_exception(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        if obj.resource.kind == Kind.AI_AGENT:
            require_capability(self.organization, AI_AGENTS)
        return quality.update_exception(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        quality.delete_exception(self.organization, self.request.user, obj)
