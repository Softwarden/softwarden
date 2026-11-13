"""People & Teams viewsets: people, teams, departments and vendors, and the IT steward picker.
Lists are plain arrays ordered by name, the shape the SPA has always read.

Roles: Viewer reads; Editor creates and edits; anonymizing a Person is Admin-only. Teams, departments and vendors
are deleted by Editors and Admins, refused while anything uses them. Every write goes through catalog/services.py or
catalog/admin_services.py."""

from typing import Any, ClassVar
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import VENDOR_RISK, has_capability
from core.dates import org_today
from core.drf import (
    ADMIN,
    EDITOR,
    PROBLEMS,
    VIEWER,
    RoleAPIView,
    TenantViewSet,
    UserBriefSerializer,
    policy,
    refuse_unknown_params,
    revision,
)
from core.models import Membership
from money.models import RenewalObligation

from . import admin_services, customfields, services, vendor_risk
from .filters import PersonFilterSet, VendorFilterSet
from .models import (
    RECORD_KINDS,
    Department,
    Person,
    RecordReview,
    Resource,
    SoftwareCategory,
    Team,
    TeamMember,
    Vendor,
)
from .serializers import (
    AnonymizeInputSerializer,
    DepartmentInputSerializer,
    DepartmentSerializer,
    PersonDetailSerializer,
    PersonInputSerializer,
    PersonSerializer,
    SoftwareCategoryInputSerializer,
    SoftwareCategorySerializer,
    TeamInputSerializer,
    TeamSerializer,
    VendorDetailSerializer,
    VendorInputSerializer,
    VendorSerializer,
)


def responsible_counts(org):
    """{person_id: number of unarchived records they own}; one owner of both kinds counts the record once."""
    counts: dict = {}
    rows = Resource.objects.filter(organization=org, kind__in=RECORD_KINDS, archived_at__isnull=True).values_list(
        "business_owner_id", "technical_owner_id"
    )
    for business, technical in rows:
        for person_id in {business, technical} - {None}:
            counts[person_id] = counts.get(person_id, 0) + 1
    return counts


def responsibilities(org, person):
    """The records a Person owns or has confirmed, and the renewals they are responsible for."""

    def ref(r):
        return {"id": str(r.id), "kind": r.kind, "name": r.name, "archived": r.archived_at is not None}

    owned = Resource.objects.filter(organization=org).filter(Q(business_owner=person) | Q(technical_owner=person))
    reviews = RecordReview.objects.filter(organization=org, confirmed_by=person).select_related("resource")
    confirmed: dict[UUID, Resource] = {}
    for review in reviews:  # newest first, so the first review per record is its latest
        confirmed.setdefault(review.resource_id, review.resource)
    latest = reviews.first()
    return {
        "business_owner": [ref(r) for r in owned if r.business_owner_id == person.id],
        "technical_owner": [ref(r) for r in owned if r.technical_owner_id == person.id],
        "confirmed": [ref(r) for r in confirmed.values()],
        "review_count": reviews.count(),
        "latest_review": latest.confirmation_date.isoformat() if latest else None,
        "renewals": [
            {"id": str(o.id), "name": o.name, "archived": o.archived_at is not None}
            for o in RenewalObligation.objects.filter(organization=org, responsible=person).order_by("name")
        ],
    }


@extend_schema_view(
    list=extend_schema(summary="List people"),
    create=extend_schema(summary="Add a person (Editor, Admin)"),
    retrieve=extend_schema(summary="Get a person with what they own and have confirmed"),
    partial_update=extend_schema(summary="Update a person (Editor, Admin)"),
    anonymize=extend_schema(
        summary="Anonymize a person (Admin; can't be undone)",
        request=AnonymizeInputSerializer,
        responses={200: PersonSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
    ),
)
class PersonViewSet(TenantViewSet):
    """People who can own or confirm records. People are deactivated or anonymized, never deleted."""

    role_policy = policy(VIEWER, EDITOR, anonymize=ADMIN)
    pagination_class = None
    serializer_class = PersonSerializer
    detail_serializer_class = PersonDetailSerializer
    input_serializer_class = PersonInputSerializer
    filterset_class = PersonFilterSet
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return Person.objects.select_related("department", "user")

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        context: dict[str, Any] = {"counts": responsible_counts(self.organization)}
        if self.action in ("retrieve", "partial_update"):
            context["responsibilities"] = {p.id: responsibilities(self.organization, p) for p in rows}
        return context

    def create_object(self, data):
        return services.create_person(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return admin_services.update_person(self.organization, self.request.user, obj, data)

    @action(detail=True, methods=["post"])
    def anonymize(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            person = admin_services.anonymize_person(
                self.organization, request.user, self.get_object(), data.get("confirm_name")
            )
            return self.detail_response(self.fresh(person), serializer_class=PersonSerializer)


class NamedViewSet(TenantViewSet):
    """Teams, departments, vendors and software categories: a unique name and their own details, with how many rows
    use each."""

    model: ClassVar[Any]  # Team, Department, Vendor or SoftwareCategory
    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return self.model.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"usage": admin_services.usage_counts(self.model, self.organization)}

    def create_object(self, data):
        return admin_services.create_named(self.model, self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return admin_services.update_named(self.model, self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        admin_services.delete_named(self.model, self.organization, self.request.user, obj)


def _named_docs(noun: str, plural: str, list_description: str = ""):
    return extend_schema_view(
        list=extend_schema(summary=f"List {plural}", description=list_description),
        create=extend_schema(summary=f"Add a {noun} (Editor, Admin)"),
        retrieve=extend_schema(summary=f"Get a {noun}"),
        partial_update=extend_schema(summary=f"Update a {noun} (Editor, Admin)"),
        destroy=extend_schema(summary=f"Delete a {noun} (Editor, Admin; refused while anything uses it)"),
    )


def team_members(team_ids) -> dict[Any, list[Person]]:
    """{team_id: [Person]} by name, in one query, for a page of teams."""
    found: dict[Any, list[Person]] = {}
    links = (
        TeamMember.objects.filter(team_id__in=list(team_ids))
        .select_related("person__department")
        .order_by("person__name", "person_id")
    )
    for link in links:
        found.setdefault(link.team_id, []).append(link.person)
    return found


@_named_docs("team", "support teams")
class TeamViewSet(NamedViewSet):
    """Support teams with their optional members. A team is never an owner."""

    model = Team
    serializer_class = TeamSerializer
    input_serializer_class = TeamInputSerializer

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {**super().page_context(rows), "members": team_members(r.id for r in rows)}

    def revision_of(self, obj) -> str:
        members = sorted(str(p) for p in TeamMember.objects.filter(team=obj).values_list("person_id", flat=True))
        return revision(obj, extra={"members": members})


@_named_docs("department", "departments")
class DepartmentViewSet(NamedViewSet):
    model = Department
    serializer_class = DepartmentSerializer
    input_serializer_class = DepartmentInputSerializer


@_named_docs("vendor", "vendors", customfields.FILTERS_DOC)
class VendorViewSet(NamedViewSet):
    """Vendors with their support details, account manager and custom fields (`custom_field_data`; filters
    cf_<name>, as on the record lists)."""

    model = Vendor
    serializer_class = VendorSerializer
    detail_serializer_class = VendorDetailSerializer
    input_serializer_class = VendorInputSerializer
    filterset_class = VendorFilterSet

    def base_queryset(self):
        return Vendor.objects.select_related("account_manager__department")

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        context = {**super().page_context(rows), "custom_fields": customfields.for_kind(org, "vendor")}
        if self.action in ("retrieve", "partial_update") and has_capability(org, VENDOR_RISK):
            # The detail's risk summary: two queries for the one vendor.
            today, days = org_today(org), vendor_risk.horizon(org)
            context["risk"] = {
                vid: vendor_risk.summary(f, today, days) for vid, f in vendor_risk.files(org, rows).items()
            }
        return context


@_named_docs("software category", "software categories")
class SoftwareCategoryViewSet(NamedViewSet):
    """Categories for applications: seeded per organization, edited by Editors and Admins; one in use by
    an application can't be deleted."""

    model = SoftwareCategory
    serializer_class = SoftwareCategorySerializer
    input_serializer_class = SoftwareCategoryInputSerializer


class StewardsView(RoleAPIView):
    """The users who can be an IT steward: the organization's Admins and Editors."""

    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Admins and Editors, for the IT steward picker", responses={200: UserBriefSerializer(many=True)}
    )
    def get(self, request):
        refuse_unknown_params(request)
        users = [
            m.user
            for m in Membership.objects.filter(
                organization=self.organization, role__in=services.WRITE_ROLES
            ).select_related("user")
        ]
        return Response(UserBriefSerializer(users, many=True).data)
