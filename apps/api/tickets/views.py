"""Ticket link and ticket connection endpoints (capability `ticket_links`).

Links, nested under an assessment or a renewal (every member reads; Editors and Admins write):

- `GET|POST /assessments/{id}/ticket-links`, `/renewals/{id}/ticket-links`: the links, and linking a pasted URL.
- `GET|PATCH|DELETE .../ticket-links/{link_id}`: GET and PATCH return the link's `ETag`; PATCH and DELETE take
  `If-Match` (412 when the link changed since, a status read included; 428 from a signed-in session without it while
  SOFTWARDEN_REQUIRE_IF_MATCH is on).
- `POST .../ticket-links/{link_id}/refresh`: read the ticket's status now through the system's connection.
- `POST .../ticket-links/create-ticket`: create a ticket in Jira, ServiceNow or Halo and link it; 409 unless the
  connection allows creating (off by default). Every attempt is audited.
- `GET /ticket-links/parse?url=`: the system and key a URL parses to (no network).
- `GET /ticket-links/systems`: whether each system's statuses are read and tickets can be created there (every member).

Connections (Admin only; a Halo one also needs discovery.halo): `GET|POST /ticket-connections`,
`GET|PATCH|DELETE /ticket-connections/{id}`, `POST /ticket-connections/{id}/test`. The secret is write-only.

ServiceNow and Halo are preview features (capability `preview`, tickets/services.SYSTEM_CAPABILITY): without it a
connection kept for one is listed with `available: false` and can only be deleted (every other route on it is a 403
naming the capability), creating one is a 403, and `systems` omits them. Their links stay, with a status set by
hand."""

from __future__ import annotations

from typing import ClassVar

from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import TICKET_LINKS
from core.drf import (
    ADMIN,
    EDITOR,
    IF_MATCH,
    PROBLEMS,
    VIEWER,
    RoleAPIView,
    TenantViewSet,
    etag,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
    set_location,
)
from core.problems import FieldErrors

from . import parsing, reads, services
from .models import TicketConnection, TicketLink
from .serializers import (
    CreateTicketInputSerializer,
    ParsedUrlSerializer,
    TicketConnectionInputSerializer,
    TicketConnectionSerializer,
    TicketConnectionTestResponseSerializer,
    TicketLinkInputSerializer,
    TicketLinkSerializer,
    TicketSystemSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
LINKS = policy(VIEWER, EDITOR, capability=TICKET_LINKS)


class _TargetView(RoleAPIView):
    role_policy = LINKS
    kind: ClassVar[str] = "assessment"

    def target(self, pk) -> services.Target:
        if self.kind == "assessment":
            return services.assessment_target(self.organization, pk)
        return services.renewal_target(self.organization, pk)


class TicketLinksView(_TargetView):
    @extend_schema(
        summary="Ticket links",
        description="Jira, ServiceNow, Halo and other tickets linked here, oldest first.",
        responses={200: TicketLinkSerializer(many=True), **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        target = self.target(pk)
        return Response([reads.link_json(link) for link in services.links_of(self.organization, target)])

    @extend_schema(
        summary="Link a ticket (Editor, Admin)",
        description="Paste the ticket's https address: the system and key are parsed from Jira (/browse/KEY), "
        "ServiceNow (a task number in the address) and Halo (/tickets?id=N) addresses, or give them. A link to the "
        "same address twice is refused. `status` sets the status by hand.",
        request=TicketLinkInputSerializer,
        responses={201: TicketLinkSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        link = services.create_link(self.organization, request.user, self.target(pk), self.payload())
        response = Response(reads.link_json(link), status=201)
        set_location(request, response, f"{request.path_info.rstrip('/')}/{link.pk}")
        return response


class TicketLinkView(_TargetView):
    def respond(self, link: TicketLink) -> Response:
        response = Response(reads.link_json(link))
        response["ETag"] = etag(revision(link))
        return response

    def check_if_match(self, link: TicketLink) -> None:
        """If-Match against the link's stored revision, under its row lock (held until the write commits)."""
        locked = TicketLink.objects.select_for_update().filter(organization=self.organization, pk=link.pk).first()
        require_match(self.request, revision(locked or link))

    @extend_schema(summary="A ticket link", responses={200: TicketLinkSerializer, **ERRORS})
    def get(self, request, pk, link_pk):
        refuse_unknown_params(request)
        return self.respond(services.get_link(self.organization, self.target(pk), link_pk))

    @extend_schema(
        summary="Change a ticket link (Editor, Admin)",
        description="A new url is parsed again unless system or external_key are sent with it. Setting status "
        "makes it a manual status until the next read.",
        parameters=[IF_MATCH],
        request=TicketLinkInputSerializer,
        responses={200: TicketLinkSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    )
    def patch(self, request, pk, link_pk):
        target = self.target(pk)
        data = self.payload()
        with transaction.atomic():
            link = services.get_link(self.organization, target, link_pk)
            self.check_if_match(link)
            link = services.update_link(self.organization, request.user, target, link, data)
        return self.respond(services.get_link(self.organization, target, link.pk))

    @extend_schema(
        summary="Remove a ticket link (Editor, Admin)",
        parameters=[IF_MATCH],
        responses={204: None, **ERRORS, 412: PROBLEMS[412]},
    )
    def delete(self, request, pk, link_pk):
        target = self.target(pk)
        with transaction.atomic():
            link = services.get_link(self.organization, target, link_pk)
            self.check_if_match(link)
            services.delete_link(self.organization, request.user, link)
        return Response(status=204)


class TicketLinkRefreshView(_TargetView):
    @extend_schema(
        summary="Read a ticket's status now (Editor, Admin)",
        description="Reads the ticket by its key through the system's ticket connection (the connection's own "
        "address; never the link's URL). A failed read is recorded in status_error and the link is returned. 409 "
        "when the system has no usable connection, or the link is an `other` ticket.",
        request=None,
        responses={200: TicketLinkSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk, link_pk):
        target = self.target(pk)
        link = services.refresh_link(
            self.organization, request.user, services.get_link(self.organization, target, link_pk)
        )
        return Response(reads.link_json(services.get_link(self.organization, target, link.pk)))


class CreateTicketView(_TargetView):
    @extend_schema(
        summary="Create a ticket and link it (Editor, Admin)",
        description="Creates a ticket through the system's ticket connection and links it. Refused (409) unless an "
        "Admin turned on creating for that connection (off by default) and the connection passed its test. Sends the "
        "title and description only. Every attempt is audited; a failed one answers 409 with `problem` and says when "
        "the ticket may have been created anyway.",
        request=CreateTicketInputSerializer,
        responses={201: TicketLinkSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        link = services.create_ticket(self.organization, request.user, self.target(pk), self.payload())
        response = Response(reads.link_json(link), status=201)
        set_location(request, response, f"{request.path_info.removesuffix('/create-ticket')}/{link.pk}")
        return response


class ParseUrlView(RoleAPIView):
    role_policy = LINKS

    @extend_schema(
        summary="Which ticket an address is",
        description="The system and key a ticket URL parses to, for the link form's preview. No network.",
        parameters=[OpenApiParameter("url", str, required=True)],
        responses={200: ParsedUrlSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request, ("url",))
        try:
            parsed = services.parse_url(self.organization, request.query_params.get("url"))
        except parsing.InvalidUrl as e:
            raise FieldErrors({"url": [str(e)]}) from None
        return Response(
            {"system": parsed.system, "system_label": reads.SYSTEM_LABELS[parsed.system], "external_key": parsed.key}
        )


class SystemsView(RoleAPIView):
    role_policy = LINKS

    @extend_schema(
        summary="What each ticket system does here",
        description="One entry per connected system (Jira, ServiceNow, Halo): `reads` when its ticket connection is "
        "usable (on, with its secret, tested at its current revision), so linked tickets' statuses are read; "
        "`creates` when it also allows creating tickets (and Jira names a project). Every member reads it; it never "
        "carries a connection's settings.",
        responses={200: TicketSystemSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(services.systems(self.organization))


@extend_schema_view(
    list=extend_schema(
        summary="Ticket connections (Admin)",
        description="`available` false: a connection kept for a system this organization can't use now (ServiceNow "
        "and Halo need the preview capability). It is never read or written to and can only be deleted.",
    ),
    create=extend_schema(
        summary="Add a ticket connection (Admin)",
        description="One per system. Jira: base_url, username (the account email) and secret (an API token). "
        "ServiceNow: base_url, username and secret (the password). Halo: halo_connection (needs discovery.halo).",
        request=TicketConnectionInputSerializer,
    ),
    retrieve=extend_schema(summary="Get a ticket connection (Admin)"),
    partial_update=extend_schema(
        summary="Change a ticket connection (Admin); send secret only to replace it",
        description="A change to the address, user, Halo connection or secret raises the revision: test again "
        "before statuses are read.",
        request=TicketConnectionInputSerializer,
    ),
    destroy=extend_schema(
        summary="Delete a ticket connection (Admin)",
        description="Links stay; they become manual. Allowed on a connection that isn't `available` too.",
    ),
    test=extend_schema(
        summary="Test a ticket connection (Admin)",
        description="Signs in and makes one read (Jira: myself; ServiceNow: one task; Halo: a token with a ticket "
        "scope), outside any transaction, and records the outcome in last_test.",
        request=None,
        responses={200: TicketConnectionTestResponseSerializer, **{c: PROBLEMS[c] for c in (403, 404)}},
    ),
)
class TicketConnectionViewSet(TenantViewSet):
    role_policy = policy(ADMIN, ADMIN, capability=TICKET_LINKS)
    pagination_class = None
    serializer_class = TicketConnectionSerializer
    input_serializer_class = TicketConnectionInputSerializer
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return TicketConnection.objects.select_related("tested_by", "halo_connection")

    def page_context(self, rows):
        return {"available_systems": set(services.available_systems(self.organization))}

    def get_object(self):
        found = super().get_object()  # another organization's ID is a 404 first
        # A system the organization can't use (ServiceNow and Halo while preview features are off): a 403 naming the
        # capability, except deleting it, so a kept connection never blocks anything (its Halo connection's delete).
        if self.action != "destroy":
            services.require_system(self.organization, found.system)
        return found

    def revision_of(self, obj) -> str:
        return revision(obj, skip=services.SECRET_COLUMNS)

    def create_object(self, data):
        return services.create_connection(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return services.update_connection(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        services.delete_connection(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        conn = services.run_test(self.organization, request.user, self.get_object())
        response = self.detail_response(self.fresh(conn))
        response.data = {**response.data, "result": conn.probe.as_dict()}
        return response
