"""Connections: Microsoft and Halo credentials that discovery connectors sign in with, and Okta
and Google Workspace. Routes in core/urls.py.

Roles: Admin only, reads included (a connection names a tenant, an app and an address an Admin chose). Halo also needs
the capability `discovery.halo`, Okta `discovery.okta` and Google Workspace `discovery.google` (403 problem naming
it). Every write goes through core/connections.py.
The secret is write-only: no response, ETag or audit event carries it. A test asks the provider for a token
in the request, outside any transaction, then records the outcome; a connector runs only while its connection's
`test_current` is true."""

from typing import Any, ClassVar

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action

from . import connections
from .capabilities import DISCOVERY_GOOGLE, DISCOVERY_HALO, DISCOVERY_OKTA
from .connection_serializers import (
    GoogleWorkspaceConnectionInputSerializer,
    GoogleWorkspaceConnectionSerializer,
    GoogleWorkspaceConnectionTestResponseSerializer,
    HaloConnectionInputSerializer,
    HaloConnectionSerializer,
    HaloConnectionTestResponseSerializer,
    MicrosoftConnectionInputSerializer,
    MicrosoftConnectionSerializer,
    MicrosoftConnectionTestResponseSerializer,
    OktaConnectionInputSerializer,
    OktaConnectionSerializer,
    OktaConnectionTestResponseSerializer,
)
from .drf import ADMIN, PROBLEMS, TenantViewSet, policy, revision

TEST_RESPONSES = {c: PROBLEMS[c] for c in (403, 404)}
DELETE_HELP = (
    "Refused with 409 while a connector uses the connection; the problem's `connectors` lists them ({id, name})."
)
TEST_HELP = (
    "Asks the provider for a token with the stored values, outside any transaction, and records the outcome in "
    "last_test. Success sets tested_revision to the current revision; a failure clears it. Microsoft: a "
    "client-credentials token from login.microsoftonline.com/{tenant}, classified by AADSTS code (wrong or expired "
    "secret, unknown app, unknown tenant, consent missing, ...); it needs no API permission and proves none. Halo: a "
    "client-credentials token from {base_url}/auth/token. Okta: a client-credentials token from "
    "{base_url}/oauth2/v1/token with a client assertion signed by the stored private key (wrong key or client, scope "
    "not granted, DPoP required). Google Workspace: a token from oauth2.googleapis.com acting as the administrator "
    "(domain-wide delegation missing, unknown administrator, deleted key). `result` is this call's outcome: the same as last_test, "
    "except problem `changed` when the connection was edited during the test, which records nothing."
)


class ConnectionViewSet(TenantViewSet):
    pagination_class = None
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)
    kind: ClassVar[connections.Kind]

    def base_queryset(self):
        return self.kind.model.objects.select_related("tested_by")

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        from discovery.models import Connector

        found: dict[Any, list[dict[str, str]]] = {}
        attr = f"{self.kind.key}_connection_id"
        for pk, name, conn in (
            Connector.objects.filter(**{f"{attr}__in": [r.id for r in rows]})
            .order_by("name")
            .values_list("id", "name", attr)
        ):
            found.setdefault(conn, []).append({"id": str(pk), "name": name})
        return {"connectors": found}

    def revision_of(self, obj) -> str:
        return revision(obj, skip=connections.SECRET_COLUMNS)

    def create_object(self, data):
        return connections.create_connection(self.organization, self.request.user, self.kind, data)

    def update_object(self, obj, data):
        return connections.update_connection(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        connections.delete_connection(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        conn = connections.run_test(self.organization, request.user, self.get_object())
        response = self.detail_response(self.fresh(conn))
        # This call's outcome: the same as last_test, except when the connection changed during the test (problem
        # "changed"), which leaves the stored last_test as it was.
        response.data = {**response.data, "result": conn.probe.as_dict()}
        return response


def _docs(label: str, test_serializer, input_serializer, extra: str = ""):
    return extend_schema_view(
        list=extend_schema(summary=f"{label}s (Admin){extra}"),
        create=extend_schema(summary=f"Add a {label.lower()} (Admin){extra}", request=input_serializer),
        retrieve=extend_schema(summary=f"Get a {label.lower()} (Admin)"),
        partial_update=extend_schema(
            summary=f"Change a {label.lower()} (Admin); send secret only to replace it",
            description="A change to a sign-in value or the secret raises the revision: test again before any "
            "connector runs.",
            request=input_serializer,
        ),
        destroy=extend_schema(summary=f"Delete a {label.lower()} (Admin)", description=DELETE_HELP),
        test=extend_schema(
            summary=f"Test a {label.lower()} (Admin)",
            description=TEST_HELP,
            request=None,
            responses={200: test_serializer, **TEST_RESPONSES},
        ),
    )


@_docs("Microsoft connection", MicrosoftConnectionTestResponseSerializer, MicrosoftConnectionInputSerializer)
class MicrosoftConnectionViewSet(ConnectionViewSet):
    role_policy = policy(ADMIN, ADMIN)
    kind = connections.MICROSOFT
    serializer_class = MicrosoftConnectionSerializer
    input_serializer_class = MicrosoftConnectionInputSerializer


@_docs(
    "Halo connection",
    HaloConnectionTestResponseSerializer,
    HaloConnectionInputSerializer,
    "; needs discovery.halo",
)
class HaloConnectionViewSet(ConnectionViewSet):
    role_policy = policy(ADMIN, ADMIN, capability=DISCOVERY_HALO)
    kind = connections.HALO
    serializer_class = HaloConnectionSerializer
    input_serializer_class = HaloConnectionInputSerializer


@_docs(
    "Okta connection",
    OktaConnectionTestResponseSerializer,
    OktaConnectionInputSerializer,
    "; needs discovery.okta",
)
class OktaConnectionViewSet(ConnectionViewSet):
    """An Okta API service app (core/okta.py). The test asks Okta's org authorization server for a token
    with a client assertion signed by the stored private key."""

    role_policy = policy(ADMIN, ADMIN, capability=DISCOVERY_OKTA)
    kind = connections.OKTA
    serializer_class = OktaConnectionSerializer
    input_serializer_class = OktaConnectionInputSerializer


@_docs(
    "Google Workspace connection",
    GoogleWorkspaceConnectionTestResponseSerializer,
    GoogleWorkspaceConnectionInputSerializer,
    "; needs discovery.google",
)
class GoogleWorkspaceConnectionViewSet(ConnectionViewSet):
    """A service account with domain-wide delegation (core/google_workspace.py). The test asks Google for a
    token acting as the administrator, with the Reports scope."""

    role_policy = policy(ADMIN, ADMIN, capability=DISCOVERY_GOOGLE)
    kind = connections.GOOGLE
    serializer_class = GoogleWorkspaceConnectionSerializer
    input_serializer_class = GoogleWorkspaceConnectionInputSerializer
