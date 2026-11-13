"""The full CRUD lifecycle of every model, through the API with an API token.

For every model a viewset writes, an Editor token and an Admin token create it, read it, find it in its list, change
it with PATCH and If-Match (the ETag moves), then archive and restore it and delete it, as far as the model allows and
the token's role may. An Editor token is refused (403 problem) where the model needs an Admin, and a read-only token is
refused every write with the read-only problem. Models a viewset exposes without the whole lifecycle are listed in
PARTIAL with the reason; a new viewset must join one table or the other (docs/API.md "What the API covers").

The organization is on the enterprise edition, so every capability-gated model (AI agents, attachments, Halo) is in."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from api import tokens
from catalog import ai, services
from catalog.models import SavedFilter
from core.capabilities import set_edition
from core.models import Membership
from tests import vendor_keys
from tests.test_drf_resources import viewset_routes

F = dict[str, Any]


@dataclass(frozen=True)
class Lifecycle:
    """How one viewset's model goes through create, read, list, update and delete or archive.

    `role` is the least role whose token may write it. `collection(f)` is the list and create path under /api/v1/,
    `body(f)` a valid create body; `detail(f, id)` defaults to `<collection>/<id>`. `patch` changes something, so the
    ETag must move; None: the model has no update (append-only evidence). `archive`: it has archive and restore.
    `delete`: the least role that may delete it, or None when it is never deleted (`end` names the closing action
    instead, as (path suffix, body), or `deactivate` as a closing PATCH)."""

    role: str
    collection: Callable[[F], str]
    body: Callable[[F], dict[str, Any]]
    patch: dict[str, Any] | None
    delete: str | None = "editor"
    archive: bool = False
    detail: Callable[[F, str], str] | None = None
    list_query: str = ""
    end: tuple[str, Callable[[F, dict[str, Any]], dict[str, Any]]] | None = None
    deactivate: dict[str, Any] | None = None  # a closing PATCH (with If-Match) for a model that is never deleted
    multipart: bool = False

    def detail_path(self, f: F, pk: str) -> str:
        return self.detail(f, pk) if self.detail else f"{self.collection(f)}/{pk}"


def ids(f: F, name: str) -> str:
    return str(f[name].id)


def const(path: str) -> Callable[[F], str]:
    return lambda f: path


def record_reviews(kind: str) -> Lifecycle:
    return Lifecycle(
        role="editor",
        collection=lambda f: f"{kind}/{ids(f, 'parent_' + kind)}/reviews",
        body=lambda f: {"confirmed_by": str(f["people"]["dana"].id), "evidence": "Confirmed by email"},
        patch=None,
        delete=None,
        detail=lambda f, pk: f"{kind}/{ids(f, 'parent_' + kind)}/reviews/{pk}",
    )


def link_confirmations(kind: str) -> Lifecycle:
    return Lifecycle(
        role="editor",
        collection=lambda f: f"{kind}/{ids(f, 'link_' + kind)}/confirmations",
        body=lambda f: {"confirmed_by": str(f["people"]["dana"].id), "evidence": "Confirmed by email"},
        patch=None,
        delete=None,
        detail=lambda f, pk: f"{kind}/{ids(f, 'link_' + kind)}/confirmations/{pk}",
    )


def attachments_of(kind: str) -> Lifecycle:
    """A link attachment on a parent, then edited and removed at attachments/<id> (AttachmentViewSet)."""
    return Lifecycle(
        role="editor",
        collection=lambda f: f"{kind}/{ids(f, 'parent_' + kind)}/attachments",
        body=lambda f: {"url": "https://example.test/runbook", "title": "Runbook", "category": "sop_runbook"},
        patch={"title": "Runbook, second edition"},
        detail=lambda f, pk: f"attachments/{pk}",
    )


IMPORT_CSV = b"name,lifecycle\nLifecycle import,active\n,active\n"  # row 3 has no name: an error to exclude

LIFECYCLE: dict[str, Lifecycle] = {
    # catalog records
    "ApplicationViewSet": Lifecycle(
        "editor",
        const("applications"),
        lambda f: {"name": "Lifecycle app", "lifecycle": "active"},
        {"description": "Changed through the API"},
        delete="admin",
        archive=True,
    ),
    "ComponentViewSet": Lifecycle(
        "editor",
        const("components"),
        lambda f: {"name": "Lifecycle job", "lifecycle": "active"},
        {"description": "Changed through the API"},
        delete="admin",
        archive=True,
    ),
    "AIAgentViewSet": Lifecycle(
        "editor",
        const("ai-agents"),
        lambda f: {"name": "Lifecycle agent", "lifecycle": "active", "models_used": [ids(f, "model")]},
        {"description": "Changed through the API"},
        delete="admin",
        archive=True,
    ),
    "IntegrationViewSet": Lifecycle(
        "editor",
        const("integrations"),
        lambda f: {"name": "Lifecycle feed", "lifecycle": "active", "source": ids(f, "app"), "target": ids(f, "job")},
        {"description": "Changed through the API"},
        delete="admin",
        archive=True,
    ),
    "RelationshipViewSet": Lifecycle(
        "editor",
        const("relationships"),
        lambda f: {"type": "depends_on", "source": ids(f, "job"), "target": ids(f, "app")},
        {"purpose": "Changed through the API"},
    ),
    "InstanceViewSet": Lifecycle(
        "editor",
        lambda f: f"applications/{ids(f, 'app')}/instances",
        lambda f: {"name": "Lifecycle prod"},
        {"url": "https://prod.example.test"},
        detail=lambda f, pk: f"applications/{ids(f, 'app')}/instances/{pk}",
    ),
    "ApplicationReviewViewSet": record_reviews("applications"),
    "ComponentReviewViewSet": record_reviews("components"),
    "AIAgentReviewViewSet": record_reviews("ai-agents"),
    "RelationshipConfirmationViewSet": link_confirmations("relationships"),
    "IntegrationConfirmationViewSet": link_confirmations("integrations"),
    "AssessmentViewSet": Lifecycle(
        "editor",
        const("assessments"),
        lambda f: {"target": ids(f, "app"), "change_type": "change", "title": "Lifecycle change"},
        {"title": "Lifecycle change, renamed"},
        delete=None,
        end=("status", lambda f, made: {"status": "cancelled"}),
    ),
    # directory
    "PersonViewSet": Lifecycle(
        "editor",
        const("people"),
        lambda f: {"name": "Lifecycle Person", "email": "lifecycle.person@example.test"},
        {"title": "Changed through the API"},
        delete=None,
        end=("anonymize", lambda f, made: {"confirm_name": made["name"]}),  # Admin only; editors deactivate
    ),
    "TeamViewSet": Lifecycle("editor", const("teams"), lambda f: {"name": "Lifecycle team"}, {"description": "Ops"}),
    "DepartmentViewSet": Lifecycle(
        "editor", const("departments"), lambda f: {"name": "Lifecycle department"}, {"name": "Lifecycle dept"}
    ),
    "VendorViewSet": Lifecycle(
        "editor", const("vendors"), lambda f: {"name": "Lifecycle vendor"}, {"notes": "Changed through the API"}
    ),
    "SoftwareCategoryViewSet": Lifecycle(
        "editor", const("categories"), lambda f: {"name": "Lifecycle category"}, {"description": "Changed"}
    ),
    "AIProviderViewSet": Lifecycle(
        "editor", const("ai-providers"), lambda f: {"name": "Lifecycle AI"}, {"website": "https://ai.example.test"}
    ),
    "AIModelViewSet": Lifecycle(
        "editor",
        const("ai-models"),
        lambda f: {"provider": ids(f, "provider"), "name": "Lifecycle model"},
        {"version": "2"},
    ),
    "SavedFilterViewSet": Lifecycle(
        "viewer",
        const("saved-filters"),
        lambda f: {"name": "Lifecycle view", "kind": "application", "query": {}},
        {"name": "Lifecycle view, renamed"},
        delete="viewer",
        list_query="?kind=application",
    ),
    # Admin settings of the catalog
    "CustomFieldViewSet": Lifecycle(
        "admin",
        const("custom-fields"),
        lambda f: {"name": "lifecycle_field", "label": "Lifecycle", "type": "text", "applies_to": ["application"]},
        {"label": "Lifecycle, relabelled"},
        delete="admin",
    ),
    "RelationshipTypeViewSet": Lifecycle(
        "admin",
        const("relationship-types"),
        lambda f: {"name": "Lifecycle type"},
        {"description": "Changed through the API"},
        delete="admin",
    ),
    # attachments (enterprise and community both have the capability)
    "ApplicationAttachmentViewSet": attachments_of("applications"),
    "ComponentAttachmentViewSet": attachments_of("components"),
    "AIAgentAttachmentViewSet": attachments_of("ai-agents"),
    "IntegrationAttachmentViewSet": attachments_of("integrations"),
    "VendorAttachmentViewSet": attachments_of("vendors"),
    "ContractAttachmentViewSet": attachments_of("contracts"),
    # Vendor risk documents: added on the vendor, then read, edited and removed by ID
    "VendorRiskDocumentsOfVendorViewSet": Lifecycle(
        "editor",
        lambda f: f"vendors/{ids(f, 'parent_vendors')}/risk-documents",
        lambda f: {"kind": "soc2", "issued_on": "2026-01-15", "validity_months": 12},
        {"title": "SOC 2 Type II bridge letter"},
        detail=lambda f, pk: f"vendor-risk-documents/{pk}",
    ),
    # data quality
    "QualityExceptionViewSet": Lifecycle(
        "editor",
        const("data-quality/exceptions"),
        lambda f: {
            "record": ids(f, "job"),
            "rule": "documentation",
            "reason": "Documented in the vendor's portal.",
            "owner": str(f["people"]["dana"].id),
        },
        {"reason": "Documented in the vendor's portal, section 4."},
    ),
    # money
    "RenewalViewSet": Lifecycle(
        "editor",
        const("renewals"),
        lambda f: {"name": "Lifecycle renewal", "auto_renew": "yes"},
        {"name": "Lifecycle renewal, renamed"},
        delete="admin",
        archive=True,
    ),
    "ContractViewSet": Lifecycle(
        "editor",
        const("contracts"),
        lambda f: {"name": "Lifecycle contract", "end_date": "2027-03-31"},
        {"po_number": "PO-1234"},
        delete="admin",
        archive=True,
    ),
    "ContractLineViewSet": Lifecycle(
        "editor",
        lambda f: f"contracts/{ids(f, 'parent_contracts')}/lines",
        lambda f: {"record": ids(f, "app"), "allocation_percent": "60"},
        {"allocation_percent": "40"},
        detail=lambda f, pk: f"contracts/{ids(f, 'parent_contracts')}/lines/{pk}",
    ),
    "LicensePoolViewSet": Lifecycle(
        "editor",
        const("license-pools"),
        lambda f: {"application": ids(f, "app"), "sku_name": "Lifecycle SKU"},
        {"seats_purchased": 25},
        delete="admin",
    ),
    # notifications
    "SnoozeViewSet": Lifecycle(
        "viewer",
        const("needs-attention/snoozes"),
        lambda f: {
            "subject_type": "ownership_gaps",
            "subject_id": ids(f, "app"),
            "kind": "snooze",
            "until": (services.org_today(f["org"]) + timedelta(days=3)).isoformat(),
        },
        None,  # a snooze is replaced or removed, not edited
        delete="viewer",
    ),
    "SavedViewSubscriptionViewSet": Lifecycle(
        "viewer",
        const("saved-view-subscriptions"),
        lambda f: {"saved_view": ids(f, "shared_view"), "weekday": 2},
        {"weekday": 3},
        delete="viewer",
    ),
    "TeamsDestinationViewSet": Lifecycle(
        "admin",
        const("notification-settings/teams-destinations"),
        lambda f: {"name": "Lifecycle Teams", "url": "https://example.test/flows/lifecycle?sig=x"},
        {"name": "Lifecycle Teams, renamed"},
        delete="admin",
    ),
    "WebhookDestinationViewSet": Lifecycle(
        "admin",
        const("notification-settings/webhook-destinations"),
        lambda f: {"name": "Lifecycle hook", "url": "https://example.test/lifecycle", "signing_secret": "s" * 32},
        {"enabled": False},
        delete="admin",
    ),
    "WebhookViewSet": Lifecycle(
        "admin",
        const("webhooks"),
        lambda f: {
            "name": "Lifecycle webhook",
            "url": "https://example.test/lifecycle-webhook",
            "signing_secret": "w" * 32,
            "events": ["record.*"],
        },
        {"events": ["record.updated", "reminder.*"]},
        delete="admin",
    ),
    "NotificationRuleViewSet": Lifecycle(
        "admin",
        const("notification-rules"),
        lambda f: {"name": "Lifecycle rule", "source_types": ["contract"], "lead_times_days": [30]},
        {"enabled": False},
        delete="admin",
    ),
    # connections, discovery and imports
    "MicrosoftConnectionViewSet": Lifecycle(
        "admin",
        const("connections/microsoft"),
        lambda f: {"name": "Lifecycle Entra", "tenant_id": "simulated", "secret": "synthetic-secret"},
        {"notes": "Changed through the API"},
        delete="admin",
    ),
    "HaloConnectionViewSet": Lifecycle(
        "admin",
        const("connections/halo"),
        lambda f: {
            "name": "Lifecycle Halo",
            "base_url": "https://halo.simulated.invalid",
            "client_id": "c",
            "secret": "synthetic-secret",
        },
        {"notes": "Changed through the API"},
        delete="admin",
    ),
    "TicketConnectionViewSet": Lifecycle(  # ticket links
        "admin",
        const("ticket-connections"),
        lambda f: {
            "system": "jira",
            "base_url": "https://jira.simulated.invalid",
            "username": "it@example.test",
            "secret": "synthetic-secret",
        },
        {"notes": "Changed through the API"},
        delete="admin",
    ),
    "OktaConnectionViewSet": Lifecycle(
        "admin",
        const("connections/okta"),
        lambda f: {
            "name": "Lifecycle Okta",
            "base_url": vendor_keys.OKTA_URL,
            "client_id": "c",
            "secret": vendor_keys.pem(),
        },
        {"notes": "Changed through the API"},
        delete="admin",
    ),
    "GoogleWorkspaceConnectionViewSet": Lifecycle(
        "admin",
        const("connections/google_workspace"),
        lambda f: {
            "name": "Lifecycle Workspace",
            "admin_email": vendor_keys.GOOGLE_ADMIN,
            "secret": vendor_keys.key_file(),
        },
        {"notes": "Changed through the API"},
        delete="admin",
    ),
    "ConnectorViewSet": Lifecycle(
        "admin",
        const("connectors"),
        lambda f: {"source": "entra", "name": "Lifecycle connector", "microsoft_connection": ids(f, "connection")},
        {"name": "Lifecycle connector, renamed"},
        delete="admin",
    ),
    "ImportSourceViewSet": Lifecycle(
        "admin",
        const("import-sources"),
        lambda f: {"name": "Lifecycle HR", "kind": "application"},
        {"name": "Lifecycle HR export"},
        delete="admin",
    ),
    "ImportRunViewSet": Lifecycle(
        "editor",
        const("imports"),
        lambda f: {"kind": "application", "file": SimpleUploadedFile("lifecycle.csv", IMPORT_CSV)},
        {"exclude": [3]},
        delete=None,  # never deleted: the 30-day purge removes an import's data
        end=("cancel", lambda f, made: {}),
        multipart=True,
    ),
}

# Viewsets without the whole lifecycle, and why (docs/API.md "What the API covers").
PARTIAL: dict[str, str] = {
    "TagViewSet": "Tags are created and removed through a record's `tags`; list, retrieve and PATCH (colour, "
    "description) here.",
    "VendorRiskProfileViewSet": "One risk profile per vendor, keyed by the vendor: read and PATCH (the first PATCH "
    "creates it); it goes with the vendor, never on its own. test_a_vendor_risk_profile_with_a_token.",
    "VendorRiskDocumentViewSet": "A risk document is added on its vendor (VendorRiskDocumentsOfVendorViewSet above); "
    "this viewset lists every vendor's, reads, edits, deletes and exports them, covered by that lifecycle.",
    "AttachmentViewSet": "An attachment is created on its parent (*AttachmentViewSet above); this viewset reads, "
    "edits and deletes it, and is covered by those lifecycles.",
    "ApiTokenViewSet": "Tokens are managed from a signed-in session only, never with a token: create, list, "
    "retrieve and revoke; never edited or deleted, so the audit log keeps naming them.",
    "AuditEventViewSet": "The audit log is append-only: list and retrieve.",
    "AuditCsvExportViewSet": "A download of the audit log.",
    "AuditJsonlExportViewSet": "A download of the audit log (audit.export).",
    "ApplicationAuditViewSet": "A record's history: append-only, read.",
    "ComponentAuditViewSet": "A record's history: append-only, read.",
    "AIAgentAuditViewSet": "A record's history: append-only, read.",
    "IntegrationAuditViewSet": "A record's history: append-only, read.",
    "RenewalAuditViewSet": "A renewal's history: append-only, read.",
    "ContractAuditViewSet": "A contract's history: append-only, read.",
    "LicensePoolAuditViewSet": "A licence pool's history: append-only, read.",
    "ConnectorRunViewSet": "Runs are written by the worker (Run now queues one): read.",
    "RunHistoryViewSet": "Runs are written by the worker: read.",
    "ObservationViewSet": "Observations come from discovery; editors decide with link, create, dismiss, reopen, drift "
    "and missing actions, each an audited edit.",
    "DiscoveredCredentialViewSet": "Credentials come from discovery: read.",
    "ShadowItViewSet": "The shadow IT overview is written by its connector's runs: read and CSV export; an app is "
    "decided through its discovery candidate.",
    "SsoCoverageViewSet": "The SSO and MFA coverage report is computed from coverage connectors' runs and the records: "
    "read and CSV export; a record's own SSO and SCIM fields are edited on the record.",
    "RecordSourceViewSet": "Which discovery sources saw a record: read.",
    "ReminderViewSet": "Reminders are generated from dates; people snooze, acknowledge, reopen and resolve them.",
    "DigestViewSet": "Digests are built by the worker; an Admin retries a failed one.",
    "NotificationLogViewSet": "Deliveries are written by the worker; an Admin resends one.",
    "WebhookDeliveryViewSet": "Webhook deliveries are written by the worker; an Admin resends one.",
    "FailureViewSet": "Data-quality failures are computed from the records: read.",
    "UserViewSet": "Members join by accepting an invitation (InvitationViewSet; POST users makes one); here list, "
    "retrieve, PATCH role and active, remove, reset a password and unlink identities.",
    "InvitationViewSet": "Created, listed, read, resent and revoked; never edited, and kept once revoked or accepted so "
    "the audit log keeps naming them.",
    "PendingAccessViewSet": "Requests come from single sign-on; an Admin approves (with a role) or declines (deletes).",
    "OrgExportViewSet": "Asked for (POST) and built by the worker, then downloaded; never edited, and expires "
    "on its own after 7 days (tests/test_org_lifecycle.py).",
}

# One-per-organization (or per-user) settings: read, then PATCH with If-Match. {path: (least role, PATCH body)}.
SINGLETONS: dict[str, tuple[str, dict[str, Any]]] = {
    "organization": ("admin", {"review_interval_days": 120}),
    "auth/me/preferences": ("viewer", {"digest_enabled": False}),
    "notification-settings/email": (
        "admin",
        {"transport": "smtp", "smtp_host": "smtp.example.test", "from_address": "it@example.test"},
    ),
    "assistant/settings": ("admin", {"monthly_cap_usd": "15.00"}),
    "sso/settings": ("admin", {"provider": "okta"}),
}

ROLES = ("viewer", "editor", "admin")
LEVEL = {role: i for i, role in enumerate(ROLES)}


def test_every_viewset_is_in_the_lifecycle_or_has_a_reason():
    names = {cls.__name__ for _, cls, _ in viewset_routes()}
    assert not set(LIFECYCLE) & set(PARTIAL)
    assert names == set(LIFECYCLE) | set(PARTIAL), (
        "add the viewset to LIFECYCLE, or to PARTIAL with the reason it has no full lifecycle"
    )


# ---- the organization and its tokens -------------------------------------------------------------------------------


@pytest.fixture
def world(a):
    """The `a` organization on enterprise, with the records the bodies point at, made by an Editor."""
    a["org"], _ = set_edition(a["org"], "enterprise")
    org, editor = a["org"], a["users"]["editor"]
    from core import connections

    job = services.create_resource(
        org, editor, {"name": "Lifecycle target", "kind": "component", "lifecycle": "active"}
    )
    provider = ai.create_provider(org, editor, {"name": "Lifecycle provider"})
    model = ai.create_model(org, editor, {"provider": str(provider.id), "name": "Lifecycle base model"})
    agent = services.create_resource(
        org, editor, {"name": "Lifecycle parent agent", "kind": "ai_agent", "lifecycle": "active",
                      "models_used": [str(model.id)]}
    )  # fmt: skip
    feed = services.create_integration(
        org, editor, {"name": "Lifecycle parent feed", "lifecycle": "active", "source": str(a["app"].id),
                      "target": str(job.id)}
    )  # fmt: skip
    link = services.create_relationship(
        org, editor, {"type": "depends_on", "source": str(a["app"].id), "target": str(job.id)}
    )
    from catalog.models import Vendor
    from money import contracts

    vendor = Vendor.objects.create(organization=org, name="Lifecycle parent vendor")
    contract = contracts.create_contract(org, editor, {"name": "Lifecycle parent contract", "end_date": "2027-06-30"})
    connection = connections.create_connection(
        org,
        a["users"]["admin"],
        connections.MICROSOFT,
        {"name": "Lifecycle connection", "tenant_id": "simulated", "secret": "synthetic-secret"},
    )
    shared_view = SavedFilter.objects.create(
        organization=org, user=editor, shared=True, kind="application", name="Shared", query={}
    )
    a.update(
        shared_view=shared_view,
        job=job,
        provider=provider,
        model=model,
        connection=connection,
        link_relationships=link,
        link_integrations=feed,
        parent_applications=a["app"],
        parent_components=job,
        parent_integrations=feed,
        parent_vendors=vendor,
        parent_contracts=contract,
        **{"parent_ai-agents": agent},
    )
    return a


class TokenClient:
    """Bearer-token JSON requests, CSRF enforced (a token request carries no cookie)."""

    def __init__(self, f: F, role: str, read_only: bool = False):
        owner = Membership.objects.get(organization=f["org"], user=f["users"][role])
        expires = services.org_today(f["org"]) + timedelta(days=30)
        token, secret = tokens.create_token(f["org"], f["users"]["admin"], owner, f"{role} script", read_only, expires)
        self.token = token
        self.client = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {secret}")

    def call(self, method: str, path: str, body: Any = None, multipart: bool = False, **headers: Any) -> Any:
        url = "/api/v1/" + path
        response: Any
        if method == "get":
            response = self.client.get(url, **headers)
        elif multipart:
            response = self.client.post(url, body or {}, **headers)
        else:
            data = json.dumps(body if body is not None else {})
            response = getattr(self.client, method)(url, data, content_type="application/json", **headers)
        response.body = response.json() if response.content and "json" in response["Content-Type"] else None
        return response


def problem(response, status: int, problem_type: str | None = None) -> dict[str, Any]:
    assert response.status_code == status, (response.status_code, response.body)
    assert response["Content-Type"] == "application/problem+json"
    body = response.body
    assert {"type", "title", "status", "detail"} <= set(body) and body["status"] == status, body
    if problem_type is not None:
        assert body["type"] == problem_type, body
    return body


def ok(response, status: int = 200) -> Any:
    body = response.body if isinstance(response.body, dict) else {}
    assert response.status_code == status, (response.status_code, body.get("detail"), body.get("errors"))
    return response.body


def listed(body: Any) -> list[dict[str, Any]]:
    return body["results"] if isinstance(body, dict) and "results" in body else body


def run_lifecycle(f: F, name: str, spec: Lifecycle, client: TokenClient, role: str) -> list[str]:
    """Create, read, list, update with If-Match, then archive and restore and delete as `role` may. Returns the steps
    done, for the assertions on coverage."""
    steps = []
    made = ok(client.call("post", spec.collection(f), spec.body(f), multipart=spec.multipart), 201)
    pk = str(made["id"])
    detail = spec.detail_path(f, pk)
    steps.append("create")

    read = client.call("get", detail)
    ok(read)
    assert str(read.body["id"]) == pk
    etag = read["ETag"]
    assert etag.startswith('"') and etag.endswith('"'), (name, etag)
    steps.append("read")

    rows = listed(ok(client.call("get", spec.collection(f) + spec.list_query)))
    assert pk in {str(row["id"]) for row in rows}, (name, "not in its list")
    steps.append("list")

    if spec.patch is not None:
        changed = client.call("patch", detail, spec.patch, HTTP_IF_MATCH=etag)
        ok(changed)
        assert changed["ETag"] != etag, (name, "PATCH didn't move the ETag")
        for key, value in spec.patch.items():
            if key in changed.body and not isinstance(value, list):
                assert str(changed.body[key]) in (str(value), f"{value}.00"), (name, key, changed.body[key])
        stale = client.call("patch", detail, spec.patch, HTTP_IF_MATCH=etag)  # the tag it changed from
        problem(stale, 412, "/problems/precondition-failed")
        etag = changed["ETag"]
        steps.append("update")

    if spec.archive:
        archived = ok(client.call("post", f"{detail}/archive"))
        assert archived.get("archived_at") or archived.get("archived") is True, name
        restored = ok(client.call("post", f"{detail}/restore"))
        assert not (restored.get("archived_at") or restored.get("archived")), name
        etag = client.call("get", detail)["ETag"]
        steps.append("archive")

    if spec.end is not None:
        suffix, end_body = spec.end
        closing = client.call("post", f"{detail}/{suffix}", end_body(f, made))
        if suffix == "anonymize" and LEVEL[role] < LEVEL["admin"]:
            problem(closing, 403, "/problems/forbidden")
        else:
            ok(closing)
            steps.append(suffix)

    if spec.deactivate is not None:
        closed = client.call("patch", detail, spec.deactivate, HTTP_IF_MATCH=etag)
        ok(closed)
        etag = closed["ETag"]
        steps.append("deactivate")

    if spec.delete is not None:
        removed = client.call("delete", detail, HTTP_IF_MATCH=etag)
        if LEVEL[role] >= LEVEL[spec.delete]:
            assert removed.status_code == 204, (name, removed.status_code, removed.body)
            problem(client.call("get", detail), 404, "/problems/not-found")
            steps.append("delete")
        else:
            problem(removed, 403, "/problems/forbidden")
            ok(client.call("get", detail))  # still there
    return steps


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
@pytest.mark.parametrize("role", ["editor", "admin"])
def test_every_model_goes_through_its_lifecycle_with_a_token(world, role):
    client = TokenClient(world, role)
    done: dict[str, list[str]] = {}
    for name, spec in LIFECYCLE.items():
        if LEVEL[role] < LEVEL[spec.role]:
            refused = client.call("post", spec.collection(world), spec.body(world), multipart=spec.multipart)
            problem(refused, 403, "/problems/forbidden")
            done[name] = ["refused"]
            continue
        done[name] = run_lifecycle(world, name, spec, client, role)
    if role == "admin":
        # Every model was created, read, listed and closed by an Admin token: deleted, archived or ended.
        assert all({"create", "read", "list"} <= set(steps) for steps in done.values()), done
        closed = {
            n for n, steps in done.items() if {"delete", "archive", "deactivate"} & set(steps) or LIFECYCLE[n].end
        }
        append_only = {n for n, spec in LIFECYCLE.items() if spec.patch is None and spec.delete is None}
        assert closed | append_only == set(LIFECYCLE), set(LIFECYCLE) - closed - append_only
    else:
        assert {n for n, steps in done.items() if steps == ["refused"]} == {
            n for n, spec in LIFECYCLE.items() if spec.role == "admin"
        }


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_a_read_only_token_reads_every_model_and_is_refused_every_write(world):
    reader = TokenClient(world, "admin", read_only=True)
    writer = TokenClient(world, "admin")
    for name, spec in LIFECYCLE.items():
        refused = reader.call("post", spec.collection(world), spec.body(world), multipart=spec.multipart)
        body = problem(refused, 403, "/problems/read-only-token")
        assert body["detail"] == "This token is read-only.", name
        made = ok(writer.call("post", spec.collection(world), spec.body(world), multipart=spec.multipart), 201)
        detail = spec.detail_path(world, str(made["id"]))
        etag = ok(reader.call("get", detail)) and reader.call("get", detail)["ETag"]
        assert ok(reader.call("get", spec.collection(world) + spec.list_query)) is not None, name
        if spec.patch is not None:
            problem(reader.call("patch", detail, spec.patch, HTTP_IF_MATCH=etag), 403, "/problems/read-only-token")
        if spec.delete is not None:
            problem(reader.call("delete", detail, HTTP_IF_MATCH=etag), 403, "/problems/read-only-token")
        ok(reader.call("get", detail))  # unchanged and still there


@pytest.mark.parametrize("path", list(SINGLETONS))
def test_settings_read_and_update_with_a_token(world, path):
    role, change = SINGLETONS[path]
    client = TokenClient(world, "admin")
    first = client.call("get", path)
    ok(first)
    headers = {"HTTP_IF_MATCH": first["ETag"]} if first.has_header("ETag") else {}
    changed = client.call("patch", path, change, **headers)
    ok(changed)
    if headers:
        assert changed["ETag"] != first["ETag"], path
        problem(client.call("patch", path, change, **headers), 412, "/problems/precondition-failed")
    if role == "admin":
        problem(TokenClient(world, "editor").call("patch", path, change), 403, "/problems/forbidden")
    problem(TokenClient(world, role, read_only=True).call("patch", path, change), 403, "/problems/read-only-token")


def test_a_vendor_risk_profile_with_a_token(world):
    """A vendor's risk profile is one per vendor, created by its first PATCH and never deleted on its own
    (it goes with the vendor). An Editor's token writes it with If-Match, also before it exists; a Viewer's and a
    read-only token can't."""
    path = f"vendors/{ids(world, 'parent_vendors')}/risk"
    client = TokenClient(world, "editor")
    first = client.call("get", path)
    assert ok(first)["id"] is None
    changed = client.call("patch", path, {"risk_tier": "high"}, HTTP_IF_MATCH=first["ETag"])
    assert ok(changed)["risk_tier"] == "high" and changed["ETag"] != first["ETag"]
    problem(client.call("patch", path, {"risk_tier": "low"}, HTTP_IF_MATCH=first["ETag"]), 412)
    problem(TokenClient(world, "viewer").call("patch", path, {"risk_tier": "low"}), 403, "/problems/forbidden")
    problem(
        TokenClient(world, "editor", read_only=True).call("patch", path, {"risk_tier": "low"}),
        403,
        "/problems/read-only-token",
    )


def test_calendar_feeds_are_managed_from_a_session_only(world):
    """The renewals calendar feed's secret is a credential of its member, like an API token: a token can't create,
    rotate or revoke one (tests/test_calendar_feed.py covers the session lifecycle)."""
    client = TokenClient(world, "viewer")
    for method, path in (("get", "calendar-feed"), ("post", "calendar-feed"), ("post", "calendar-feed/rotate")):
        body = problem(client.call(method, path), 403, "/problems/forbidden")
        assert body["detail"] == "Manage your calendar feed from a signed-in session, not with a token."


# ---- the write actions of the PARTIAL models, with a token -----------------------------------------------------------


def _observations(f: F) -> dict[str, Any]:
    """A connector run on the simulated tenant, so its observations wait in the queue."""
    from discovery.models import Observation
    from tests.test_drf_resources import _connector

    connector = _connector(f)
    return {o.display_name: o for o in Observation.objects.filter(connector=connector, kind="application")}


def gone(observation) -> None:
    from django.utils import timezone

    type(observation).objects.filter(pk=observation.pk).update(gone_at=timezone.now())


def action(client: TokenClient, reader: TokenClient, path: str, body: Any = None, **headers: Any) -> Any:
    """The read-only token is refused first (and changes nothing), then the writer's token succeeds (200, or 201
    when the action makes a record, 202 when it queues a delivery)."""
    problem(reader.call("post", path, body, **headers), 403, "/problems/read-only-token")
    done = client.call("post", path, body, **headers)
    return ok(done, done.status_code if done.status_code in (201, 202) else 200)


def test_discovery_decisions_work_with_a_token(world):
    from discovery import engine

    editor, reader = TokenClient(world, "editor"), TokenClient(world, "admin", read_only=True)
    seen = _observations(world)
    linked, dismissed, created, kept = (seen[n] for n in sorted(seen)[:4])
    base = "discovery/observations"

    action(editor, reader, f"{base}/{linked.id}/link", {"resource": ids(world, "app")})
    differences = ok(editor.call("get", f"{base}/{linked.id}/drift"))["differences"]
    values = {d["field"]: d["source"] for d in differences}
    first, *rest = list(values)
    action(editor, reader, f"{base}/{linked.id}/drift/ignore", {"fields": [first], "seen": {first: values[first]}})
    if rest:
        tag = editor.call("get", f"applications/{ids(world, 'app')}")["ETag"]
        body = {"fields": [rest[0]], "seen": {rest[0]: values[rest[0]]}}
        action(editor, reader, f"{base}/{linked.id}/drift/apply", body, HTTP_IF_MATCH=tag)

    action(editor, reader, f"{base}/{dismissed.id}/dismiss", {"reason": "Not ours: a test tenant."})
    action(editor, reader, f"{base}/{dismissed.id}/reopen", {})

    made = action(editor, reader, f"{base}/{created.id}/create", {})
    gone(created)
    action(editor, reader, f"{base}/{created.id}/missing/acknowledge", {"note": "Moved to another tenant."})

    _, record = engine.create_record(world["org"], world["users"]["editor"], kept, {})
    gone(kept)
    tag = editor.call("get", f"applications/{record.id}")["ETag"]
    action(editor, reader, f"{base}/{kept.id}/missing/archive", {}, HTTP_IF_MATCH=tag)
    assert made["decision"]


def test_reminder_actions_work_with_a_token(world):
    from tests.test_drf_resources import _reminder

    editor, reader = TokenClient(world, "editor"), TokenClient(world, "admin", read_only=True)
    reminder = _reminder(world)
    until = (services.org_today(world["org"]) + timedelta(days=3)).isoformat()
    assert action(editor, reader, f"reminders/{reminder.id}/snooze", {"until": until})["status"] == "snoozed"
    action(editor, reader, f"reminders/{reminder.id}/reopen", {})
    action(editor, reader, f"reminders/{reminder.id}/acknowledge", {})
    action(editor, reader, f"reminders/{reminder.id}/reopen", {})
    resolved = action(editor, reader, f"reminders/{reminder.id}/resolve", {"kind": "done", "note": "Reviewed."})
    assert resolved["status"] == "done"


def test_digest_retry_and_delivery_resend_work_with_an_admin_token(world, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True  # a retry needs a way to send
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    from notify.models import Delivery
    from tests.test_drf_resources import _delivery, _digest

    admin, reader = TokenClient(world, "admin"), TokenClient(world, "admin", read_only=True)
    digest = _digest(world)
    action(admin, reader, f"admin/digests/{digest.id}/retry", {})
    delivery = _delivery(world)
    Delivery.objects.filter(pk=delivery.pk).update(status="failed", last_error="Refused by the server.")
    action(admin, reader, f"notification-log/{delivery.id}/resend", {})
    problem(
        TokenClient(world, "editor").call("post", f"notification-log/{delivery.id}/resend"), 403, "/problems/forbidden"
    )


def test_a_tag_is_edited_with_a_token(world):
    from tests.test_drf_resources import _tag

    editor, reader = TokenClient(world, "editor"), TokenClient(world, "admin", read_only=True)
    pk = _tag(world)["pk"]
    tag = editor.call("get", f"tags/{pk}")["ETag"]
    change = {"description": "Finance systems"}
    problem(reader.call("patch", f"tags/{pk}", change, HTTP_IF_MATCH=tag), 403, "/problems/read-only-token")
    changed = editor.call("patch", f"tags/{pk}", change, HTTP_IF_MATCH=tag)
    assert ok(changed)["description"] == "Finance systems" and changed["ETag"] != tag


# ---- writes outside the viewsets -------------------------------------------------------------------------------------

# Every non-viewset write route (RoleAPIView and function views) and how a token uses it: "token: <test>" where a test
# here runs it with a token, otherwise why not. A new write route fails the guard until it is listed.
OTHER_WRITES: dict[tuple[str, str], str] = {
    ("post", "api/v1/auth/login"): "public: signs a session in; tokens don't sign in",
    ("post", "api/v1/auth/logout"): "session: ends the session",
    ("post", "api/v1/auth/password"): "session: the signed-in account's own password",
    ("post", "api/v1/auth/me/organization"): "session: switches the session's organization; a token has one",
    ("post", "api/v1/setup"): "public: first-run setup, before any token exists",
    ("post", "api/v1/auth/invitations/preview"): "public: the invitation page reads its invitation (CSRF, no token)",
    ("post", "api/v1/auth/invitations/accept"): "public: accepting an invitation; a token is refused (403), "
    "tests/test_invitations.py",
    ("patch", "api/v1/organization"): "token: test_settings_read_and_update_with_a_token",
    ("post", "api/v1/auth/signup"): "public: signup, before any account exists (CSRF, no token)",
    ("post", "api/v1/auth/signup/resend"): "public: a new signup confirmation link (CSRF, no token)",
    ("post", "api/v1/auth/signup/verify"): "public: confirming a signup signs a browser in; tokens don't sign in",
    ("post", "api/v1/auth/password-reset"): "public: asks for a reset link by email, before anyone signs in",
    ("post", "api/v1/auth/password-reset/confirm"): "public: sets a password from an emailed link; no token",
    (
        "post",
        "api/v1/organization/deletion",
    ): "session only: deleting the organization needs a re-authenticated person; "
    "a token is refused (403), tests/test_org_lifecycle.py",
    ("delete", "api/v1/organization/deletion"): "session only: as asking for it (403 for a token)",
    ("patch", "api/v1/auth/me/preferences"): "token: test_settings_read_and_update_with_a_token",
    ("patch", "api/v1/notification-settings/email"): "token: test_settings_read_and_update_with_a_token",
    ("delete", "api/v1/notification-settings/email"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/notification-settings/email/test-connection"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/notification-settings/email/test-send"): "token: test_other_writes_work_with_a_token",
    ("patch", "api/v1/assistant/settings"): "token: test_settings_read_and_update_with_a_token",
    ("post", "api/v1/assistant/batches"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/bulk-edit/preview"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/bulk-edit/apply"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/bulk-delete/preview"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/bulk-delete/apply"): "token: test_other_writes_work_with_a_token",
    ("post", "api/v1/catalog-code/plan"): "token: tests/test_catalog_code.py test_plan_and_apply_with_a_token",
    ("post", "api/v1/catalog-code/apply"): "token: tests/test_catalog_code.py test_plan_and_apply_with_a_token",
    ("post", "api/v1/people/<uuid:pk>/reassign"): "token: tests/test_offboarding.py test_reassign_with_a_token",
    ("post", "api/v1/calendar-feed"): "session only: test_calendar_feeds_are_managed_from_a_session_only",
    ("delete", "api/v1/calendar-feed"): "session only: test_calendar_feeds_are_managed_from_a_session_only",
    ("post", "api/v1/calendar-feed/rotate"): "session only: test_calendar_feeds_are_managed_from_a_session_only",
    ("patch", "api/v1/sso/settings"): "token: test_settings_read_and_update_with_a_token",
    ("post", "api/v1/sso/settings/test"): "session only: test_single_sign_on_with_a_token (a browser round trip)",
    ("post", "api/v1/sso/settings/activate"): "token: test_single_sign_on_with_a_token",
    ("post", "api/v1/sso/settings/disable"): "token: test_single_sign_on_with_a_token",
    ("post", "api/v1/sso/settings/domains/verify"): "token: test_verifying_a_sign_in_domain_with_a_token",
    ("post", "api/v1/platform/sign-in-domains/move"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/sign-in-domains/release"): "platform: session only, tokens refused",
    ("post", "api/v1/system/backup/acknowledge-key"): "token: test_backup_key_acknowledgement_with_a_token",
    # End-of-life tracking: one sub-resource per record or instance, not a viewset
    ("patch", "api/v1/applications/<uuid:pk>/end-of-life"): "token: test_end_of_life_tracking_with_a_token",
    ("delete", "api/v1/applications/<uuid:pk>/end-of-life"): "token: test_end_of_life_tracking_with_a_token",
    ("patch", "api/v1/components/<uuid:pk>/end-of-life"): "token: test_end_of_life_tracking_with_a_token",
    ("delete", "api/v1/components/<uuid:pk>/end-of-life"): "token: test_end_of_life_tracking_with_a_token",
    (
        "patch",
        "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life",
    ): "token: test_end_of_life_tracking_with_a_token",
    (
        "delete",
        "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life",
    ): "token: test_end_of_life_tracking_with_a_token",
    # Ticket links: nested under an assessment or a renewal, not a viewset
    ("post", "api/v1/assessments/<uuid:pk>/ticket-links"): "token: test_ticket_links_with_a_token",
    ("patch", "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>"): "token: test_ticket_links_with_a_token",
    ("delete", "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>"): "token: test_ticket_links_with_a_token",
    (
        "post",
        "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh",
    ): "token: test_ticket_links_with_a_token",
    ("post", "api/v1/assessments/<uuid:pk>/ticket-links/create-ticket"): "token: test_ticket_links_with_a_token",
    ("post", "api/v1/renewals/<uuid:pk>/ticket-links"): "token: test_ticket_links_with_a_token",
    ("patch", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>"): "token: test_ticket_links_with_a_token",
    ("delete", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>"): "token: test_ticket_links_with_a_token",
    ("post", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh"): "token: test_ticket_links_with_a_token",
    ("post", "api/v1/renewals/<uuid:pk>/ticket-links/create-ticket"): "token: test_ticket_links_with_a_token",
    # Platform admin console: platform staff sessions only, every API token refused
    # (tests/test_platform.py test_every_platform_route_needs_a_staff_session).
    ("post", "api/v1/platform/organizations/<uuid:pk>/edition"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/cancel-deletion"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/suspend"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/unsuspend"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/capability-overrides"): "platform: session only, tokens refused",
    (
        "post",
        "api/v1/platform/organizations/<uuid:pk>/capability-overrides/<uuid:override_pk>/remove",
    ): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/limits"): "platform: session only, tokens refused",
    (
        "post",
        "api/v1/platform/organizations/<uuid:pk>/limits/<str:name>/remove",
    ): "platform: session only, tokens refused",
    ("post", "api/v1/platform/organizations/<uuid:pk>/exports"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/system/backup/acknowledge-key"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/impersonation"): "platform: session only, tokens refused",
    ("delete", "api/v1/platform/impersonation"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/second-factor/enrollment"): "platform: session only, tokens refused",
    ("post", "api/v1/platform/second-factor/verify"): "platform: session only, tokens refused",
}


def test_backup_key_acknowledgement_with_a_token(world, settings, tmp_path):
    """An Admin token acknowledges the generated backup key (core/backup.py); a read-only or Editor token
    can't."""
    key = tmp_path / "backup_key"
    key.write_text("generated-backup-key-" + "x" * 40 + "\n", encoding="utf-8")
    settings.BACKUP_KEY_FILE, settings.BACKUP_KEY_FILE_CONFIGURED = str(key), False
    admin, reader = TokenClient(world, "admin"), TokenClient(world, "admin", read_only=True)
    problem(TokenClient(world, "editor").call("post", "system/backup/acknowledge-key"), 403, "/problems/forbidden")
    assert action(admin, reader, "system/backup/acknowledge-key")["key_acknowledged"] is True


def test_single_sign_on_with_a_token(world):
    """An Admin token activates a tested draft and turns single sign-on off; the test sign-in is a browser round trip
    and needs a signed-in session (tests/test_sso.py covers the flow)."""
    from core.models import SsoSettings
    from tests.sso_helpers import activate as set_up

    admin, reader = TokenClient(world, "admin"), TokenClient(world, "admin", read_only=True)
    row = set_up(world["org"], "okta")
    SsoSettings.objects.filter(pk=row.pk).update(draft_test_ok=True, draft_tested_revision=row.draft_revision)
    body = problem(admin.call("post", "sso/settings/test", {"draft_revision": row.draft_revision}), 403)
    assert body["detail"] == "Run a test sign-in from a signed-in browser session, not with a token."
    revision = {"draft_revision": row.draft_revision}
    assert action(admin, reader, "sso/settings/activate", revision)["status"] == "active"
    problem(TokenClient(world, "editor").call("post", "sso/settings/disable"), 403, "/problems/forbidden")
    assert action(admin, reader, "sso/settings/disable")["status"] == "disabled"


def test_verifying_a_sign_in_domain_with_a_token(world, settings):
    """An Admin token verifies a pending sign-in domain once its TXT record is published; a read-only or Editor
    token can't."""
    from core import dns_txt
    from tests.sso_helpers import activate as set_up

    settings.SOFTWARDEN_SSO_DOMAIN_VERIFICATION = True
    assert dns_txt.simulated()
    dns_txt.SIMULATED.clear()
    admin, reader = TokenClient(world, "admin"), TokenClient(world, "admin", read_only=True)
    set_up(world["org"], "okta")
    claimed = ok(admin.call("patch", "sso/settings", {"sign_in_domains": ["token-verify.example"]}))
    state = next(s for s in claimed["sign_in_domain_states"] if s["domain"] == "token-verify.example")
    assert state["status"] == "pending"
    dns_txt.SIMULATED[state["record_name"]] = [state["record_value"]]
    body = {"domain": "token-verify.example"}
    problem(TokenClient(world, "editor").call("post", "sso/settings/domains/verify", body), 403, "/problems/forbidden")
    try:
        assert action(admin, reader, "sso/settings/domains/verify", body)["status"] == "verified"
    finally:
        dns_txt.SIMULATED.clear()


def test_invitations_pending_access_and_members_with_a_token(world):
    """The write actions of the PARTIAL account viewsets: invite, resend, revoke, approve, decline, and a
    member's PATCH with If-Match."""
    from django.utils import timezone

    from core.models import PendingAccess

    admin, reader = TokenClient(world, "admin"), TokenClient(world, "admin", read_only=True)
    body = {"email": "by.token@orga.test", "role": "viewer"}
    problem(reader.call("post", "invitations", body), 403, "/problems/read-only-token")
    problem(TokenClient(world, "editor").call("post", "invitations", body), 403, "/problems/forbidden")
    made = ok(admin.call("post", "invitations", body), 201)
    assert "/invite#" in made["link"]
    assert action(admin, reader, f"invitations/{made['id']}/resend", {})["link"] != made["link"]
    assert action(admin, reader, f"invitations/{made['id']}/revoke", {})["status"] == "revoked"
    now = timezone.now()
    rows = [
        PendingAccess.objects.create(
            organization=world["org"], issuer="https://idp.example.test", subject=s, email=f"{s}@orga.test",
            first_seen=now, last_seen=now,
        )
        for s in ("approve", "decline")
    ]  # fmt: skip
    member = action(admin, reader, f"pending-access/{rows[0].id}/approve", {"role": "viewer"})
    problem(reader.call("delete", f"pending-access/{rows[1].id}"), 403, "/problems/read-only-token")
    assert admin.call("delete", f"pending-access/{rows[1].id}").status_code == 204
    detail = f"users/{member['id']}"
    tag = admin.call("get", detail)["ETag"]
    changed = admin.call("patch", detail, {"role": "editor"}, HTTP_IF_MATCH=tag)
    assert ok(changed)["role"] == "editor"
    problem(admin.call("patch", detail, {"role": "viewer"}, HTTP_IF_MATCH=tag), 412, "/problems/precondition-failed")
    assert ok(admin.call("patch", detail, {"active": False}, HTTP_IF_MATCH=changed["ETag"]))["active"] is False


def test_end_of_life_tracking_with_a_token(world):
    """Set, change and clear a record's and an instance's tracking with an Editor's token;
    a read-only token is refused each write and reads them."""
    editor = TokenClient(world, "editor")
    reader = TokenClient(world, "admin", read_only=True)
    job = services.create_resource(
        world["org"], world["users"]["editor"], {"name": "EOL job", "kind": "component", "lifecycle": "active"}
    )
    instance = services.create_instance(world["org"], world["users"]["editor"], world["app"], {"name": "EOL prod"})
    paths = [
        (f"applications/{ids(world, 'app')}/end-of-life", {"product": "postgresql", "version": "16.4"}),
        (f"applications/{ids(world, 'app')}/instances/{instance.id}/end-of-life", {"version": "13.2"}),
        (f"components/{job.id}/end-of-life", {"product": "postgresql", "cycle": "16"}),
    ]
    for path, body in paths:
        problem(reader.call("patch", path, body), 403, "/problems/read-only-token")
        assert ok(editor.call("patch", path, body))["product"] == "postgresql"
        assert ok(reader.call("get", path))["product"] == "postgresql"
    for path, _body in reversed(paths):
        problem(reader.call("delete", path), 403, "/problems/read-only-token")
        assert editor.call("delete", path).status_code == 204


def test_ticket_links_with_a_token(world):
    """Link, change, refresh, create-ticket and remove with an Editor's token on an
    assessment and a renewal; a read-only token reads them and is refused each write."""
    from catalog import assessments as assessments_service
    from money import renewals

    editor = TokenClient(world, "editor")
    reader = TokenClient(world, "admin", read_only=True)
    org, user = world["org"], world["users"]["editor"]
    assessment = assessments_service.create_assessment(
        org, user, {"target": ids(world, "app"), "change_type": "change", "title": "Token ticket"}
    )
    renewal = renewals.create_renewal(org, user, {"name": "Token renewal"})
    for base in (f"assessments/{assessment.id}/ticket-links", f"renewals/{renewal.id}/ticket-links"):
        body = {"url": "https://contoso.atlassian.net/browse/ITOPS-7"}
        problem(reader.call("post", base, body), 403, "/problems/read-only-token")
        made = ok(editor.call("post", base, body), 201)
        assert made["external_key"] == "ITOPS-7"
        link = f"{base}/{made['id']}"
        assert ok(reader.call("get", link))["id"] == made["id"]
        problem(reader.call("patch", link, {"status": "open"}), 403, "/problems/read-only-token")
        assert ok(editor.call("patch", link, {"status": "open"}))["status"] == "open"
        problem(reader.call("post", f"{link}/refresh"), 403, "/problems/read-only-token")
        assert editor.call("post", f"{link}/refresh").status_code == 409  # no Jira connection
        problem(reader.call("post", f"{base}/create-ticket", {"system": "jira"}), 403, "/problems/read-only-token")
        assert editor.call("post", f"{base}/create-ticket", {"system": "jira"}).status_code == 409  # turned off
        problem(reader.call("delete", link), 403, "/problems/read-only-token")
        assert editor.call("delete", link).status_code == 204


def test_every_write_outside_the_viewsets_is_covered_or_has_a_reason():
    from django.urls import get_resolver

    from core import drf
    from tests.test_url_coverage import methods_and_actions, walk

    found = set()
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if cls is None or issubclass(cls, drf.TenantViewSet) or not route.startswith("api/v1/"):
            continue
        found |= {(m, route) for m, _ in methods_and_actions(callback) if m not in ("get", "head", "options")}
    assert found == set(OTHER_WRITES), "list the new write route in OTHER_WRITES with how a token uses it"


def test_other_writes_work_with_a_token(world, settings, monkeypatch):
    editor, admin = TokenClient(world, "editor"), TokenClient(world, "admin")
    reader = TokenClient(world, "admin", read_only=True)
    bulk = {"kind": "application", "ids": [ids(world, "app")], "changes": {"add_tags": ["via-token"]}}
    action(editor, reader, "bulk-edit/preview", bulk)
    assert action(editor, reader, "bulk-edit/apply", bulk)
    spare = services.create_resource(
        world["org"], world["users"]["editor"], {"name": "Spare for bulk delete", "lifecycle": "active"}
    )
    doomed = {"kind": "application", "ids": [str(spare.id)]}
    action(admin, reader, "bulk-delete/preview", doomed)
    action(admin, reader, "bulk-delete/apply", doomed)
    problem(editor.call("post", "bulk-delete/apply", doomed), 403, "/problems/forbidden")

    # Email: test the connection and send a test through the in-memory backend, then remove the settings.
    from notify import channels

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    smtp = {"transport": "smtp", "smtp_host": "smtp.example.test", "from_address": "it@example.test"}
    ok(admin.call("patch", "notification-settings/email", smtp))
    monkeypatch.setattr(channels, "test_email_connection", lambda *a, **k: channels.TestResult(True, "Connected."))
    for path in ("notification-settings/email/test-connection", "notification-settings/email/test-send"):
        problem(reader.call("post", path), 403, "/problems/read-only-token")
        assert admin.call("post", path).status_code in (200, 202), path
    problem(reader.call("delete", "notification-settings/email"), 403, "/problems/read-only-token")
    assert admin.call("delete", "notification-settings/email").status_code in (200, 204)

    # An assistant batch: refused for a read-only token; with a key configured, an Editor's token queues one (202).
    # The batch is only queued here, so the Anthropic API is never called (tests/conftest.py no_anthropic_api).
    from discovery import assistant

    assistant.update_settings(
        world["org"], world["users"]["admin"], {"enabled": True, "api_key": "sk-ant-lifecycle-key-0001"}
    )
    problem(reader.call("post", "assistant/batches", {}), 403, "/problems/read-only-token")
    queued = editor.call("post", "assistant/batches", {})
    assert queued.status_code == 202, queued.body
    assert queued.body["status"] == "queued" and queued["Location"]
