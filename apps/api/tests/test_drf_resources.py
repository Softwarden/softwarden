"""Every viewset and composite view of the DRF layer ("DRF layer", parts A, B and C).

The scoping, read-only token and If-Match checks run over every TenantViewSet route found in the URL resolver, so a new
viewset is covered as soon as it is routed: it only needs an entry in FACTORIES (the test fails without one). The
composite responses built as plain data (assessments, renewals, Needs attention, the dependency explorer, ...) are
checked against the serializers that describe them in OpenAPI, key for key. Bulk edit's row locks are checked against
a concurrent If-Match writer."""

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client
from django.urls import get_resolver
from rest_framework import serializers

from api import tokens
from catalog import ai, assessments, attachments, bulk, customfields, quality, relationship_types, services
from catalog.models import (
    AIModel,
    AIProvider,
    ChangeAssessment,
    Department,
    Relationship,
    Resource,
    ResourceTag,
    SavedFilter,
    SoftwareCategory,
    Team,
    Vendor,
)
from catalog.serializers import (
    AssessmentListSerializer,
    AssessmentSerializer,
    BulkDeletePreviewSerializer,
    BulkDeleteRefusedRowSerializer,
    BulkDeleteResultSerializer,
    BulkPreviewSerializer,
    BulkRefusedRowSerializer,
    BulkResultSerializer,
    DependenciesSerializer,
    SavedFilterSerializer,
    SnapshotSerializer,
    SnapshotSummarySerializer,
)
from core import connections, drf
from core.models import AuditEvent, Membership
from discovery import engine as discovery
from discovery.models import ConnectorRun, Observation
from imports import importer
from money import contracts, renewals
from money.serializers import DeadlineSerializer, RenewalListSerializer, RenewalSerializer
from notify.models import NotificationSnooze
from notify.serializers import AttentionSerializer, PreferenceSerializer
from tests import vendor_keys
from tests.conftest import digest_row, make_org
from tests.test_tokens import Bearer, mint  # noqa: F401  (mint is a fixture)
from tests.test_url_coverage import walk

# ---- one object per viewset, in a given organization ---------------------------------------------------------------


def _component(f, name="Job"):
    return services.create_resource(
        f["org"], f["users"]["editor"], {"name": name, "kind": "component", "lifecycle": "active"}
    )


def _ai_model(f, name="Model"):
    """An AI model of one provider per organization fixture (provider names are unique)."""
    provider = AIProvider.objects.filter(organization=f["org"], name="Fabrikam AI").first()
    if provider is None:
        provider = ai.create_provider(f["org"], f["users"]["editor"], {"name": "Fabrikam AI"})
    found = AIModel.objects.filter(organization=f["org"], provider=provider, name=name).first()
    return found or ai.create_model(f["org"], f["users"]["editor"], {"provider": str(provider.id), "name": name})


def _agent(f, name="Helper"):
    return services.create_resource(
        f["org"],
        f["users"]["editor"],
        {"name": name, "kind": "ai_agent", "lifecycle": "active", "models_used": [str(_ai_model(f).id)]},
    )


def _integration(f):
    return services.create_integration(
        f["org"],
        f["users"]["editor"],
        {"name": "Feed", "lifecycle": "active", "source": str(f["app"].id), "target": str(_component(f).id)},
    )


def _relationship(f):
    return services.create_relationship(
        f["org"],
        f["users"]["editor"],
        {"type": "depends_on", "source": str(f["app"].id), "target": str(_component(f).id)},
    )


def _confirmed_relationship(f):
    rel = _relationship(f)
    done = services.confirm_link(
        f["org"], f["users"]["editor"], rel, {"confirmed_by": str(f["people"]["dana"].id), "evidence": "Email"}
    )
    return {"pk": rel.id, "confirmation_pk": done.id}


def _confirmed_integration(f):
    feed = _integration(f)
    done = services.confirm_link(
        f["org"],
        f["users"]["editor"],
        feed.integration,
        {"confirmed_by": str(f["people"]["dana"].id), "evidence": "Email"},
    )
    return {"pk": feed.id, "confirmation_pk": done.id}


def _review(f, record):
    review = services.record_review(
        f["org"], f["users"]["editor"], record, {"confirmed_by": str(f["people"]["dana"].id), "evidence": "Mail"}
    )
    return {"pk": record.id, "review_pk": review.id}


def _tag(f):
    services.update_resource(f["org"], f["users"]["editor"], f["app"], {"tags": ["finance"]})
    return {"pk": ResourceTag.objects.get(resource=f["app"]).tag_id}


def _assessment(f):
    held = ChangeAssessment.objects.create(
        organization=f["org"], target=f["app"], change_type="change", title="Held", owner=f["users"]["editor"]
    )
    snapshot = assessments.write_snapshot(f["org"], f["users"]["editor"], held)
    return {"pk": held.id, "snapshot_pk": snapshot.id}


def _renewal(f):
    obligation = renewals.create_renewal(f["org"], f["users"]["editor"], {"name": "Held", "auto_renew": "yes"})
    return {"pk": obligation.id, "cycle_pk": obligation.cycles.get().id}


def _snooze(f, user):
    today = services.org_today(f["org"])
    return NotificationSnooze.objects.create(
        organization=f["org"],
        user=user,
        subject_type="ownership_gaps",
        subject_id=str(f["app"].id),
        kind="snooze",
        until=today + timedelta(days=3),
    )


def _digest(f):
    return digest_row(f["org"], f["users"]["editor"], "failed")


def _saved_filter(f, user):
    return SavedFilter.objects.create(
        organization=f["org"], user=user, kind="application", name="Mine", query={}, created_by=user
    )


def _connector(f):
    """One connector per organization fixture (names are unique), run once so it has a run and observations."""
    if "_connector" not in f:
        connector = discovery.create_connector(
            f["org"], f["users"]["admin"], {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}
        )
        connections.run_test(
            f["org"], f["users"]["admin"], connector.microsoft_connection
        )  # a run needs a tested connection
        discovery.run(connector)
        f["_connector"] = connector
    return f["_connector"]


def _connector_run(f):
    connector = _connector(f)
    return {"pk": connector.id, "run_pk": ConnectorRun.objects.filter(connector=connector).earliest("started_at").id}


def _observation(f):
    """The connector's first observation, linked to the fixture application (the drift and missing routes
    act on a linked observation's record, so a stale If-Match is a 412 there rather than "not linked")."""
    from discovery import engine

    found = Observation.objects.filter(connector=_connector(f)).earliest("display_name")
    if not hasattr(found, "decision"):
        engine.link(f["org"], f["users"]["editor"], found, {"resource": str(f["app"].id)})
    return {"pk": found.id}


IMPORT_CSV = b"name,lifecycle\nPayroll,active\n"


def _import_run(f):
    return {"pk": importer.create_run(f["org"], f["users"]["editor"], "application", "x.csv", IMPORT_CSV).id}


def _import_source(f):
    return {"pk": importer.create_source(f["org"], f["users"]["admin"], {"name": "HR", "kind": "application"}).id}


def _token(f):
    owner = Membership.objects.get(organization=f["org"], user=f["users"]["editor"])
    expires = services.org_today(f["org"]) + timedelta(days=30)
    token, _ = tokens.create_token(f["org"], f["users"]["admin"], owner, "Held", True, expires)
    return {"pk": token.id}


def _attachment(f):
    """A file attachment (the download route needs a stored file)."""
    upload = SimpleUploadedFile("notes.txt", b"Runbook notes.")
    return {"pk": attachments.upload_file(f["org"], f["users"]["editor"], f["app"], upload, {}).id}


def _risk_document(f):
    """A vendor risk document on a vendor of its own."""
    from catalog import vendor_risk

    owner = Vendor.objects.create(organization=f["org"], name="Risk document vendor")
    return {"pk": vendor_risk.create_document(f["org"], f["users"]["editor"], owner, {"kind": "pen_test"}).id}


def _quality_exception(f):
    """A documentation exception on a component (the fixture's application already has one per rule at most)."""
    data = {
        "record": str(_component(f, "Undocumented job").id),
        "rule": "documentation",
        "reason": "Documented in the vendor's portal.",
        "owner": str(f["people"]["dana"].id),
    }
    return {"pk": quality.create_exception(f["org"], f["users"]["editor"], data).id}


def _audit_event(f):
    held = AuditEvent.objects.create(organization=f["org"], actor=f["users"]["admin"], action="export", changes={})
    return {"pk": held.id}


def _enterprise(f):
    """The JSON lines export needs `audit.export`: the organization becomes enterprise; the route takes no IDs."""
    from core.capabilities import set_edition

    set_edition(f["org"], "enterprise")
    return {}


def _history_event(f, pk, target_type):
    """A record's (or renewal's) ID and one event of its history."""
    held = AuditEvent.objects.create(
        organization=f["org"], action="resource.updated", target_type=target_type, target_id=pk, changes={}
    )
    return {"pk": pk, "event_pk": held.id}


def _contract(f, name="Held contract"):
    """A contract per organization fixture and name, made once."""
    key = f"_contract_{name}"
    if key not in f:
        f[key] = contracts.create_contract(f["org"], f["users"]["editor"], {"name": name, "end_date": "2027-03-31"})
    return f[key]


def _contract_line(f):
    contract = _contract(f, "Lined contract")
    line = contracts.add_item(f["org"], f["users"]["editor"], contract, {"record": str(f["app"].id)})
    return {"pk": contract.id, "item_pk": line.id}


def _pool(f):
    if "_pool" not in f:
        f["_pool"] = contracts.create_pool(
            f["org"], f["users"]["editor"], {"application": str(f["app"].id), "sku_name": "Held SKU"}
        )
    return f["_pool"]


def _pool_with_proposal(f):
    """The held pool, bound to a SKU a synthetic licence observation reports with other counts (a seat proposal)."""
    from django.utils import timezone

    pool = _pool(f)
    if "_licence" not in f:
        contracts.update_pool(f["org"], f["users"]["editor"], pool, {"sku_id": "sku-held", "seats_purchased": 5})
        now = timezone.now()
        f["_licence"] = Observation.objects.create(
            organization=f["org"],
            connector=_connector(f),
            kind="license",
            external_id="sku-held",
            display_name="Zz Held SKU seats",  # after the connector's applications (_observation takes the first)
            data={"sku_id": "sku-held", "sku_part_number": "HELD", "seats_purchased": 10, "seats_assigned": 3},
            fingerprint="held",
            first_seen_at=now,
            last_seen_at=now,
            last_changed_at=now,
        )
    return {"pk": pool.id, "observation_pk": f["_licence"].id}


def _reminder(f):
    """A live reminder of the fixture application (the generator makes them; the sweep only needs one row)."""
    from datetime import date

    from notify.models import Reminder

    found = Reminder.objects.filter(organization=f["org"], source_id=f["app"].id).first()
    return found or Reminder.objects.create(
        organization=f["org"],
        source_type="periodic_review",
        source_id=f["app"].id,
        resource=f["app"],
        due_date=date(2027, 1, 31),
        title=f["app"].name,
        label="Periodic review due",
    )


def _microsoft_connection(f):
    from core.models import MicrosoftConnection

    found = MicrosoftConnection.objects.filter(organization=f["org"]).first()
    return found or connections.create_connection(
        f["org"],
        f["users"]["admin"],
        connections.MICROSOFT,
        {"name": "Entra (sweep)", "tenant_id": "simulated", "secret": "synthetic-secret"},
    )


def _halo_connection(f):
    """A Halo connection (discovery.halo, in every edition). The fixture organization moves to the enterprise edition,
    which has every community capability too, so the sweep also reaches the enterprise-only routes."""
    from core.capabilities import set_edition
    from core.models import HaloConnection

    f["org"], _ = set_edition(f["org"], "enterprise")
    found = HaloConnection.objects.filter(organization=f["org"]).first()
    return found or connections.create_connection(
        f["org"],
        f["users"]["admin"],
        connections.HALO,
        {"name": "Halo (sweep)", "base_url": "https://halo.simulated.invalid", "client_id": "c", "secret": "s"},
    )


def _ticket_connection(f):
    from tickets import services as ticket_services
    from tickets.models import TicketConnection

    found = TicketConnection.objects.filter(organization=f["org"]).first()
    return found or ticket_services.create_connection(
        f["org"],
        f["users"]["admin"],
        {"system": "jira", "base_url": "https://jira.simulated.invalid", "username": "u@x.test", "secret": "s"},
    )


def _okta_connection(f):
    """Okta connections are enterprise (discovery.okta), with a synthetic private key (tests/vendor_keys.py)."""
    from core.capabilities import set_edition
    from core.models import OktaConnection

    f["org"], _ = set_edition(f["org"], "enterprise")
    found = OktaConnection.objects.filter(organization=f["org"]).first()
    return found or connections.create_connection(
        f["org"],
        f["users"]["admin"],
        connections.OKTA,
        {"name": "Okta (sweep)", "base_url": vendor_keys.OKTA_URL, "client_id": "c", "secret": vendor_keys.pem()},
    )


def _google_connection(f):
    """Google Workspace connections are enterprise (discovery.google), with a synthetic key file."""
    from core.capabilities import set_edition
    from core.models import GoogleWorkspaceConnection

    f["org"], _ = set_edition(f["org"], "enterprise")
    found = GoogleWorkspaceConnection.objects.filter(organization=f["org"]).first()
    return found or connections.create_connection(
        f["org"],
        f["users"]["admin"],
        connections.GOOGLE,
        {"name": "Workspace (sweep)", "admin_email": vendor_keys.GOOGLE_ADMIN, "secret": vendor_keys.key_file()},
    )


def _teams_destination(f):
    from notify import channel_services
    from notify.models import TeamsDestination

    found = TeamsDestination.objects.filter(organization=f["org"]).first()
    return found or channel_services.create_destination(
        f["org"], f["users"]["admin"], TeamsDestination, {"name": "Ops", "url": "https://example.test/flows/1?sig=x"}
    )


def _webhook_destination(f):
    from notify import channel_services
    from notify.models import WebhookDestination

    found = WebhookDestination.objects.filter(organization=f["org"]).first()
    return found or channel_services.create_destination(
        f["org"],
        f["users"]["admin"],
        WebhookDestination,
        {"name": "Hook", "url": "https://example.test/hook", "signing_secret": "s" * 32},
    )


def _subscription(f, user):
    """A shared view and `user`'s weekly subscription to it (saved views)."""
    from notify.models import SavedViewSubscription

    view = SavedFilter.objects.create(
        organization=f["org"], user=user, shared=True, kind="application", name="Subscribed", query={}
    )
    membership = Membership.objects.get(organization=f["org"], user=user)
    return SavedViewSubscription.objects.create(
        organization=f["org"], saved_view=view, membership=membership, weekday=2
    )


def _notification_rule(f):
    from notify import rule_services
    from notify.models import NotificationRule

    found = NotificationRule.objects.filter(organization=f["org"], name="Spare rule").first()
    return found or rule_services.create_rule(
        f["org"], f["users"]["admin"], {"name": "Spare rule", "source_types": ["contract"], "lead_times_days": [30]}
    )


def _webhook_delivery(f):
    """A queued event delivery to the sweep's webhook, with one logged attempt."""
    from django.utils import timezone

    from notify import deliveries
    from notify.models import DeliveryAttempt

    delivery, _ = deliveries.enqueue(
        f["org"],
        channel="webhook",
        kind="event",
        dedupe_key="sweep:webhook",
        subject="record.updated",
        content={"event": "record.updated", "id": "5a3c3f8e-5d0b-4f62-9a8e-0d9b5b1d2c11", "data": {"record": {}}},
        webhook_destination=_webhook_destination(f),
    )
    DeliveryAttempt.objects.get_or_create(
        organization=f["org"],
        delivery=delivery,
        number=1,
        defaults={"started_at": timezone.now(), "outcome": "retrying", "status_code": 500, "duration_ms": 12},
    )
    return delivery


def _delivery(f):
    from notify import deliveries

    delivery, _ = deliveries.enqueue(
        f["org"],
        channel="email",
        dedupe_key="sweep:delivery",
        subject="Sweep",
        content={"text": "Hi", "html": "<p>Hi</p>"},
        recipient=f["users"]["admin"].email,
    )
    return delivery


# {viewset class name: (organization fixture, acting user) -> URL kwargs}. Rows that belong to one user (snoozes,
# personal saved filters) belong to the acting user.
def _member_identity(f):
    """The viewer and a pinned identity of theirs (DELETE users/<pk>/identities/<identity_id>)."""
    from core.models import UserIdentity

    viewer = f["users"]["viewer"]
    identity, _ = UserIdentity.objects.get_or_create(
        user=viewer, issuer="https://idp.example.test", subject=str(viewer.id)
    )
    return {"pk": viewer.id, "identity_id": identity.id}


def _invitation(f):
    from catalog import invitations

    made, _ = invitations.create_invitation(
        f["org"], f["users"]["admin"], {"email": f"invited@{f['org'].slug}.test", "role": "viewer"}
    )
    return {"pk": made.id}


def _org_export(f):
    """A ready export with its zip on disk, so the download route answers 200."""
    from catalog import org_lifecycle
    from core.models import ExportStatus, OrgExport

    export = OrgExport.objects.create(organization=f["org"], requested_by=f["users"]["admin"])
    org_lifecycle._finish(export, ExportStatus.READY, **org_lifecycle.build(export))
    return {"pk": export.id}


def _pending_access(f):
    from django.utils import timezone

    from core.models import PendingAccess

    now = timezone.now()
    row = PendingAccess.objects.create(
        organization=f["org"], issuer="https://idp.example.test", subject=f"p-{f['org'].slug}", email="p@x.test",
        first_seen=now, last_seen=now,
    )  # fmt: skip
    return {"pk": row.id}


FACTORIES = {
    "ApplicationViewSet": lambda f, u: {"pk": f["app"].id},
    "ComponentViewSet": lambda f, u: {"pk": _component(f).id},
    "AIAgentViewSet": lambda f, u: {"pk": _agent(f).id},
    "AIAgentReviewViewSet": lambda f, u: _review(f, _agent(f)),
    "AIProviderViewSet": lambda f, u: {
        "pk": ai.create_provider(f["org"], f["users"]["editor"], {"name": "Spare AI"}).id
    },
    "AIModelViewSet": lambda f, u: {"pk": _ai_model(f, "Spare model").id},
    "CustomFieldViewSet": lambda f, u: {
        "pk": customfields.create_field(
            f["org"],
            f["users"]["admin"],
            {"name": "spare_field", "label": "Spare", "type": "text", "applies_to": ["application"]},
        ).id
    },
    "RelationshipTypeViewSet": lambda f, u: {
        "pk": relationship_types.create_type(f["org"], f["users"]["admin"], {"name": "Spare type"}).id
    },
    "IntegrationViewSet": lambda f, u: {"pk": _integration(f).id},
    "RelationshipViewSet": lambda f, u: {"pk": _relationship(f).id},
    "ApplicationReviewViewSet": lambda f, u: _review(f, f["app"]),
    "ComponentReviewViewSet": lambda f, u: _review(f, _component(f)),
    "RelationshipConfirmationViewSet": lambda f, u: _confirmed_relationship(f),
    "IntegrationConfirmationViewSet": lambda f, u: _confirmed_integration(f),
    "InstanceViewSet": lambda f, u: {
        "pk": f["app"].id,
        "instance_pk": services.create_instance(f["org"], f["users"]["editor"], f["app"], {"name": "Prod"}).id,
    },
    "AssessmentViewSet": lambda f, u: _assessment(f),
    "PersonViewSet": lambda f, u: {"pk": f["people"]["priya"].id},
    "TeamViewSet": lambda f, u: {"pk": Team.objects.create(organization=f["org"], name="Spare team").id},
    "DepartmentViewSet": lambda f, u: {"pk": Department.objects.create(organization=f["org"], name="Spare").id},
    "VendorViewSet": lambda f, u: {"pk": Vendor.objects.create(organization=f["org"], name="Spare vendor").id},
    "SoftwareCategoryViewSet": lambda f, u: {
        "pk": SoftwareCategory.objects.create(organization=f["org"], name="Spare category").id
    },
    "TagViewSet": lambda f, u: _tag(f),
    "SavedFilterViewSet": lambda f, u: {"pk": _saved_filter(f, u).id},
    "RenewalViewSet": lambda f, u: _renewal(f),
    "SnoozeViewSet": lambda f, u: {"pk": _snooze(f, u).id},
    "DigestViewSet": lambda f, u: {"pk": _digest(f).id},
    # Part C
    "ConnectorViewSet": lambda f, u: {"pk": _connector(f).id},
    "ConnectorRunViewSet": lambda f, u: _connector_run(f),
    "RunHistoryViewSet": lambda f, u: {"pk": _connector_run(f)["run_pk"]},  # also one run by its ID
    "ObservationViewSet": lambda f, u: _observation(f),
    "RecordSourceViewSet": lambda f, u: {"pk": f["app"].id},
    "DiscoveredCredentialViewSet": lambda f, u: {},  # a list without an ID
    "ShadowItViewSet": lambda f, u: {},  # the shadow IT overview, a list without an ID
    "SsoCoverageViewSet": lambda f, u: {},  # the SSO and MFA coverage report, a list without an ID
    "ImportSourceViewSet": lambda f, u: _import_source(f),
    "ImportRunViewSet": lambda f, u: _import_run(f),
    "ApiTokenViewSet": lambda f, u: _token(f),
    "UserViewSet": lambda f, u: _member_identity(f),
    "InvitationViewSet": lambda f, u: _invitation(f),
    "OrgExportViewSet": lambda f, u: _org_export(f),
    "PendingAccessViewSet": lambda f, u: _pending_access(f),
    "AuditEventViewSet": lambda f, u: _audit_event(f),
    "AuditCsvExportViewSet": lambda f, u: {},
    "AuditJsonlExportViewSet": lambda f, u: _enterprise(f),
    "ApplicationAuditViewSet": lambda f, u: _history_event(f, f["app"].id, "Resource"),
    "ComponentAuditViewSet": lambda f, u: _history_event(f, _component(f).id, "Resource"),
    "AIAgentAuditViewSet": lambda f, u: _history_event(f, _agent(f).id, "Resource"),
    "IntegrationAuditViewSet": lambda f, u: _history_event(f, _integration(f).id, "Resource"),
    "RenewalAuditViewSet": lambda f, u: _history_event(f, _renewal(f)["pk"], "RenewalObligation"),
    # Attachments
    "ApplicationAttachmentViewSet": lambda f, u: {"pk": f["app"].id},
    "ComponentAttachmentViewSet": lambda f, u: {"pk": _component(f, "Attached job").id},
    "AIAgentAttachmentViewSet": lambda f, u: {"pk": _agent(f, "Attached helper").id},
    "IntegrationAttachmentViewSet": lambda f, u: {"pk": _integration(f).id},
    "VendorAttachmentViewSet": lambda f, u: {
        "pk": Vendor.objects.create(organization=f["org"], name="Attached vendor").id
    },
    "AttachmentViewSet": lambda f, u: _attachment(f),
    # Vendor risk file
    "VendorRiskDocumentViewSet": lambda f, u: _risk_document(f),
    "VendorRiskProfileViewSet": lambda f, u: {
        "pk": Vendor.objects.create(organization=f["org"], name="Risk profile vendor").id
    },
    "VendorRiskDocumentsOfVendorViewSet": lambda f, u: {
        "pk": Vendor.objects.create(organization=f["org"], name="Risk vendor").id
    },
    # Data quality
    "FailureViewSet": lambda f, u: {},
    "QualityExceptionViewSet": lambda f, u: _quality_exception(f),
    # Contracts and licence pools
    "ContractViewSet": lambda f, u: {"pk": _contract(f).id},
    "ContractLineViewSet": lambda f, u: _contract_line(f),
    "ContractAttachmentViewSet": lambda f, u: {"pk": _contract(f).id},
    "ContractAuditViewSet": lambda f, u: _history_event(f, _contract(f).id, "Contract"),
    "LicensePoolViewSet": lambda f, u: _pool_with_proposal(f),
    "LicensePoolAuditViewSet": lambda f, u: _history_event(f, _pool(f).id, "LicensePool"),
    # Reminders
    "ReminderViewSet": lambda f, u: {"pk": _reminder(f).id},
    # Channels and the notification log
    "TeamsDestinationViewSet": lambda f, u: {"pk": _teams_destination(f).id},
    "WebhookDestinationViewSet": lambda f, u: {"pk": _webhook_destination(f).id},
    "NotificationLogViewSet": lambda f, u: {"pk": _delivery(f).id},
    # Webhooks
    "WebhookViewSet": lambda f, u: {"pk": _webhook_destination(f).id},
    "WebhookDeliveryViewSet": lambda f, u: {"pk": _webhook_delivery(f).id},
    # Notification rules
    "NotificationRuleViewSet": lambda f, u: {"pk": _notification_rule(f).id},
    # Saved view subscriptions
    "SavedViewSubscriptionViewSet": lambda f, u: {"pk": _subscription(f, u).id},
    # Connections
    "MicrosoftConnectionViewSet": lambda f, u: {"pk": _microsoft_connection(f).id},
    "HaloConnectionViewSet": lambda f, u: {"pk": _halo_connection(f).id},
    # Ticket links
    "TicketConnectionViewSet": lambda f, u: {"pk": _ticket_connection(f).id},
    # Okta and Google Workspace connections
    "OktaConnectionViewSet": lambda f, u: {"pk": _okta_connection(f).id},
    "GoogleWorkspaceConnectionViewSet": lambda f, u: {"pk": _google_connection(f).id},
}

# Routes that need a parameter to answer at all: {(viewset, action): query}.
QUERY = {("SavedFilterViewSet", "list"): "?kind=application", ("ImportRunViewSet", "template"): "?kind=application"}
# Signed-in sessions only: an API token is refused even a read.
SESSION_ONLY = {
    "ApiTokenViewSet": "Manage API tokens from a signed-in session, not with a token.",
}
# One action of a viewset whose other routes a token may read (the whole organization in one file).
SESSION_ONLY_ACTIONS = {
    ("OrgExportViewSet", "download"): "Request and download the organization's full export from a signed-in "
    "session, not with an API token.",
}

PARAM = re.compile(r"<(?:\w+:)?(\w+)>")


def viewset_routes():
    """[(route, viewset class, {method: action})] for every TenantViewSet route in the resolver."""
    found = []
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if cls is not None and issubclass(cls, drf.TenantViewSet):
            found.append((route, cls, dict(callback.actions)))
    return found


def tenant_composite(callback) -> bool:
    """A RoleAPIView that answers members of an organization: not a platform console route (staff only,
    no organization; tests/test_platform.py sweeps those)."""
    cls = getattr(callback, "cls", None)
    if cls is None or not issubclass(cls, drf.RoleAPIView):
        return False
    declared = drf.declared_policy(callback)
    return declared is None or declared.read != drf.PLATFORM


def composite_writes(f, user):
    """[(method, path, view class)] for every tenant RoleAPIView route method that isn't a read. A route that takes
    IDs (a record's or an instance's end-of-life tracking) gets them from its KEYED_COMPOSITES entry, made in
    f's organization."""
    found: list[tuple[str, str, type]] = []
    for route, callback in walk(get_resolver().url_patterns):
        if tenant_composite(callback):
            cls = callback.cls
            writes = [m for m in ("post", "put", "patch", "delete") if hasattr(cls, m)]
            if not writes:
                continue
            path = "/" + route
            if PARAM.findall(route):
                kind = getattr(callback, "initkwargs", {}).get("kind")
                path = fill(route, KEYED_COMPOSITES[cls.__name__, kind](f, user))
            found.extend((method, path, cls) for method in writes)
    return found


def fill(route, kwargs):
    return "/" + PARAM.sub(lambda m: str(kwargs[m.group(1)]), route)


# One viewset serving several record kinds takes the kind from each route (RecordSourceViewSet), so another
# kind's ID is a 404 there: those routes get a record of their own kind instead of the viewset's FACTORIES entry.
BY_ROUTE = {
    "components/<uuid:pk>/discovery-sources": lambda f, u: {"pk": _component(f, "Sourced job").id},
    "ai-agents/<uuid:pk>/discovery-sources": lambda f, u: {"pk": _agent(f, "Sourced helper").id},
}


def requests_for(f, user, keyed_only=False):
    """[(method, path, action, viewset)] for every viewset route, with IDs from objects made in f's organization."""
    out, made = [], {}
    for route, cls, actions in viewset_routes():
        params = PARAM.findall(route)
        if keyed_only and not params:
            continue
        key = next((suffix for suffix in BY_ROUTE if route.endswith(suffix)), cls.__name__)
        if key not in made:
            made[key] = (BY_ROUTE.get(key) or FACTORIES[cls.__name__])(f, user)
        for method, action in actions.items():
            if method in ("head", "options"):
                continue
            query = QUERY.get((cls.__name__, action), "")
            out.append((method, fill(route, made[key]) + query, action, cls))
    return out


def send(client, method, path, body=None, **headers):
    if method == "get":
        return client.get(path, **headers)
    data = json.dumps(body if body is not None else {})
    return getattr(client, method)(path, data, content_type="application/json", **headers)


def test_every_viewset_has_a_factory():
    names = {cls.__name__ for _, cls, _ in viewset_routes()}
    assert names == set(FACTORIES), "add the new viewset to FACTORIES so the checks below cover it"
    assert len(names) >= 32


# ---- another organization's IDs are 404, whatever the route and method -----------------------------------------------


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_another_organizations_ids_are_404_on_every_viewset_route(a, b):
    from core.capabilities import set_edition

    set_edition(a["org"], "enterprise")  # so an enterprise route (Halo connections) answers past its capability check
    client = Client()
    client.force_login(a["users"]["admin"])
    requests = requests_for(b, b["users"]["admin"], keyed_only=True)
    before = AuditEvent.objects.filter(organization=b["org"]).count()
    assert len(requests) > 40
    for method, path, action, cls in requests:
        response = send(client, method, path)
        assert response.status_code == 404, (cls.__name__, action, method, path, response.status_code)
        assert response["Content-Type"] == "application/problem+json", path
    assert AuditEvent.objects.filter(organization=b["org"]).count() == before  # nothing in b changed


# Keyed read-only composites: RoleAPIView GET routes with an ID in the path (a record's dependencies and map). The
# viewset sweep above doesn't reach them, so each (view class, kind) needs an entry here that makes the IDs in an
# organization; a new keyed RoleAPIView without one fails the test below.


def _record_ids(kind):
    def make(f, user):
        made = services.create_resource(f["org"], user, {"name": f"Keyed {kind}", "kind": kind, "lifecycle": "active"})
        return {"pk": made.id}

    return make


def _assistant_batch(f, user):
    """An assistant batch. The assistant is enterprise: the organization gets that edition (audited once,
    the setup's one event)."""
    from core.capabilities import set_edition
    from discovery.models import AssistantBatch

    f["org"], _ = set_edition(f["org"], "enterprise")
    return {"pk": AssistantBatch.objects.create(organization=f["org"], trigger="manual", status="succeeded").id}


def _tracked_record(kind):
    """A record with end-of-life tracking: the record through the service (its one setup event), the
    tracking row directly, so the 404 sweep's count of setup events stays one per route."""

    def make(f, user):
        from eol.models import TrackedVersion

        made = services.create_resource(f["org"], user, {"name": f"EOL {kind}", "kind": kind, "lifecycle": "active"})
        TrackedVersion.objects.create(organization=f["org"], resource=made, product="postgresql", version="16.4")
        return {"pk": made.id}

    return make


def _tracked_instance(f, user):
    """An application instance with a version of its own, one setup event as above."""
    from catalog.models import ApplicationInstance
    from eol.models import TrackedVersion

    made = services.create_resource(f["org"], user, {"name": "EOL app", "kind": "application", "lifecycle": "active"})
    instance = ApplicationInstance.objects.create(organization=f["org"], application=made, name="Production")
    for where in (None, instance):
        TrackedVersion.objects.create(organization=f["org"], resource=made, instance=where, product="postgresql")
    return {"pk": made.id, "instance_pk": instance.id}


def _ticket_link(kind, with_link):
    """An assessment or a renewal (made directly, no event) with one ticket link through the service: its one setup
    event (tickets/views.py)."""

    def make(f, user):
        from catalog.models import ChangeAssessment
        from money.models import RenewalObligation
        from tickets import services as ticket_services

        parent: ChangeAssessment | RenewalObligation
        if kind == "assessment":
            parent = ChangeAssessment.objects.create(
                organization=f["org"], target=f["app"], change_type="change", title="Ticketed change"
            )
            target = ticket_services.Target("assessment", parent)
        else:
            parent = RenewalObligation.objects.create(organization=f["org"], name="Ticketed renewal")
            target = ticket_services.Target("renewal", parent)
        link = ticket_services.create_link(
            f["org"], user, target, {"url": "https://contoso.atlassian.net/browse/ITOPS-1"}
        )
        return {"pk": parent.id, "link_pk": link.id} if with_link else {"pk": parent.id}

    return make


def _eol_product(f, user):
    from eol.models import EolProduct

    return {"name": EolProduct.objects.get_or_create(name="postgresql", defaults={"label": "PostgreSQL"})[0].name}


def _sku(f, user):
    """A Microsoft SKU ID (the licence waste drill-down): public, the same in every tenant."""
    return {"sku_id": "06ebc4ee-1bb5-47dd-8120-11324bc54e06"}


def _person(f, user):
    """A Person (offboarding: footprint and reassign), through the service: one setup event."""
    return {"pk": services.create_person(f["org"], user, {"name": "Leaving Person"}).id}


KEYED_COMPOSITES: dict[tuple[str, str | None], Callable[[Any, Any], dict[str, Any]]] = {
    **{
        (view, kind): _record_ids(kind)
        for view in ("RecordDependenciesView", "RecordMapView")
        for kind in ("application", "component", "ai_agent")
    },
    ("AssistantBatchView", None): _assistant_batch,
    **{("RecordEndOfLifeView", kind): _tracked_record(kind) for kind in ("application", "component")},
    ("InstanceEndOfLifeView", None): _tracked_instance,
    ("ProductDetailView", None): _eol_product,
    **{(view, None): _person for view in ("FootprintView", "FootprintExportView", "ReassignView")},
    # Ticket links, nested under an assessment or a renewal; CreateTicketView and TicketLinkRefreshView are
    # write-only (no GET): the sweep below counts them, and composite_writes takes their IDs from here
    **{
        (view, kind): _ticket_link(kind, False)
        for view in ("TicketLinksView", "CreateTicketView")
        for kind in ("assessment", "renewal")
    },
    **{
        (view, kind): _ticket_link(kind, True)
        for view in ("TicketLinkView", "TicketLinkRefreshView")
        for kind in ("assessment", "renewal")
    },
    # Licence waste: a SKU's drill-down (Microsoft's SKU ID, the same in every tenant), a pool's and a
    # contract's block.
    **{(view, None): _sku for view in ("LicenceWasteUsersView", "LicenceWasteUsersExportView")},
    ("LicencePoolWasteView", None): lambda f, u: {"pk": _pool(f).id},
    ("ContractWasteView", None): lambda f, u: {"pk": _contract(f).id},
}
# Keyed by installation-wide public data, not an organization's row: every organization reads the same one, so the
# 404 sweep below leaves it out. An endoflife.date product name (eol/views.py), and a Microsoft SKU ID (the
# licence waste drill-down answers any organization with its own users of that SKU; another's are never listed).
INSTALLATION_KEYED = {"ProductDetailView", "LicenceWasteUsersView", "LicenceWasteUsersExportView"}


def keyed_composite_reads():
    """[(route, view class, kind)] for every tenant RoleAPIView GET route that takes an ID; `kind` from the
    URLconf."""
    found = []
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if tenant_composite(callback) and hasattr(cls, "get") and PARAM.findall(route):
            found.append((route, cls, getattr(callback, "initkwargs", {}).get("kind")))
    return found


def test_another_organizations_ids_are_404_on_every_keyed_composite_route(a, b):
    client = Client()
    client.force_login(a["users"]["admin"])
    routes = keyed_composite_reads()
    # Write-only keyed routes (a person's reassign, a ticket link's create-ticket and refresh) take their IDs
    # from KEYED_COMPOSITES too (composite_writes), so the registry is every keyed route, read or write.
    write_only = {
        (callback.cls.__name__, getattr(callback, "initkwargs", {}).get("kind"))
        for route, callback in walk(get_resolver().url_patterns)
        if tenant_composite(callback) and PARAM.findall(route) and not hasattr(callback.cls, "get")
    }
    assert {(cls.__name__, kind) for _, cls, kind in routes} | write_only == set(KEYED_COMPOSITES), (
        "add an entry to KEYED_COMPOSITES"
    )
    prefixes = {route.split("/")[2] for route, _, _ in routes}
    assert {"applications", "components", "ai-agents"} <= prefixes
    from core.capabilities import set_edition

    set_edition(a["org"], "enterprise")  # capability-gated routes (the assistant's batches) answer past the gate
    before = AuditEvent.objects.filter(organization=b["org"]).count()
    routes = [r for r in routes if r[1].__name__ not in INSTALLATION_KEYED]
    for route, cls, kind in routes:
        theirs = fill(route, KEYED_COMPOSITES[cls.__name__, kind](b, b["users"]["admin"]))
        response = send(client, "get", theirs)
        assert response.status_code == 404, (cls.__name__, theirs, response.status_code)
        assert response["Content-Type"] == "application/problem+json", theirs
        mine = fill(route, KEYED_COMPOSITES[cls.__name__, kind](a, a["users"]["admin"]))
        assert send(client, "get", mine).status_code == 200, mine  # the route itself works for one's own record
    assert AuditEvent.objects.filter(organization=b["org"]).count() == before + len(routes)  # only the setup's


# ---- a read-only token never writes ------------------------------------------------------------------------------------


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_a_read_only_token_is_refused_on_every_viewset_write(a, mint):  # noqa: F811
    bearer = Bearer(mint("admin", read_only=True)["secret"])
    requests = requests_for(a, a["users"]["admin"])
    composites = composite_writes(a, a["users"]["admin"])
    before = AuditEvent.objects.filter(organization=a["org"]).count()
    writes = [r for r in requests if r[0] != "get"]
    assert len(writes) > 30
    for method, path, action, cls in writes:
        response = send(bearer.client, method, path)
        assert response.status_code == 403, (cls.__name__, action, method, path, response.status_code)
        assert response.json()["detail"] == "This token is read-only.", path
    assert {path for _, path, _ in composites} >= {
        "/api/v1/bulk-edit/preview",
        "/api/v1/bulk-edit/apply",
        "/api/v1/bulk-delete/preview",
        "/api/v1/bulk-delete/apply",
        "/api/v1/auth/me/preferences",
        "/api/v1/organization",
        "/api/v1/auth/password",
    }
    body = {"kind": "application", "ids": [str(a["app"].id)], "changes": {"add_tags": ["via-token"]}}
    for method, path, cls in composites:
        response = send(bearer.client, method, path, body)
        assert response.status_code == 403, (cls.__name__, method, path, response.status_code)
        assert response.json()["detail"] == "This token is read-only.", path
    assert AuditEvent.objects.filter(organization=a["org"]).count() == before  # none of the writes happened
    assert not ResourceTag.objects.filter(resource=a["app"], tag__name="via-token").exists()
    reads = [r for r in requests if r[0] == "get"]
    for method, path, action, cls in reads:
        response = send(bearer.client, method, path)
        if cls.__name__ in SESSION_ONLY:
            assert response.status_code == 403 and response.json()["detail"] == SESSION_ONLY[cls.__name__], path
        elif (cls.__name__, action) in SESSION_ONLY_ACTIONS:
            detail = SESSION_ONLY_ACTIONS[cls.__name__, action]
            assert response.status_code == 403 and response.json()["detail"] == detail, path
        else:
            assert response.status_code == 200, (cls.__name__, action, path)


# ---- If-Match --------------------------------------------------------------------------------------------------------

ETAG = re.compile(r'^"[0-9a-f]{32}"$')
STALE = '"' + "0" * 32 + '"'


def conditional(action, cls):
    return action in ("partial_update", "destroy") or action in cls.if_match_actions


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_a_stale_if_match_is_412_on_every_conditional_write(a):
    client = Client()
    client.force_login(a["users"]["admin"])
    requests = [r for r in requests_for(a, a["users"]["admin"]) if conditional(r[2], r[3])]
    assert {cls.__name__ for *_, cls in requests} >= {
        "ApplicationViewSet",
        "IntegrationViewSet",
        "RelationshipViewSet",
        "InstanceViewSet",
        "AssessmentViewSet",
        "PersonViewSet",
        "TeamViewSet",
        "SavedFilterViewSet",
        "RenewalViewSet",
        "SnoozeViewSet",
        "ConnectorViewSet",
        "ImportSourceViewSet",
        "ImportRunViewSet",
        "UserViewSet",
    }
    before = AuditEvent.objects.filter(organization=a["org"]).count()
    for method, path, action, cls in requests:
        response = send(client, method, path, {"name": "Changed"}, HTTP_IF_MATCH=STALE)
        assert response.status_code == 412, (cls.__name__, action, method, path, response.status_code)
        body = response.json()
        assert body["type"] == "/problems/precondition-failed" and ETAG.match(response["ETag"]), path
        assert body["revision"] == response["ETag"].strip('"')
    assert AuditEvent.objects.filter(organization=a["org"]).count() == before  # nothing was written


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_retrieve_carries_a_stable_etag_that_if_match_accepts(a):
    client = Client()
    client.force_login(a["users"]["admin"])
    requests = requests_for(a, a["users"]["admin"])
    detail = {(path, cls) for method, path, action, cls in requests if action == "retrieve"}
    assert len(detail) >= 10
    patchable = {(path, cls) for method, path, action, cls in requests if action == "partial_update"}
    for path, cls in detail:
        first = client.get(path)
        assert first.status_code == 200 and ETAG.match(first["ETag"]), (cls.__name__, path)
        assert client.get(path)["ETag"] == first["ETag"], path
        if (path, cls) in patchable:  # a current tag is never a 412 (an empty PATCH may still be refused)
            response = send(client, "patch", path, {}, HTTP_IF_MATCH=first["ETag"])
            assert response.status_code != 412, (cls.__name__, path)


def test_a_renewal_cycle_edit_honours_the_renewals_etag(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    ids = _renewal(a)
    url, cycle = f"/api/v1/renewals/{ids['pk']}", f"/api/v1/renewals/{ids['pk']}/cycles/{ids['cycle_pk']}"
    seen = client.get(url)["ETag"]
    edited = send(client, "patch", cycle, {"renewal_date": "2027-03-31"}, HTTP_IF_MATCH=seen)
    assert edited.status_code == 200 and edited["ETag"] != seen  # the cycle is part of the renewal's revision
    stale = send(client, "patch", cycle, {"renewal_date": "2027-04-30"}, HTTP_IF_MATCH=seen)
    assert stale.status_code == 412
    assert client.get(url).json()["cycle"]["renewal_date"] == "2027-03-31"


# ---- plain-array lists: ordering, and parameters they don't take ---------------------------------------------------------


def test_short_lists_are_arrays_with_whitelisted_ordering(a, api):
    client = api(a["users"]["viewer"])
    names = [p["name"] for p in client.get("people").json_body]
    assert names == sorted(names)
    assert [p["name"] for p in client.get("people?ordering=-name").json_body] == sorted(names, reverse=True)
    refused = client.get("people?ordering=email&colour=red&cursor=abc")
    assert refused.status_code == 400
    assert set(refused.json_body["errors"]) == {"ordering", "colour", "cursor"}
    assert client.get("people?active=false").json_body[0]["name"] == "Former person"
    assert client.get("renewals?ordering=name").status_code == 400  # by action deadline, always


def test_people_search_and_limit_for_search_boxes(a, api):
    """The command palette asks people?q=…&limit=5 instead of loading everyone."""
    from catalog.models import Person

    a["people"]["dana"].email = "dana.ortiz@example.test"
    a["people"]["dana"].save()
    for i in range(7):
        Person.objects.create(organization=a["org"], name=f"Searchable {i}")
    client = api(a["users"]["viewer"])
    assert [p["name"] for p in client.get("people?q=ORTIZ").json_body] == ["Dana Ortiz"]
    assert [p["name"] for p in client.get("people?q=example.test").json_body] == ["Dana Ortiz"]  # email
    limited = [p["name"] for p in client.get("people?q=searchable&limit=5").json_body]
    assert limited == [f"Searchable {i}" for i in range(5)]
    assert [p["name"] for p in client.get("people?q=searchable&limit=3&ordering=-name").json_body] == [
        "Searchable 2",
        "Searchable 1",
        "Searchable 0",
    ]  # the first three by name, shown in the asked order
    for bad in ("0", "51", "x"):
        refused = client.get(f"people?limit={bad}")
        assert refused.status_code == 400 and "limit" in refused.json_body["errors"]


def test_an_empty_filter_value_is_no_filter(a, api):
    """As the function views had it: relationships?resource=, ?type= and people?active= with no value filter nothing."""
    client = api(a["users"]["viewer"])
    _relationship(a)
    everyone = client.get("people").json_body
    assert client.get("people?active=").json_body == everyone
    links = client.get("relationships").json_body
    assert len(links) == 1
    for query in ("resource=", "type=", "resource=&type="):
        response = client.get(f"relationships?{query}")
        assert response.status_code == 200 and response.json_body == links, query
    assert client.get("relationships?type=nope").status_code == 400  # a value is still checked
    assert client.get("people?active=maybe").status_code == 400


def test_a_viewer_is_refused_a_shared_filter_before_if_match(a, api):
    shared = SavedFilter.objects.create(
        organization=a["org"],
        user=a["users"]["editor"],
        shared=True,
        kind="application",
        name="Team view",
        query={},
        created_by=a["users"]["editor"],
    )
    client = Client()
    client.force_login(a["users"]["viewer"])
    url = f"/api/v1/saved-filters/{shared.id}"
    assert client.get(url).status_code == 200
    response = send(client, "delete", url, HTTP_IF_MATCH=STALE)
    assert response.status_code == 403  # the role refusal, not 412
    assert response.json()["detail"] == (
        "Only its owner (an Editor or Admin) or an Admin can change or remove a shared view."
    )
    assert client.get("/api/v1/saved-filters/builtin:active").status_code == 404
    assert SavedFilter.objects.filter(id=shared.id).exists()


# ---- composite responses are described key for key ---------------------------------------------------------------------


def described(serializer, data, where="$"):
    """Problems where `data` has a key `serializer` doesn't declare, or lacks one it requires (recursively)."""
    problems = []
    if isinstance(serializer, serializers.ListSerializer):
        for i, item in enumerate(data):
            problems += described(serializer.child, item, f"{where}[{i}]")
        return problems
    if not isinstance(serializer, serializers.Serializer) or not isinstance(data, dict):
        return problems
    fields = serializer.fields
    extra = set(data) - set(fields)
    missing = {name for name, field in fields.items() if field.required and name not in data}
    problems += [f"{where}: undescribed {sorted(extra)}"] if extra else []
    problems += [f"{where}: missing {sorted(missing)}"] if missing else []
    for name, field in fields.items():
        if data.get(name) is not None:
            problems += described(field, data[name], f"{where}.{name}")
    return problems


@pytest.fixture
def busy(a):
    """Something in every queue and on every composite screen."""
    org, editor = a["org"], a["users"]["editor"]
    job = _component(a, "Nightly job")
    services.create_instance(org, editor, a["app"], {"name": "Prod"})
    services.create_relationship(org, editor, {"type": "depends_on", "source": str(job.id), "target": str(a["app"].id)})
    feed = services.create_integration(
        org, editor, {"name": "Feed", "lifecycle": "active", "source": str(a["app"].id), "target": str(job.id)}
    )
    services.create_relationship(org, editor, {"type": "implements", "source": str(job.id), "target": str(feed.id)})
    services.record_review(org, editor, a["app"], {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Mail"})
    held = assessments.create_assessment(
        org, editor, {"target": str(a["app"].id), "change_type": "change", "title": "Move"}
    )
    services.update_resource(org, editor, job, {"lifecycle": "deprecated"})  # makes the snapshot stale
    soon = (services.org_today(org) + timedelta(days=20)).isoformat()
    renewal = renewals.create_renewal(
        org, editor, {"name": "Licence", "auto_renew": "yes", "renewal_date": soon, "applications": [str(a["app"].id)]}
    )
    renewals.create_renewal(org, editor, {"name": "Gappy", "auto_renew": "unknown"})
    return {"job": job, "assessment": held, "renewal": renewal}


def test_composite_responses_match_their_serializers(a, api, busy):
    client = api(a["users"]["admin"])
    app = a["app"].id
    held = busy["assessment"].id
    snapshot = client.get(f"assessments/{held}/snapshots").json_body[0]["id"]
    cases = [
        (DependenciesSerializer(), f"dependencies?target={app}&direction=both&depth=all&show_instances=true"),
        (AttentionSerializer(), "needs-attention?scope=all"),
        (AssessmentListSerializer(many=True), "assessments"),
        (AssessmentSerializer(), f"assessments/{held}"),
        (SnapshotSummarySerializer(many=True), f"assessments/{held}/snapshots"),
        (SnapshotSerializer(), f"assessments/{held}/snapshots/{snapshot}"),
        (RenewalListSerializer(many=True), "renewals"),
        (RenewalSerializer(), f"renewals/{busy['renewal'].id}"),
        (DeadlineSerializer(), "renewals/preview?renewal_date=2027-03-31&notice_value=2&notice_unit=months"),
        (PreferenceSerializer(), "auth/me/preferences"),
        (SavedFilterSerializer(many=True), "saved-filters?kind=application"),
    ]
    for serializer, url in cases:
        response = client.get(url)
        assert response.status_code == 200, (url, response.json_body)
        problems = described(serializer, response.json_body)
        assert problems == [], (url, problems)
    attention = client.get("needs-attention?scope=all").json_body
    filled = [
        q for q in ("ownership_gaps", "reviews", "stale_assessments", "renewal_actions", "renewal_gaps") if attention[q]
    ]
    assert len(filled) == 5, attention  # the fixture reaches the queues the check is about
    body = {"kind": "application", "ids": [str(app)], "changes": {"add_tags": ["finance"]}}
    preview = client.post("bulk-edit/preview", body)
    assert described(BulkPreviewSerializer(), preview.json_body) == []
    applied = client.post("bulk-edit/apply", body)
    assert described(BulkResultSerializer(), applied.json_body) == []
    spare = services.create_resource(
        a["org"], a["users"]["admin"], {"name": "Spare", "kind": "application", "lifecycle": "active"}
    )
    referenced = services.create_resource(
        a["org"], a["users"]["admin"], {"name": "Held", "kind": "application", "lifecycle": "active"}
    )
    services.create_relationship(
        a["org"], a["users"]["admin"], {"type": "depends_on", "source": str(spare.id), "target": str(referenced.id)}
    )
    doomed = {"kind": "application", "ids": [str(spare.id), str(referenced.id)]}
    preview = client.post("bulk-delete/preview", doomed)
    assert described(BulkDeletePreviewSerializer(), preview.json_body) == []
    refused = client.post("bulk-delete/apply", doomed)
    assert refused.status_code == 409
    assert [described(BulkDeleteRefusedRowSerializer(), row) for row in refused.json_body["rows"]] == [[], []]
    retire = {"kind": "application", "ids": [str(referenced.id)], "changes": {"lifecycle": "retired"}}
    edit_refused = client.post("bulk-edit/apply", retire)
    assert edit_refused.status_code == 400
    assert [described(BulkRefusedRowSerializer(), row) for row in edit_refused.json_body["rows"]] == [[]]
    services.delete_relationship(a["org"], a["users"]["admin"], Relationship.objects.get(source=spare))
    deleted = client.post("bulk-delete/apply", doomed)
    assert described(BulkDeleteResultSerializer(), deleted.json_body) == []


def test_the_described_check_notices_drift():
    class Shape(serializers.Serializer):
        a = serializers.CharField()
        b = serializers.CharField(required=False)

    assert described(Shape(), {"a": "x"}) == []
    assert described(Shape(), {"a": "x", "c": 1}) == ["$: undescribed ['c']"]
    assert described(Shape(), {"b": "x"}) == ["$: missing ['a']"]


# ---- OpenAPI ---------------------------------------------------------------------------------------------------------


def test_new_viewsets_document_if_match_and_ordering(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths = doc["paths"]
    for url, method in (
        ("/api/v1/people/{id}", "patch"),
        ("/api/v1/teams/{id}", "delete"),
        ("/api/v1/relationships/{id}", "patch"),
        ("/api/v1/integrations/{id}", "delete"),
        ("/api/v1/applications/{id}/instances/{instance_pk}", "patch"),
        ("/api/v1/renewals/{id}", "patch"),
        ("/api/v1/renewals/{id}/cycles/{cycle_pk}", "patch"),
        ("/api/v1/needs-attention/snoozes/{id}", "delete"),
    ):
        op = paths[url][method]
        assert "If-Match" in {p["name"] for p in op.get("parameters", [])}, (url, method)
        assert "412" in op["responses"], (url, method)
    assert "ordering" in {p["name"] for p in paths["/api/v1/people"]["get"]["parameters"]}
    assert {"active", "ordering"} <= {p["name"] for p in paths["/api/v1/people"]["get"]["parameters"]}
    integrations = {p["name"] for p in paths["/api/v1/integrations"]["get"]["parameters"]}
    assert {"source", "target", "protocol", "implemented", "ordering", "cursor"} <= integrations
    export = {p["name"] for p in paths["/api/v1/applications/export.csv"]["get"]["parameters"]}
    assert {"include_archived", "lifecycle", "q"} <= export


# ---- bulk edit holds its rows against If-Match writers (DRF layer A review, carried to B) ---------------------------


def test_bulk_apply_waits_for_an_if_match_writer_holding_the_row(transactional_db):
    """An If-Match write locks the Resource row and writes under the lock (core/drf.check_if_match). Bulk apply plans
    each record's tags from what it reads, so it must take the same row lock before reading: it then waits for the
    writer, plans from the writer's committed tags and keeps them. Planning from an unlocked read would lose the
    writer's tag (the final list would be computed from the tags before the writer committed)."""
    f = make_org("bulkrace")
    app = f["app"]
    org, editor = f["org"], f["users"]["editor"]
    locked, seen, done = threading.Event(), [], []

    def revision_now():
        tag_ids = sorted(
            str(t) for t in ResourceTag.objects.filter(resource_id=app.id).values_list("tag_id", flat=True)
        )
        row = Resource.objects.get(id=app.id)
        return drf.revision(row, row.extension, extra={"tags": tag_ids})

    def if_match_writer():
        try:
            with transaction.atomic():
                list(Resource.objects.select_for_update().filter(pk=app.pk).values_list("pk", flat=True))
                seen.append(revision_now())
                locked.set()
                time.sleep(0.6)  # bulk apply starts meanwhile and must wait
                seen.append(revision_now())  # nothing moved under the writer's lock
                services.update_resource(org, editor, Resource.objects.get(pk=app.pk), {"tags": ["ops"]})
            done.append(("writer", time.monotonic()))
        finally:
            connection.close()

    def bulk_editor():
        try:
            locked.wait(5)
            bulk.apply(org, editor, {"kind": "application", "ids": [str(app.id)], "changes": {"add_tags": ["finance"]}})
            done.append(("bulk", time.monotonic()))
        finally:
            connection.close()

    threads = [threading.Thread(target=if_match_writer), threading.Thread(target=bulk_editor)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert [who for who, _ in sorted(done, key=lambda x: x[1])] == ["writer", "bulk"]
    assert seen[0] == seen[1]
    tags = set(ResourceTag.objects.filter(resource_id=app.id).values_list("tag__name", flat=True))
    assert tags == {"ops", "finance"}  # bulk planned from the writer's committed tags


# ---- Every API timestamp is UTC with Z (docs/API.md "Timestamps") --------------------------------------------------

DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)$")
# Meant in the organization's timezone, so they keep its offset, and exempted by where they are, never by key name:
# (request path, JSON path). An audit row's `at` (the audit log, its rows, and every record's audit list and rows) and
# your preferences' `next_digest_at`. Any other field named `at` must be UTC with Z.
AUDIT_PATH = re.compile(r"^/api/v1/(audit-events|[a-z-]+/[0-9a-f-]{36}/audit)(/[0-9a-f-]{36})?$")
ORG_LOCAL = (
    (AUDIT_PATH, re.compile(r"^\$(\.results\[\d+\])?\.at$")),
    (re.compile(r"^/api/v1/auth/me/preferences$"), re.compile(r"^\$\.next_digest_at$")),
)


def org_local(path: str, where: str) -> bool:
    route = path.split("?", 1)[0]
    return any(r.match(route) and w.match(where) for r, w in ORG_LOCAL)


def offset_timestamps(value, where="$", path=""):
    """Datetime strings in `value` that carry an offset instead of Z, except the org-local ones at `path`."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{where}.{key}"
            if isinstance(item, str) and DATETIME.match(item) and not item.endswith("Z"):
                if not org_local(path, here):
                    found.append(f"{here}={item}")
            else:
                found += offset_timestamps(item, here, path)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += offset_timestamps(item, f"{where}[{i}]", path)
    return found


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_every_timestamp_in_a_response_is_utc_with_z(a):
    """Every viewset read, every unkeyed composite and settings GET and every keyed composite, on the objects the sweep
    factories make: no datetime value carries an offset instead of Z (plain-data builders use core.drf.timestamp)."""
    from core.capabilities import set_edition

    a["org"], _ = set_edition(a["org"], "enterprise")
    client = Client()
    client.force_login(a["users"]["admin"])
    paths = [path for method, path, _a, cls in requests_for(a, a["users"]["admin"]) if method == "get"]
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if cls is not None and tenant_composite(callback) and hasattr(cls, "get") and route.startswith("api/"):
            if PARAM.findall(route):
                kind = getattr(callback, "initkwargs", {}).get("kind")
                paths.append(fill(route, KEYED_COMPOSITES[cls.__name__, kind](a, a["users"]["admin"])))
            else:
                paths.append("/" + route)
    client.post("/api/v1/calendar-feed")
    found, checked = [], 0
    for path in paths:
        response = client.get(path)
        if response.status_code != 200 or not response.get("Content-Type", "").startswith("application/json"):
            continue
        checked += 1
        found += [f"{path}: {hit}" for hit in offset_timestamps(response.json(), path=path)]
    assert checked > 100
    assert found == [], "\n".join(found[:40])


def test_the_timestamp_check_notices_an_offset():
    assert offset_timestamps({"created_at": "2026-11-07T08:00:00+00:00"}) == ["$.created_at=2026-11-07T08:00:00+00:00"]
    local = {"created_at": "2026-11-07T08:00:00.5Z", "at": "2026-11-07T03:00-05:00"}
    assert offset_timestamps(local, path="/api/v1/audit-events/9b2f3c1e-1d2a-4c5b-8e9f-0a1b2c3d4e5f") == []
    rows = {"results": [local]}
    assert offset_timestamps(rows, path="/api/v1/applications/9b2f3c1e-1d2a-4c5b-8e9f-0a1b2c3d4e5f/audit?x=1") == []
    # An `at` anywhere else is checked like any other timestamp, and so is an audit row's other fields.
    assert offset_timestamps(local, path="/api/v1/connections/microsoft") == ["$.at=2026-11-07T03:00-05:00"]
    assert offset_timestamps({"tested": local}, path="/api/v1/audit-events") == ["$.tested.at=2026-11-07T03:00-05:00"]
    assert offset_timestamps({"next_digest_at": "2026-11-08T08:00:00-05:00"}, path="/api/v1/auth/me/preferences") == []
    assert offset_timestamps({"next_digest_at": "2026-11-08T08:00:00-05:00"}, path="/api/v1/auth/me") != []
    assert offset_timestamps({"day": "2026-11-07", "rows": [{"t": "2026-11-07T08:00:00-05:00"}]}) == [
        "$.rows[0].t=2026-11-07T08:00:00-05:00"
    ]
