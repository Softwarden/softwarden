"""Every write route writes its AuditEvent (CLAUDE.md: every write goes through a service that writes an
AuditEvent in the same transaction).

The sweep sends one valid write to every TenantViewSet write route and every tenant RoleAPIView write route found in
the URL resolver (the same enumeration as tests/test_drf_resources.py), as the organization's Admin from a signed-in
session, and asserts that the organization's audit log grew by an event with the action the route is expected to
write. A route either has a case in WRITES or a reason in NOT_AUDITED; a new write route fails
test_every_write_route_is_in_the_audit_sweep until it joins one of them. So dropping the audit call from a service
(the mutants a review tried: a contract line's or a licence pool's deletion, a renewal's archive or deletion, a member's role
change, a SCIM token's revocation, ...) fails here.

Scope: the TenantViewSet and tenant RoleAPIView routes only. Plain function views (the SCIM v2 service, the SAML and
OpenID Connect callbacks, first-run setup) and the platform console's routes are covered by their per-feature tests
(tests/test_sso.py, test_setup.py, test_platform.py) and by the action-naming check below,
not by this sweep.

test_every_audit_action_is_named_by_a_test checks the other direction: every action string the code writes is named
in at least one test file, so an action no test mentions shows up at once."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.urls import get_resolver
from django.utils import timezone

from catalog import services
from core.models import AuditEvent
from tests import vendor_keys
from tests.test_drf_resources import PARAM, fill, tenant_composite, viewset_routes
from tests.test_url_coverage import walk

W = dict[str, Any]
Key = tuple[str, str]

_names = itertools.count(1)


def uniq(prefix: str) -> str:
    return f"{prefix} {next(_names)}"


# ---- the routes --------------------------------------------------------------------------------------------------------


def write_routes() -> dict[Key, tuple[str, str]]:
    """{key: (method, route)} for every write route: (viewset, action) for a TenantViewSet route, (view, method) for a
    tenant RoleAPIView route; a view routed once per kind (a record's end-of-life tracking, ticket links on an
    assessment or a renewal) is `View[kind]`."""
    found: dict[Key, tuple[str, str]] = {}
    for route, cls, actions in viewset_routes():
        for method, action in actions.items():
            if method in ("get", "head", "options"):
                continue
            assert (cls.__name__, action) not in found, (cls.__name__, action)
            found[cls.__name__, action] = (method, route)
    for route, callback in walk(get_resolver().url_patterns):
        if not tenant_composite(callback):
            continue
        kind = getattr(callback, "initkwargs", {}).get("kind")
        name = callback.cls.__name__ + (f"[{kind}]" if kind else "")
        for method in ("post", "put", "patch", "delete"):
            if hasattr(callback.cls, method):
                assert (name, method) not in found, (name, method)
                found[name, method] = (method, route)
    return found


# ---- the cases ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Write:
    """One write: `setup(w)` makes what it acts on and returns the path's IDs (and anything `body` needs); `body(w, ids)`
    is the request body; `expect` is the audit action the write must add to the organization's log."""

    expect: str
    setup: Callable[[W], dict[str, Any]] = lambda w: {}
    body: Callable[[W, dict[str, Any]], Any] = lambda w, ids: {}
    multipart: bool = False
    status: tuple[int, ...] = (200, 201, 202, 204)


def const(value: Any) -> Callable[[W, dict[str, Any]], Any]:
    return lambda w, ids: value


def editor(w: W):
    return w["users"]["editor"]


def admin(w: W):
    return w["users"]["admin"]


# ---- setups ----


def resource(kind: str, **extra: Any) -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        data = {"name": uniq(f"Swept {kind}"), "kind": kind, "lifecycle": "active", **extra}
        if kind == "ai_agent":
            data["models_used"] = [str(w["model"].id)]
        return {"pk": services.create_resource(w["org"], editor(w), data).id}

    return make


def archived(kind: str) -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        ids = resource(kind)(w)
        record = services.Resource.objects.get(id=ids["pk"])
        services.archive_resource(w["org"], editor(w), record)
        return ids

    return make


def integration(w: W) -> dict[str, Any]:
    feed = services.create_integration(
        w["org"],
        editor(w),
        {"name": uniq("Swept feed"), "lifecycle": "active", "source": str(w["app"].id), "target": str(w["job"].id)},
    )
    return {"pk": feed.id}


def archived_integration(w: W) -> dict[str, Any]:
    ids = integration(w)
    services.archive_resource(w["org"], editor(w), services.Resource.objects.get(id=ids["pk"]))
    return ids


def relationship(w: W) -> dict[str, Any]:
    job = services.create_resource(
        w["org"], editor(w), {"name": uniq("Linked job"), "kind": "component", "lifecycle": "active"}
    )
    made = services.create_relationship(
        w["org"], editor(w), {"type": "depends_on", "source": str(w["app"].id), "target": str(job.id)}
    )
    return {"pk": made.id}


def instance(w: W) -> dict[str, Any]:
    made = services.create_instance(w["org"], editor(w), w["app"], {"name": uniq("Instance")})
    return {"pk": w["app"].id, "instance_pk": made.id}


def model_row(make: Callable[[W], Any]) -> Callable[[W], dict[str, Any]]:
    return lambda w: {"pk": make(w).id}


def team(w: W):
    from catalog.models import Team

    return Team.objects.create(organization=w["org"], name=uniq("Team"))


def department(w: W):
    from catalog.models import Department

    return Department.objects.create(organization=w["org"], name=uniq("Department"))


def vendor(w: W):
    from catalog.models import Vendor

    return Vendor.objects.create(organization=w["org"], name=uniq("Vendor"))


def category(w: W):
    from catalog.models import SoftwareCategory

    return SoftwareCategory.objects.create(organization=w["org"], name=uniq("Category"))


def provider(w: W):
    from catalog import ai

    return ai.create_provider(w["org"], editor(w), {"name": uniq("Provider")})


def ai_model(w: W):
    from catalog import ai

    return ai.create_model(w["org"], editor(w), {"provider": str(w["provider"].id), "name": uniq("Model")})


def custom_field(w: W):
    from catalog import customfields

    name = f"swept_{next(_names)}"
    data = {"name": name, "label": "Swept", "type": "text", "applies_to": ["application"]}
    return customfields.create_field(w["org"], admin(w), data)


def relationship_type(w: W):
    from catalog import relationship_types

    return relationship_types.create_type(w["org"], admin(w), {"name": uniq("Type")})


def tag(w: W) -> dict[str, Any]:
    from catalog.models import ResourceTag

    services.update_resource(w["org"], editor(w), w["app"], {"tags": ["finance"]})
    return {"pk": ResourceTag.objects.get(resource=w["app"], tag__name="finance").tag_id}


def saved_filter(w: W):
    from catalog.models import SavedFilter

    return SavedFilter.objects.create(
        organization=w["org"], user=admin(w), kind="application", name=uniq("Filter"), query={}, created_by=admin(w)
    )


def link_attachment(w: W) -> dict[str, Any]:
    from catalog import attachments

    made = attachments.add_link(w["org"], editor(w), w["app"], {"url": "https://example.test/runbook"})
    return {"pk": made.id}


def risk_document(w: W) -> dict[str, Any]:
    from catalog import vendor_risk

    return {"pk": vendor_risk.create_document(w["org"], editor(w), vendor(w), {"kind": "pen_test"}).id}


def quality_exception(w: W) -> dict[str, Any]:
    from catalog import quality

    job = services.create_resource(
        w["org"], editor(w), {"name": uniq("Undocumented"), "kind": "component", "lifecycle": "active"}
    )
    data = {
        "record": str(job.id),
        "rule": "documentation",
        "reason": "Documented elsewhere.",
        "owner": str(w["people"]["dana"].id),
    }
    return {"pk": quality.create_exception(w["org"], editor(w), data).id, "job": job}


def assessment(w: W) -> dict[str, Any]:
    from catalog import assessments

    made = assessments.create_assessment(
        w["org"], editor(w), {"target": str(w["app"].id), "change_type": "change", "title": uniq("Change")}
    )
    return {"pk": made.id}


def person(w: W) -> dict[str, Any]:
    made = services.create_person(w["org"], editor(w), {"name": uniq("Leaving Person")})
    return {"pk": made.id, "name": made.name}


def connection(kind: str) -> Callable[[W], dict[str, Any]]:
    """A connection of `kind` (the route's segment: microsoft, halo, okta, google_workspace)."""

    def make(w: W) -> dict[str, Any]:
        from core import connections

        bodies: dict[str, tuple[Any, dict[str, Any]]] = {
            "microsoft": (connections.MICROSOFT, {"tenant_id": "simulated", "secret": "synthetic-secret"}),
            "halo": (
                connections.HALO,
                {"base_url": "https://halo.simulated.invalid", "client_id": "c", "secret": "s"},
            ),
            "okta": (
                connections.OKTA,
                {"base_url": vendor_keys.OKTA_URL, "client_id": "c", "secret": vendor_keys.pem()},
            ),
            "google_workspace": (
                connections.GOOGLE,
                {"admin_email": vendor_keys.GOOGLE_ADMIN, "secret": vendor_keys.key_file()},
            ),
        }
        which, data = bodies[kind]
        return {"pk": connections.create_connection(w["org"], admin(w), which, {"name": uniq(kind), **data}).id}

    return make


def renewal(w: W, **extra: Any) -> dict[str, Any]:
    from money import renewals

    made = renewals.create_renewal(w["org"], editor(w), {"name": uniq("Renewal"), "auto_renew": "yes", **extra})
    return {"pk": made.id, "cycle_pk": made.cycles.get().id, "obligation": made}


def decided_renewal(w: W) -> dict[str, Any]:
    from money import renewals

    ids = renewal(w)
    renewals.record_decision(w["org"], editor(w), ids["obligation"], ids["cycle_pk"], {"decision": "renew"})
    return ids


def completed_renewal(w: W) -> dict[str, Any]:
    from money import renewals

    ids = decided_renewal(w)
    today = services.org_today(w["org"]).isoformat()
    data = {"completed_on": today, "completion_evidence": "PO 1"}
    renewals.record_completion(w["org"], editor(w), ids["obligation"], ids["cycle_pk"], data)
    return ids


def archived_renewal(w: W) -> dict[str, Any]:
    from money import renewals

    ids = renewal(w)
    renewals.archive_renewal(w["org"], editor(w), ids["obligation"])
    return ids


def contract(w: W, **extra: Any) -> dict[str, Any]:
    from money import contracts

    made = contracts.create_contract(w["org"], editor(w), {"name": uniq("Contract"), **extra})
    return {"pk": made.id, "contract": made}


def archived_contract(w: W) -> dict[str, Any]:
    from money import contracts

    ids = contract(w)
    contracts.archive_contract(w["org"], editor(w), ids["contract"])
    return ids


def contract_line(w: W) -> dict[str, Any]:
    from money import contracts

    ids = contract(w)
    line = contracts.add_item(w["org"], editor(w), ids["contract"], {"record": str(w["app"].id)})
    return {"pk": ids["pk"], "item_pk": line.id}


def pool(w: W, **extra: Any) -> dict[str, Any]:
    from money import contracts

    data = {"application": str(w["app"].id), "sku_name": uniq("SKU"), **extra}
    return {"pk": contracts.create_pool(w["org"], editor(w), data).id}


def licence(w: W, sku: str):
    from discovery.models import Observation

    now = timezone.now()
    return Observation.objects.create(
        organization=w["org"],
        connector=w["connector"],
        kind="license",
        external_id=sku,
        display_name=f"Zz {sku}",
        data={"sku_id": sku, "sku_part_number": sku.upper(), "seats_purchased": 10, "seats_assigned": 3},
        fingerprint=sku,
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )


def pool_with_proposal(w: W) -> dict[str, Any]:
    sku = f"sku-{next(_names)}"
    ids = pool(w, sku_id=sku, seats_purchased=5)
    return {**ids, "observation_pk": licence(w, sku).id}


def unbound(w: W) -> dict[str, Any]:
    return {"observation_pk": licence(w, f"sku-{next(_names)}").id}


def unbound_pool(w: W) -> dict[str, Any]:
    return {**pool(w), "observation": licence(w, f"sku-{next(_names)}").id}


def snooze(w: W) -> dict[str, Any]:
    from notify.models import NotificationSnooze

    record = services.create_resource(w["org"], editor(w), {"name": uniq("Snoozed"), "lifecycle": "active"})
    made = NotificationSnooze.objects.create(
        organization=w["org"],
        user=admin(w),
        subject_type="ownership_gaps",
        subject_id=str(record.id),
        kind="snooze",
        until=services.org_today(w["org"]) + timedelta(days=3),
    )
    return {"pk": made.id}


def digest(w: W) -> dict[str, Any]:
    from tests.conftest import digest_row

    return {"pk": digest_row(w["org"], editor(w), "failed", attempts=4).id}


def reminder(status: str = "open") -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        from datetime import date

        from notify.models import Reminder

        record = services.create_resource(w["org"], editor(w), {"name": uniq("Reminded"), "lifecycle": "active"})
        made = Reminder.objects.create(
            organization=w["org"],
            source_type="periodic_review",
            source_id=record.id,
            resource=record,
            due_date=date(2027, 1, 31),
            title=record.name,
            label="Periodic review due",
        )
        if status == "acknowledged":
            from notify import services as notify_services

            notify_services.acknowledge_reminder(w["org"], editor(w), made)
        return {"pk": made.id}

    return make


def destination(model_name: str) -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        from notify import channel_services, models

        model = getattr(models, model_name)
        data: dict[str, Any] = {"name": uniq("Destination"), "url": "https://example.test/flows/1?sig=x"}
        if model_name == "WebhookDestination":
            data = {"name": uniq("Hook"), "url": "https://example.test/hook", "signing_secret": "s" * 32}
        return {"pk": channel_services.create_destination(w["org"], admin(w), model, data).id}

    return make


def webhook(w: W) -> dict[str, Any]:
    from notify import channel_services
    from notify.models import WebhookDestination

    data = {
        "name": uniq("Webhook"),
        "url": "https://example.test/hook",
        "signing_secret": "s" * 32,
        "events": ["record.*"],
    }
    made = channel_services.create_destination(w["org"], admin(w), WebhookDestination, data, subscriptions=True)
    return {"pk": made.id}


def failed_delivery(w: W) -> dict[str, Any]:
    from notify import deliveries
    from notify.models import Delivery

    made, _ = deliveries.enqueue(
        w["org"],
        channel="email",
        dedupe_key=f"sweep:{next(_names)}",
        subject="Sweep",
        content={"text": "Hi", "html": "<p>Hi</p>"},
        recipient=admin(w).email,
    )
    Delivery.objects.filter(pk=made.pk).update(status="failed", last_error="Refused by the server.")
    return {"pk": made.id}


def webhook_delivery(w: W) -> dict[str, Any]:
    from notify import deliveries
    from notify.models import Delivery, WebhookDestination

    made, _ = deliveries.enqueue(
        w["org"],
        channel="webhook",
        kind="event",
        dedupe_key=f"sweep:hook:{next(_names)}",
        subject="record.updated",
        content={"event": "record.updated", "id": "5a3c3f8e-5d0b-4f62-9a8e-0d9b5b1d2c11", "data": {"record": {}}},
        webhook_destination=WebhookDestination.objects.get(id=webhook(w)["pk"]),
    )
    Delivery.objects.filter(pk=made.pk).update(status="failed", last_error="500")
    return {"pk": made.id}


def notification_rule(w: W) -> dict[str, Any]:
    from notify import rule_services

    data = {"name": uniq("Rule"), "source_types": ["contract"], "lead_times_days": [30]}
    return {"pk": rule_services.create_rule(w["org"], admin(w), data).id}


def subscription(w: W) -> dict[str, Any]:
    from catalog.models import SavedFilter
    from core.models import Membership
    from notify.models import SavedViewSubscription

    view = SavedFilter.objects.create(
        organization=w["org"], user=admin(w), shared=True, kind="application", name=uniq("Subscribed"), query={}
    )
    membership = Membership.objects.get(organization=w["org"], user=admin(w))
    made = SavedViewSubscription.objects.create(
        organization=w["org"], saved_view=view, membership=membership, weekday=2
    )
    return {"pk": made.id, "view": view.id}


def connector(w: W) -> dict[str, Any]:
    from discovery import engine

    made = engine.create_connector(
        w["org"],
        admin(w),
        {"name": uniq("Connector"), "source": "entra", "microsoft_connection": str(w["connection"].id)},
    )
    return {"pk": made.id}


def observation(state: str = "new") -> Callable[[W], dict[str, Any]]:
    """An application observation of the swept connector, in `state`: new (undecided), linked to a record, dismissed,
    created (a record made from it), or gone from the source after being linked."""

    def make(w: W) -> dict[str, Any]:
        from discovery import engine
        from discovery.models import Observation

        now = timezone.now()
        name = uniq("Seen app")
        found = Observation.objects.create(
            organization=w["org"],
            connector=w["connector"],
            kind="application",
            external_id=name.lower().replace(" ", "-"),
            display_name=name,
            data={"description": f"{name} for the team", "publisher": "Contoso"},
            fingerprint=name,
            first_seen_at=now,
            last_seen_at=now,
            last_changed_at=now,
        )
        ids: dict[str, Any] = {"pk": found.id}
        if state in ("linked", "gone"):
            record = services.create_resource(
                w["org"], editor(w), {"name": uniq("Curated"), "description": "Old", "lifecycle": "active"}
            )
            engine.link(w["org"], editor(w), found, {"resource": str(record.id)})
            ids["record"] = record.id
        elif state == "dismissed":
            engine.dismiss(w["org"], editor(w), found, {"reason": "Not ours."})
        if state == "gone":
            Observation.objects.filter(pk=found.pk).update(gone_at=now)
        return ids

    return make


def suggested(action: str) -> Callable[[W], dict[str, Any]]:
    """A new observation with a ready assistant suggestion to link it to the fixture application."""

    def make(w: W) -> dict[str, Any]:
        from discovery.models import AssistantSuggestion, Observation

        ids = observation()(w)
        found = Observation.objects.get(id=ids["pk"])
        made = AssistantSuggestion.objects.create(
            organization=w["org"],
            observation=found,
            fingerprint=found.fingerprint,
            model="claude-sonnet-5-5",
            state="ready",
            action=action,
            target=w["app"],
            reason="Same name.",
        )
        return {**ids, "suggestion": str(made.id)}

    return make


def drifted(w: W) -> dict[str, Any]:
    """A linked observation whose description differs from its record's."""
    return observation("linked")(w)


def import_source(w: W) -> dict[str, Any]:
    from imports import importer

    return {"pk": importer.create_source(w["org"], admin(w), {"name": uniq("Source"), "kind": "application"}).id}


def import_csv() -> bytes:
    return f"name,lifecycle\nSwept import {next(_names)},active\n".encode()


def import_run(w: W) -> dict[str, Any]:
    from imports import importer

    return {"pk": importer.create_run(w["org"], editor(w), "application", "x.csv", import_csv()).id}


def unmapped_import(w: W) -> dict[str, Any]:
    """A run from an import source whose headings aren't the template's, so it waits for its column mapping."""
    from imports import importer
    from imports.models import ImportSource

    source = ImportSource.objects.get(id=import_source(w)["pk"])
    data = f"App name,Status\nSwept mapped {next(_names)},active\n".encode()
    run = importer.create_run(w["org"], editor(w), "application", "mapped.csv", data, source=source)
    return {"pk": run.id}


def committed_import(w: W) -> dict[str, Any]:
    from imports import importer
    from imports.models import ImportRun

    ids = import_run(w)
    importer.commit_run(w["org"], editor(w), ImportRun.objects.get(id=ids["pk"]))
    return ids


def ticket_connection(w: W) -> dict[str, Any]:
    """The organization's Jira connection on the simulated site (one per system), tested and allowed to create
    tickets; made when there is none."""
    from tickets import services as ticket_services
    from tickets import simulator
    from tickets.models import TicketConnection

    found = TicketConnection.objects.filter(organization=w["org"], system="jira").first()
    if found is None:
        data = {
            "system": "jira",
            "base_url": simulator.JIRA_URL,
            "username": "it@example.test",
            "secret": "jira-api-token-synthetic",
            "create_enabled": True,
            "create_target": "ITOPS",
        }
        found = ticket_services.run_test(
            w["org"], admin(w), ticket_services.create_connection(w["org"], admin(w), data)
        )
    return {"pk": found.id}


def api_token(w: W) -> dict[str, Any]:
    from api import tokens
    from core.models import Membership

    owner = Membership.objects.get(organization=w["org"], user=editor(w))
    expires = services.org_today(w["org"]) + timedelta(days=30)
    return {"pk": tokens.create_token(w["org"], admin(w), owner, uniq("Token"), True, expires)[0].id}


def member(w: W) -> dict[str, Any]:
    """A viewer of the swept organization who is also in another organization (a removal keeps them one)."""
    from django.contrib.auth import get_user_model

    from core.models import Membership, Organization

    n = next(_names)
    user = get_user_model().objects.create_user(username=f"member{n}@x.test", email=f"member{n}@x.test")
    Membership.objects.create(user=user, organization=w["org"], role="viewer")
    Membership.objects.create(user=user, organization=Organization.objects.create(name=f"Other {n}"), role="viewer")
    return {"pk": user.id, "user": user}


def solo_member(w: W) -> dict[str, Any]:
    """A viewer whose only organization is the swept one (an Admin may reset their password and identities)."""
    from django.contrib.auth import get_user_model

    from core.models import Membership

    n = next(_names)
    # A password account: an Admin can't reset one that signs in only through single sign-on.
    user = get_user_model().objects.create_user(
        username=f"solo{n}@x.test", email=f"solo{n}@x.test", password="solo-member-passphrase"
    )
    Membership.objects.create(user=user, organization=w["org"], role="viewer")
    return {"pk": user.id, "user": user}


def solo_identity(w: W) -> dict[str, Any]:
    from core.models import UserIdentity

    ids = solo_member(w)
    identity = UserIdentity.objects.create(user=ids["user"], issuer="https://idp.example.test", subject=str(ids["pk"]))
    return {**ids, "identity_id": identity.id}


def invitation(w: W) -> dict[str, Any]:
    from catalog import invitations

    made, _ = invitations.create_invitation(
        w["org"], admin(w), {"email": f"invited{next(_names)}@x.test", "role": "viewer"}
    )
    return {"pk": made.id}


def pending_access(w: W) -> dict[str, Any]:
    from core.models import PendingAccess

    n, now = next(_names), timezone.now()
    row = PendingAccess.objects.create(
        organization=w["org"], issuer="https://idp.example.test", subject=f"p{n}", email=f"p{n}@x.test",
        first_seen=now, last_seen=now,
    )  # fmt: skip
    return {"pk": row.id}


def ticket_link(kind: str, with_link: bool = True) -> Callable[[W], dict[str, Any]]:
    """An assessment or a renewal, with one link to an In Progress Jira issue on the simulated site unless
    `with_link` is off; the organization's Jira connection reads it (refresh) and creates tickets (create-ticket)."""

    def make(w: W) -> dict[str, Any]:
        from tickets import services as ticket_services
        from tickets import simulator

        ticket_connection(w)
        if kind == "assessment":
            from catalog.models import ChangeAssessment

            parent: Any = ChangeAssessment.objects.get(id=assessment(w)["pk"])
        else:
            parent = renewal(w)["obligation"]
        if not with_link:
            return {"pk": parent.id}
        link = ticket_services.create_link(
            w["org"],
            editor(w),
            ticket_services.Target(kind, parent),
            {"url": f"{simulator.JIRA_URL}/browse/ITOPS-2"},  # In Progress in the simulator
        )
        return {"pk": parent.id, "link_pk": link.id}

    return make


def tracked(kind: str) -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        from eol import services as eol_services

        ids = resource(kind)(w)
        record = services.Resource.objects.get(id=ids["pk"])
        eol_services.track(w["org"], editor(w), record, {"product": "postgresql", "version": "16.4"})
        return ids

    return make


def tracked_app_instance(w: W) -> dict[str, Any]:
    """An instance of an application whose end of life is tracked (an instance takes its product from it)."""
    from eol import services as eol_services

    ids = instance(w)
    eol_services.track(w["org"], editor(w), w["app"], {"product": "postgresql"})
    return ids


def tracked_instance(w: W) -> dict[str, Any]:
    from catalog.models import ApplicationInstance
    from eol import services as eol_services

    ids = instance(w)
    found = ApplicationInstance.objects.get(id=ids["instance_pk"])
    eol_services.track(w["org"], editor(w), w["app"], {"product": "postgresql"})
    eol_services.track_instance(w["org"], editor(w), found, {"version": "13.2"})
    return ids


def catalog_tree(w: W) -> dict[str, Any]:
    from catalog_code import bundle, engine, fmt

    files = engine.render_tree(w["org"])
    slug = f"swept-{next(_names)}"
    doc = {
        "apiVersion": "softwarden/v1",
        "kind": "Application",
        "metadata": {"slug": slug, "name": f"Swept {slug}"},
        "spec": {"lifecycle": "proposed"},
    }
    files = {**files, f"applications/{slug}.yaml": fmt.dump(doc)}
    return {"zip": bundle.write_zip(files)}


def planned_tree(w: W) -> dict[str, Any]:
    ids = catalog_tree(w)
    client = Client()
    client.force_login(admin(w))
    plan = client.post("/api/v1/catalog-code/plan", {"file": SimpleUploadedFile("catalog.zip", ids["zip"])})
    assert plan.status_code == 200, plan.content
    return {**ids, "plan_hash": plan.json()["plan_hash"]}


def reassignable(w: W) -> dict[str, Any]:
    leaving = services.create_person(w["org"], editor(w), {"name": uniq("Leaving")})
    services.create_resource(
        w["org"], editor(w), {"name": uniq("Owned"), "lifecycle": "active", "technical_owner": str(leaving.id)}
    )
    return {"pk": leaving.id}


def sso_draft(tested: bool) -> Callable[[W], dict[str, Any]]:
    def make(w: W) -> dict[str, Any]:
        from core.models import SsoSettings
        from tests.sso_helpers import activate

        row = activate(w["org"], "okta")
        SsoSettings.objects.filter(pk=row.pk).update(draft_test_ok=tested, draft_tested_revision=row.draft_revision)
        return {"draft_revision": row.draft_revision}

    return make


def pending_sign_in_domain(w: W) -> dict[str, Any]:
    """A pending sign-in domain whose TXT record the simulated resolver answers (core/dns_txt.SIMULATED_SUFFIX)."""
    from core.models import SsoDomain

    row = SsoDomain.objects.create(organization=w["org"], domain="swept.dns-verified.test", challenge="sweep")
    return {"domain": row.domain}


def calendar_feed(w: W) -> dict[str, Any]:
    from money import calendar_feeds as feeds

    if feeds.live_feed(w["org"], admin(w)) is None:
        feeds.create_feed(w["org"], admin(w))
    return {}


def email_settings(w: W) -> dict[str, Any]:
    from notify import channel_services

    channel_services.update_email_settings(
        w["org"],
        admin(w),
        {"transport": "smtp", "smtp_host": "smtp.example.test", "from_address": "it@example.test"},
    )
    return {}


def assistant_on(w: W) -> dict[str, Any]:
    from discovery import assistant

    assistant.update_settings(w["org"], admin(w), {"enabled": True, "api_key": "sk-ant-sweep-key-0001"})
    return {}


def cloud(w: W) -> dict[str, Any]:
    """Organization self-deletion is a cloud capability."""
    from core.capabilities import set_edition

    w["org"], _ = set_edition(w["org"], "cloud")
    return {}


def today_plus(days: int) -> Callable[[W, dict[str, Any]], Any]:
    return lambda w, ids: (services.org_today(w["org"]) + timedelta(days=days)).isoformat()


REVIEW = {"evidence": "Confirmed by email"}


def review(w: W, ids: dict[str, Any]) -> dict[str, Any]:
    return {"confirmed_by": str(w["people"]["dana"].id), **REVIEW}


def link_body(w: W, ids: dict[str, Any]) -> dict[str, Any]:
    return {"url": "https://example.test/runbook", "title": "Runbook", "category": "sop_runbook"}


def record_writes(kind: str, view: str) -> dict[Key, Write]:
    """The writes every catalog record viewset has (applications, components, AI agents): each a `resource.*`
    event."""
    extra = {"component_type": "script"} if kind == "component" else {}

    def create(w: W, ids: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = {"name": uniq(f"New {kind}"), "lifecycle": "active", **extra}
        if kind == "ai_agent":
            data["models_used"] = [str(w["model"].id)]
        return data

    return {
        (view, "create"): Write("resource.created", body=create),
        (view, "partial_update"): Write("resource.updated", resource(kind), const({"description": "Changed"})),
        (view, "destroy"): Write("resource.deleted", resource(kind)),
        (view, "archive"): Write("resource.archived", resource(kind)),
        (view, "restore"): Write("resource.restored", archived(kind)),
    }


def connection_writes(
    view: str,
    kind: str,
    body: Callable[[W, dict[str, Any]], Any],
    created_updated: tuple[str, str],
    deleted_tested: tuple[str, str],
) -> dict[Key, Write]:
    """A connection viewset's writes, each with its literal action (the noun is the connection kind's)."""
    return {
        (view, "create"): Write(created_updated[0], body=body),
        (view, "partial_update"): Write(created_updated[1], connection(kind), const({"notes": "Checked"})),
        (view, "destroy"): Write(deleted_tested[0], connection(kind)),
        (view, "test"): Write(deleted_tested[1], connection(kind)),
    }


WRITES: dict[Key, Write] = {
    # ---- accounts ----
    ("UserViewSet", "create"): Write(
        "user.invited", body=lambda w, ids: {"email": f"new{next(_names)}@x.test", "role": "viewer"}
    ),
    ("UserViewSet", "partial_update"): Write("user.updated", member, const({"role": "editor"})),
    ("UserViewSet", "destroy"): Write("user.removed", member),
    ("UserViewSet", "reset_password"): Write("user.password_reset", solo_member),
    ("UserViewSet", "identity"): Write("user.identity_unlinked", solo_identity),
    ("UserViewSet", "unlink_identity"): Write("user.identity_unlinked", solo_identity),
    ("InvitationViewSet", "create"): Write(
        "user.invited", body=lambda w, ids: {"email": f"inv{next(_names)}@x.test", "role": "viewer"}
    ),
    ("InvitationViewSet", "revoke"): Write("invitation.revoked", invitation),
    ("InvitationViewSet", "resend"): Write("invitation.resent", invitation),
    ("PendingAccessViewSet", "destroy"): Write("pending_access.declined", pending_access),
    ("PendingAccessViewSet", "approve"): Write("pending_access.approved", pending_access, const({"role": "viewer"})),
    ("OrgExportViewSet", "create"): Write("organization.export_requested"),
    ("ApiTokenViewSet", "create"): Write(
        "token.created",
        body=lambda w, ids: {"name": uniq("Token"), "user": editor(w).id, "expires_on": today_plus(30)(w, ids)},
    ),
    ("ApiTokenViewSet", "revoke"): Write("token.revoked", api_token),
    # ---- directory ----
    ("PersonViewSet", "create"): Write("person.created", body=lambda w, ids: {"name": uniq("Person")}),
    ("PersonViewSet", "partial_update"): Write("person.updated", person, const({"title": "Changed"})),
    ("PersonViewSet", "anonymize"): Write("person.anonymized", person, lambda w, ids: {"confirm_name": ids["name"]}),
    ("TeamViewSet", "create"): Write("team.created", body=lambda w, ids: {"name": uniq("Team")}),
    ("TeamViewSet", "partial_update"): Write("team.updated", model_row(team), const({"description": "Ops"})),
    ("TeamViewSet", "destroy"): Write("team.deleted", model_row(team)),
    ("DepartmentViewSet", "create"): Write("department.created", body=lambda w, ids: {"name": uniq("Department")}),
    ("DepartmentViewSet", "partial_update"): Write(
        "department.updated", model_row(department), lambda w, ids: {"name": uniq("Renamed")}
    ),
    ("DepartmentViewSet", "destroy"): Write("department.deleted", model_row(department)),
    ("VendorViewSet", "create"): Write("vendor.created", body=lambda w, ids: {"name": uniq("Vendor")}),
    ("VendorViewSet", "partial_update"): Write("vendor.updated", model_row(vendor), const({"notes": "Changed"})),
    ("VendorViewSet", "destroy"): Write("vendor.deleted", model_row(vendor)),
    ("VendorAttachmentViewSet", "create"): Write("attachment.created", model_row(vendor), link_body),
    ("VendorRiskProfileViewSet", "partial_update"): Write(
        "vendor_risk.created", model_row(vendor), const({"risk_tier": "high"})
    ),
    ("VendorRiskDocumentsOfVendorViewSet", "create"): Write(
        "vendor_risk_document.created",
        model_row(vendor),
        const({"kind": "soc2", "issued_on": "2026-01-15", "validity_months": 12}),
    ),
    ("VendorRiskDocumentViewSet", "partial_update"): Write(
        "vendor_risk_document.updated", risk_document, const({"title": "Bridge letter"})
    ),
    ("VendorRiskDocumentViewSet", "destroy"): Write("vendor_risk_document.deleted", risk_document),
    ("SoftwareCategoryViewSet", "create"): Write("category.created", body=lambda w, ids: {"name": uniq("Category")}),
    ("SoftwareCategoryViewSet", "partial_update"): Write(
        "category.updated", model_row(category), const({"description": "Changed"})
    ),
    ("SoftwareCategoryViewSet", "destroy"): Write("category.deleted", model_row(category)),
    # ---- AI, custom fields, relationship types, tags, saved filters ----
    ("AIProviderViewSet", "create"): Write("ai_provider.created", body=lambda w, ids: {"name": uniq("Provider")}),
    ("AIProviderViewSet", "partial_update"): Write(
        "ai_provider.updated", model_row(provider), const({"website": "https://ai.example.test"})
    ),
    ("AIProviderViewSet", "destroy"): Write("ai_provider.deleted", model_row(provider)),
    ("AIModelViewSet", "create"): Write(
        "ai_model.created", body=lambda w, ids: {"provider": str(w["provider"].id), "name": uniq("Model")}
    ),
    ("AIModelViewSet", "partial_update"): Write("ai_model.updated", model_row(ai_model), const({"version": "2"})),
    ("AIModelViewSet", "destroy"): Write("ai_model.deleted", model_row(ai_model)),
    ("CustomFieldViewSet", "create"): Write(
        "custom_field.created",
        body=lambda w, ids: {
            "name": f"new_{next(_names)}",
            "label": "New",
            "type": "text",
            "applies_to": ["application"],
        },
    ),
    ("CustomFieldViewSet", "partial_update"): Write(
        "custom_field.updated", model_row(custom_field), const({"label": "Relabelled"})
    ),
    ("CustomFieldViewSet", "destroy"): Write("custom_field.deleted", model_row(custom_field)),
    ("RelationshipTypeViewSet", "create"): Write(
        "relationship_type.created", body=lambda w, ids: {"name": uniq("Type")}
    ),
    ("RelationshipTypeViewSet", "partial_update"): Write(
        "relationship_type.updated", model_row(relationship_type), const({"description": "Changed"})
    ),
    ("RelationshipTypeViewSet", "destroy"): Write("relationship_type.deleted", model_row(relationship_type)),
    ("TagViewSet", "partial_update"): Write("tag.updated", tag, const({"description": "Finance systems"})),
    ("SavedFilterViewSet", "create"): Write(
        "saved_filter.created", body=lambda w, ids: {"name": uniq("Filter"), "kind": "application", "query": {}}
    ),
    ("SavedFilterViewSet", "partial_update"): Write(
        "saved_filter.updated", model_row(saved_filter), lambda w, ids: {"name": uniq("Renamed")}
    ),
    ("SavedFilterViewSet", "destroy"): Write("saved_filter.deleted", model_row(saved_filter)),
    # ---- catalog records ----
    **record_writes("application", "ApplicationViewSet"),
    **record_writes("component", "ComponentViewSet"),
    **record_writes("ai_agent", "AIAgentViewSet"),
    ("ApplicationReviewViewSet", "create"): Write("review.recorded", resource("application"), review),
    ("ComponentReviewViewSet", "create"): Write("review.recorded", resource("component"), review),
    ("AIAgentReviewViewSet", "create"): Write("review.recorded", resource("ai_agent"), review),
    ("InstanceViewSet", "create"): Write(
        "instance.created", lambda w: {"pk": w["app"].id}, lambda w, ids: {"name": uniq("Instance")}
    ),
    ("InstanceViewSet", "partial_update"): Write("instance.updated", instance, const({"status": "degraded"})),
    ("InstanceViewSet", "destroy"): Write("instance.deleted", instance),
    ("ApplicationAttachmentViewSet", "create"): Write("attachment.created", resource("application"), link_body),
    ("ComponentAttachmentViewSet", "create"): Write("attachment.created", resource("component"), link_body),
    ("AIAgentAttachmentViewSet", "create"): Write("attachment.created", resource("ai_agent"), link_body),
    ("IntegrationAttachmentViewSet", "create"): Write("attachment.created", integration, link_body),
    ("ContractAttachmentViewSet", "create"): Write("attachment.created", contract, link_body),
    ("AttachmentViewSet", "partial_update"): Write("attachment.updated", link_attachment, const({"title": "Second"})),
    ("AttachmentViewSet", "destroy"): Write("attachment.deleted", link_attachment),
    ("IntegrationViewSet", "create"): Write(
        "resource.created",
        body=lambda w, ids: {
            "name": uniq("Feed"),
            "lifecycle": "active",
            "source": str(w["app"].id),
            "target": str(w["job"].id),
        },
    ),
    ("IntegrationViewSet", "partial_update"): Write("resource.updated", integration, const({"protocol": "REST"})),
    ("IntegrationViewSet", "destroy"): Write("resource.deleted", integration),
    ("IntegrationViewSet", "archive"): Write("resource.archived", integration),
    ("IntegrationViewSet", "restore"): Write("resource.restored", archived_integration),
    ("IntegrationViewSet", "confirmations"): Write("link.confirmed", integration, review),
    ("RelationshipViewSet", "create"): Write(
        "relationship.created",
        body=lambda w, ids: {"type": "depends_on", "source": str(w["job"].id), "target": str(w["app"].id)},
    ),
    ("RelationshipViewSet", "partial_update"): Write(
        "relationship.updated", relationship, const({"purpose": "Runs nightly"})
    ),
    ("RelationshipViewSet", "destroy"): Write("relationship.deleted", relationship),
    ("RelationshipViewSet", "confirmations"): Write("link.confirmed", relationship, review),
    ("QualityExceptionViewSet", "create"): Write(
        "quality_exception.created",
        lambda w: {
            "job": services.create_resource(w["org"], editor(w), {"name": uniq("Undocumented"), "lifecycle": "active"})
        },
        lambda w, ids: {
            "record": str(ids["job"].id),
            "rule": "documentation",
            "reason": "Documented elsewhere.",
            "owner": str(w["people"]["dana"].id),
        },
    ),
    ("QualityExceptionViewSet", "partial_update"): Write(
        "quality_exception.updated", quality_exception, const({"reason": "Documented in section 4."})
    ),
    ("QualityExceptionViewSet", "destroy"): Write("quality_exception.deleted", quality_exception),
    ("AssessmentViewSet", "create"): Write(
        "assessment.created",
        body=lambda w, ids: {"target": str(w["app"].id), "change_type": "change", "title": uniq("Change")},
    ),
    ("AssessmentViewSet", "partial_update"): Write("assessment.updated", assessment, const({"findings": "Checked"})),
    ("AssessmentViewSet", "status"): Write("assessment.status_changed", assessment, const({"status": "in_review"})),
    ("AssessmentViewSet", "refresh"): Write("assessment.snapshot_refreshed", assessment),
    # ---- connections ----
    **connection_writes(
        "MicrosoftConnectionViewSet",
        "microsoft",
        lambda w, ids: {"name": uniq("Entra"), "tenant_id": "simulated", "secret": "synthetic-secret"},
        ("microsoft_connection.created", "microsoft_connection.updated"),
        ("microsoft_connection.deleted", "microsoft_connection.tested"),
    ),
    **connection_writes(
        "HaloConnectionViewSet",
        "halo",
        lambda w, ids: {
            "name": uniq("Halo"),
            "base_url": "https://halo.simulated.invalid",
            "client_id": "c",
            "secret": "s",
        },
        ("halo_connection.created", "halo_connection.updated"),
        ("halo_connection.deleted", "halo_connection.tested"),
    ),
    **connection_writes(
        "OktaConnectionViewSet",
        "okta",
        lambda w, ids: {
            "name": uniq("Okta"),
            "base_url": vendor_keys.OKTA_URL,
            "client_id": "c",
            "secret": vendor_keys.pem(),
        },
        ("okta_connection.created", "okta_connection.updated"),
        ("okta_connection.deleted", "okta_connection.tested"),
    ),
    **connection_writes(
        "GoogleWorkspaceConnectionViewSet",
        "google_workspace",
        lambda w, ids: {
            "name": uniq("Workspace"),
            "admin_email": vendor_keys.GOOGLE_ADMIN,
            "secret": vendor_keys.key_file(),
        },
        ("google_workspace_connection.created", "google_workspace_connection.updated"),
        ("google_workspace_connection.deleted", "google_workspace_connection.tested"),
    ),
    ("TicketConnectionViewSet", "create"): Write(
        "ticket_connection.created",
        body=lambda w, ids: {
            "system": "servicenow",
            "base_url": "https://snow.simulated.invalid",
            "username": "it@example.test",
            "secret": "synthetic-secret",
        },
    ),
    ("TicketConnectionViewSet", "partial_update"): Write(
        "ticket_connection.updated", ticket_connection, const({"notes": "Checked"})
    ),
    ("TicketConnectionViewSet", "destroy"): Write("ticket_connection.deleted", ticket_connection),
    ("TicketConnectionViewSet", "test"): Write("ticket_connection.tested", ticket_connection),
    # ---- money ----
    ("RenewalViewSet", "create"): Write("renewal.created", body=lambda w, ids: {"name": uniq("Renewal")}),
    ("RenewalViewSet", "partial_update"): Write("renewal.updated", renewal, const({"auto_renew": "no"})),
    ("RenewalViewSet", "destroy"): Write("renewal.deleted", renewal),
    ("RenewalViewSet", "archive"): Write("renewal.archived", renewal),
    ("RenewalViewSet", "restore"): Write("renewal.restored", archived_renewal),
    ("RenewalViewSet", "cycles"): Write("renewal_cycle.established", completed_renewal),
    ("RenewalViewSet", "cycle"): Write("renewal_cycle.updated", renewal, const({"renewal_date": "2027-03-31"})),
    ("RenewalViewSet", "decision"): Write("renewal_cycle.decided", renewal, const({"decision": "renew"})),
    ("RenewalViewSet", "completion"): Write(
        "renewal_cycle.completed",
        decided_renewal,
        lambda w, ids: {"completed_on": services.org_today(w["org"]).isoformat(), "completion_evidence": "PO 2"},
    ),
    ("ContractViewSet", "create"): Write("contract.created", body=lambda w, ids: {"name": uniq("Contract")}),
    ("ContractViewSet", "partial_update"): Write("contract.updated", contract, const({"po_number": "PO-1"})),
    ("ContractViewSet", "destroy"): Write("contract.deleted", contract),
    ("ContractViewSet", "archive"): Write("contract.archived", contract),
    ("ContractViewSet", "restore"): Write("contract.restored", archived_contract),
    ("ContractViewSet", "renew"): Write(
        "contract.decided", lambda w: contract(w, end_date="2027-03-31"), const({"decision": "renew"})
    ),
    ("ContractLineViewSet", "create"): Write(
        "contract_item.created", contract, lambda w, ids: {"record": str(w["app"].id)}
    ),
    ("ContractLineViewSet", "partial_update"): Write(
        "contract_item.updated", contract_line, const({"allocation_percent": "10"})
    ),
    ("ContractLineViewSet", "destroy"): Write("contract_item.deleted", contract_line),
    ("LicensePoolViewSet", "create"): Write(
        "license_pool.created", body=lambda w, ids: {"application": str(w["app"].id), "sku_name": uniq("SKU")}
    ),
    ("LicensePoolViewSet", "partial_update"): Write("license_pool.updated", pool, const({"seats_purchased": 5})),
    ("LicensePoolViewSet", "destroy"): Write("license_pool.deleted", pool),
    ("LicensePoolViewSet", "apply_seat_count"): Write("license_pool.updated", pool_with_proposal),
    ("LicensePoolViewSet", "dismiss_seat_count"): Write(
        "license_pool.seat_count_dismissed", pool_with_proposal, const({"reason": "Counted by hand."})
    ),
    ("LicensePoolViewSet", "create_from_unbound"): Write(
        "license_pool.created", unbound, lambda w, ids: {"application": str(w["app"].id)}
    ),
    ("LicensePoolViewSet", "bind"): Write(
        "license_pool.updated", unbound_pool, lambda w, ids: {"observation": str(ids["observation"])}
    ),
    # ---- notifications ----
    ("SnoozeViewSet", "create"): Write(
        "notification.snoozed",
        resource("application"),
        lambda w, ids: {
            "subject_type": "ownership_gaps",
            "subject_id": str(ids["pk"]),
            "kind": "snooze",
            "until": today_plus(3)(w, ids),
        },
    ),
    ("SnoozeViewSet", "destroy"): Write("notification.unsnoozed", snooze),
    ("DigestViewSet", "retry"): Write("digest.retried", digest),
    ("ReminderViewSet", "snooze"): Write(
        "reminder.snoozed", reminder(), lambda w, ids: {"until": today_plus(3)(w, ids)}
    ),
    ("ReminderViewSet", "acknowledge"): Write("reminder.acknowledged", reminder()),
    ("ReminderViewSet", "reopen"): Write("reminder.reopened", reminder("acknowledged")),
    ("ReminderViewSet", "resolve"): Write(
        "reminder.resolved", reminder(), const({"kind": "done", "note": "Reviewed."})
    ),
    ("TeamsDestinationViewSet", "create"): Write(
        "teams_destination.created",
        body=lambda w, ids: {"name": uniq("Teams"), "url": "https://example.test/flows/2?sig=x"},
    ),
    ("TeamsDestinationViewSet", "partial_update"): Write(
        "teams_destination.updated", destination("TeamsDestination"), lambda w, ids: {"name": uniq("Renamed")}
    ),
    ("TeamsDestinationViewSet", "destroy"): Write("teams_destination.deleted", destination("TeamsDestination")),
    ("TeamsDestinationViewSet", "test_send"): Write("teams_destination.tested", destination("TeamsDestination")),
    ("WebhookDestinationViewSet", "create"): Write(
        "webhook_destination.created",
        body=lambda w, ids: {"name": uniq("Hook"), "url": "https://example.test/h2", "signing_secret": "s" * 32},
    ),
    ("WebhookDestinationViewSet", "partial_update"): Write(
        "webhook_destination.updated", destination("WebhookDestination"), const({"enabled": False})
    ),
    ("WebhookDestinationViewSet", "destroy"): Write("webhook_destination.deleted", destination("WebhookDestination")),
    ("WebhookDestinationViewSet", "test_send"): Write("webhook_destination.tested", destination("WebhookDestination")),
    ("NotificationLogViewSet", "resend"): Write("notification.resent", failed_delivery),
    ("WebhookViewSet", "create"): Write(
        "webhook_destination.created",
        body=lambda w, ids: {
            "name": uniq("Webhook"),
            "url": "https://example.test/w2",
            "signing_secret": "w" * 32,
            "events": ["record.*"],
        },
    ),
    ("WebhookViewSet", "partial_update"): Write(
        "webhook_destination.updated", webhook, const({"events": ["record.updated"]})
    ),
    ("WebhookViewSet", "destroy"): Write("webhook_destination.deleted", webhook),
    ("WebhookViewSet", "ping"): Write("webhook_destination.tested", webhook),
    ("WebhookViewSet", "rotate_secret"): Write(
        "webhook_destination.secret_rotated", webhook, const({"signing_secret": "r" * 32})
    ),
    ("WebhookDeliveryViewSet", "resend"): Write("notification.resent", webhook_delivery),
    ("NotificationRuleViewSet", "create"): Write(
        "notification_rule.created",
        body=lambda w, ids: {"name": uniq("Rule"), "source_types": ["contract"], "lead_times_days": [30]},
    ),
    ("NotificationRuleViewSet", "partial_update"): Write(
        "notification_rule.updated", notification_rule, const({"enabled": False})
    ),
    ("NotificationRuleViewSet", "destroy"): Write("notification_rule.deleted", notification_rule),
    ("NotificationRuleViewSet", "test_send"): Write("notification_rule.tested", notification_rule),
    ("SavedViewSubscriptionViewSet", "create"): Write(
        "saved_view_subscription.created",
        lambda w: {"view": saved_filter_shared(w)},
        lambda w, ids: {"saved_view": str(ids["view"]), "weekday": 2},
    ),
    ("SavedViewSubscriptionViewSet", "partial_update"): Write(
        "saved_view_subscription.updated", subscription, const({"weekday": 3})
    ),
    ("SavedViewSubscriptionViewSet", "destroy"): Write("saved_view_subscription.deleted", subscription),
    ("SavedViewSubscriptionViewSet", "test_send"): Write("saved_view_subscription.tested", subscription),
    # ---- discovery ----
    ("ConnectorViewSet", "create"): Write(
        "connector.created",
        body=lambda w, ids: {
            "source": "entra",
            "name": uniq("Connector"),
            "microsoft_connection": str(w["connection"].id),
        },
    ),
    ("ConnectorViewSet", "partial_update"): Write("connector.updated", connector, lambda w, ids: {"name": uniq("C")}),
    ("ConnectorViewSet", "destroy"): Write("connector.deleted", connector),
    ("ConnectorViewSet", "run"): Write("connector.run_requested", connector),
    ("ConnectorViewSet", "access_check"): Write("connector.access_checked", connector),
    ("ObservationViewSet", "link"): Write(
        "observation.linked", observation(), lambda w, ids: {"resource": str(w["app"].id)}
    ),
    ("ObservationViewSet", "create_record"): Write("observation.created", observation()),
    ("ObservationViewSet", "dismiss"): Write("observation.dismissed", observation(), const({"reason": "Not ours."})),
    ("ObservationViewSet", "reopen"): Write("observation.reopened", observation("dismissed")),
    ("ObservationViewSet", "assistant_accept"): Write(
        "observation.linked", suggested("link"), lambda w, ids: {"suggestion": ids["suggestion"]}
    ),
    ("ObservationViewSet", "assistant_reject"): Write(
        "assistant_suggestion.rejected", suggested("link"), lambda w, ids: {"suggestion": ids["suggestion"]}
    ),
    ("ObservationViewSet", "drift_apply"): Write(
        "resource.updated",
        drifted,
        lambda w, ids: drift_body(w, ids),
    ),
    ("ObservationViewSet", "drift_ignore"): Write(
        "observation.drift_ignored",
        drifted,
        lambda w, ids: drift_body(w, ids),
    ),
    ("ObservationViewSet", "missing_acknowledge"): Write(
        "observation.missing_resolved", observation("gone"), const({"note": "Moved."})
    ),
    ("ObservationViewSet", "missing_archive"): Write("resource.archived", observation("gone")),
    ("ImportSourceViewSet", "create"): Write(
        "import_source.created", body=lambda w, ids: {"name": uniq("HR"), "kind": "application"}
    ),
    ("ImportSourceViewSet", "partial_update"): Write(
        "import_source.updated", import_source, lambda w, ids: {"name": uniq("HR export")}
    ),
    ("ImportSourceViewSet", "destroy"): Write("import_source.deleted", import_source),
    ("ImportRunViewSet", "create"): Write(
        "import.uploaded",
        body=lambda w, ids: {"kind": "application", "file": SimpleUploadedFile("swept.csv", import_csv())},
        multipart=True,
    ),
    ("ImportRunViewSet", "commit"): Write("import.committed", import_run),
    ("ImportRunViewSet", "cancel"): Write("import.cancelled", import_run),
    # PATCH with a mapping and save_mapping stores it on the run's source (importer.map_run). The other branch,
    # staging decisions and row exclusions before commit (importer.update_run), writes import.staging_updated with
    # counts only: tests/test_imports.py test_staging_edits_are_audited_with_counts_only.
    ("ImportRunViewSet", "partial_update"): Write(
        "import_source.mapping_saved",
        unmapped_import,
        const({"mapping": {"App name": "name", "Status": "lifecycle"}, "save_mapping": True}),
    ),
    ("ImportRunViewSet", "rerun"): Write("import.uploaded", committed_import),
    # ---- composite writes ----
    ("OrganizationView", "patch"): Write("organization.updated", body=const({"review_interval_days": 120})),
    ("BulkApplyView", "post"): Write(
        "resource.updated",
        resource("application"),
        lambda w, ids: {"kind": "application", "ids": [str(ids["pk"])], "changes": {"add_tags": ["swept"]}},
    ),
    ("BulkDeleteApplyView", "post"): Write(
        "resource.deleted", resource("application"), lambda w, ids: {"kind": "application", "ids": [str(ids["pk"])]}
    ),
    ("SsoSettingsView", "patch"): Write("sso_settings.updated", body=const({"provider": "okta"})),
    ("SsoActivateView", "post"): Write("sso_settings.activated", sso_draft(True), lambda w, ids: ids),
    ("SsoDisableView", "post"): Write("sso_settings.disabled", sso_draft(True)),
    ("SsoDomainVerifyView", "post"): Write("sso_settings.updated", pending_sign_in_domain, lambda w, ids: ids),
    ("BackupKeyAcknowledgeView", "post"): Write("backup.key_acknowledged"),
    ("CalendarFeedView", "post"): Write("calendar_feed.created"),
    ("CalendarFeedView", "delete"): Write("calendar_feed.revoked", calendar_feed),
    ("CalendarFeedRotateView", "post"): Write("calendar_feed.rotated", calendar_feed),
    ("PreferencesView", "patch"): Write("preference.updated", body=const({"digest_enabled": False})),
    ("EmailSettingsView", "patch"): Write(
        "email_settings.updated",
        body=const({"transport": "smtp", "smtp_host": "smtp.example.test", "from_address": "it@example.test"}),
    ),
    ("EmailSettingsView", "delete"): Write("email_settings.deleted", email_settings),
    ("EmailTestConnectionView", "post"): Write("email_settings.tested", email_settings),
    ("EmailTestSendView", "post"): Write("email_settings.tested", email_settings),
    ("AssistantSettingsView", "patch"): Write("assistant_settings.updated", body=const({"monthly_cap_usd": "15.00"})),
    ("AssistantBatchesView", "post"): Write("assistant_batch.requested", assistant_on),
    **{
        (f"RecordEndOfLifeView[{kind}]", method): spec
        for kind in ("application", "component")
        for method, spec in (
            (
                "patch",
                Write("tracked_version.created", resource(kind), const({"product": "postgresql", "cycle": "16"})),
            ),
            ("delete", Write("tracked_version.deleted", tracked(kind))),
        )
    },
    ("InstanceEndOfLifeView", "patch"): Write(
        "tracked_version.created", tracked_app_instance, const({"version": "13.2"})
    ),
    ("InstanceEndOfLifeView", "delete"): Write("tracked_version.deleted", tracked_instance),
    ("CatalogCodeApplyView", "post"): Write(
        "catalog_code.applied",
        planned_tree,
        lambda w, ids: {"file": SimpleUploadedFile("catalog.zip", ids["zip"]), "plan_hash": ids["plan_hash"]},
        multipart=True,
    ),
    ("ReassignView", "post"): Write(
        "person.reassigned",
        reassignable,
        lambda w, ids: {"to": str(w["people"]["dana"].id), "roles": ["technical_owner"]},
    ),
    **{
        (f"{view}[{kind}]", method): spec
        for kind in ("assessment", "renewal")
        for view, method, spec in (
            (
                "TicketLinksView",
                "post",
                Write(
                    "ticket_link.created",
                    ticket_link(kind, with_link=False),
                    lambda w, ids: {"url": f"https://contoso.atlassian.net/browse/ITOPS-{next(_names)}"},
                ),
            ),
            ("TicketLinkView", "patch", Write("ticket_link.updated", ticket_link(kind), const({"status": "open"}))),
            ("TicketLinkView", "delete", Write("ticket_link.deleted", ticket_link(kind))),
            ("TicketLinkRefreshView", "post", Write("ticket_link.status_changed", ticket_link(kind))),
            (
                "CreateTicketView",
                "post",
                Write(
                    "ticket_link.ticket_created",
                    ticket_link(kind, with_link=False),
                    const({"system": "jira"}),
                ),
            ),
        )
    },
    # last: these change the signed-in account and the organization as a whole
    ("PasswordView", "post"): Write(
        "user.password_changed",
        body=const({"current_password": "correct-horse-battery", "new_password": "a-new-horse-battery-staple"}),
    ),
    ("OrganizationDeletionView", "post"): Write(
        "organization.deletion_requested",
        cloud,
        lambda w, ids: {"confirm": w["org"].slug, "password": "a-new-horse-battery-staple"},
    ),
    ("OrganizationDeletionView", "delete"): Write("organization.deletion_cancelled", cloud),
}


def saved_filter_shared(w: W):
    from catalog.models import SavedFilter

    return SavedFilter.objects.create(
        organization=w["org"], user=editor(w), shared=True, kind="application", name=uniq("Shared"), query={}
    ).id


def drift_body(w: W, ids: dict[str, Any]) -> dict[str, Any]:
    client = Client()
    client.force_login(admin(w))
    differences = client.get(f"/api/v1/discovery/observations/{ids['pk']}/drift").json()["differences"]
    first = differences[0]
    return {"fields": [first["field"]], "seen": {first["field"]: first["source"]}}


# Write routes that don't write an audit event, and why. Each is a read in POST's clothing or hands the write to the
# worker, which audits there.
NOT_AUDITED: dict[Key, str] = {
    ("BulkPreviewView", "post"): "a preview: computes what bulk edit would change and writes nothing",
    ("BulkDeletePreviewView", "post"): "a preview: lists what bulk delete would remove and writes nothing",
    ("CatalogCodePlanView", "post"): "a plan: computes the changes an apply would make and writes nothing",
    ("SsoTestView", "post"): "starts a browser round trip: the pending test is kept in the session only; the "
    "callback records the result on the draft, audited there as sso_settings.tested (tests/test_sso.py)",
}


# ---- the world ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def w(a, settings, tmp_path, simulator):
    """The `a` organization on the enterprise edition (every capability but the cloud's organization self-deletion,
    which its cases turn on), with the rows the bodies point at."""
    from catalog import ai
    from core import connections
    from core.capabilities import set_edition
    from tests.test_drf_resources import _connector

    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    key = tmp_path / "backup_key"
    key.write_text("generated-backup-key-" + "x" * 40 + "\n", encoding="utf-8")
    settings.BACKUP_KEY_FILE, settings.BACKUP_KEY_FILE_CONFIGURED = str(key), False
    a["org"], _ = set_edition(a["org"], "enterprise")
    org = a["org"]
    a["job"] = services.create_resource(
        org, a["users"]["editor"], {"name": "Swept target", "kind": "component", "lifecycle": "active"}
    )
    a["provider"] = ai.create_provider(org, a["users"]["editor"], {"name": "Swept provider"})
    a["model"] = ai.create_model(org, a["users"]["editor"], {"provider": str(a["provider"].id), "name": "Base"})
    a["connection"] = connections.create_connection(
        org,
        a["users"]["admin"],
        connections.MICROSOFT,
        {"name": "Swept connection", "tenant_id": "simulated", "secret": "synthetic-secret"},
    )
    connections.run_test(org, a["users"]["admin"], a["connection"])  # a run needs a tested connection
    a["connector"] = _connector(a)
    return a


def send(client: Client, method: str, path: str, spec: Write, body: Any):
    if spec.multipart:
        return client.post(path, body)
    return getattr(client, method)(path, json.dumps(body), content_type="application/json")


def test_every_write_route_is_in_the_audit_sweep():
    found = set(write_routes())
    assert not set(WRITES) & set(NOT_AUDITED)
    assert found == set(WRITES) | set(NOT_AUDITED), (
        "add the write route to WRITES with the audit action it writes, or to NOT_AUDITED with the reason",
        sorted(found - set(WRITES) - set(NOT_AUDITED)),
        sorted((set(WRITES) | set(NOT_AUDITED)) - found),
    )


@pytest.mark.usefixtures("preview_on")
def test_every_write_writes_its_audit_event(w):
    routes = write_routes()
    client = Client()
    client.force_login(admin(w))
    problems: list[str] = []
    for key, spec in WRITES.items():  # in order: the cases that change the account or the organization come last
        method, route = routes[key]
        try:
            with transaction.atomic():  # a setup that fails is reported with the rest, not as a broken transaction
                ids = spec.setup(w)
                body = spec.body(w, ids)
        except Exception as error:
            problems.append(f"{key}: setup failed: {error!r}")
            continue
        path = fill(route, {name: ids[name] for name in PARAM.findall(route)})
        before = set(AuditEvent.objects.filter(organization=w["org"]).values_list("id", flat=True))
        response = send(client, method, path, spec, body)
        added = list(
            AuditEvent.objects.filter(organization=w["org"])
            .exclude(id__in=before)
            .order_by("created_at")
            .values_list("action", flat=True)
        )
        if response.status_code not in spec.status:
            problems.append(f"{key}: {method} {path} answered {response.status_code}: {response.content[:300]!r}")
        elif spec.expect not in added:
            problems.append(f"{key}: expected {spec.expect!r}, the write added {added}")
    assert not problems, "\n".join(problems)


# ---- every action the code writes is asserted somewhere ----------------------------------------------------------------

TESTS = Path(__file__).resolve().parent


def platform_actions() -> set[str]:
    """The platform console's audit actions (catalog/platform_admin.platform_audit(actor, action, ...))."""
    import ast

    from tests.test_audit_labels import APPS, ROOT

    found = set()
    for app in APPS:
        for path in (ROOT / app).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    arg = node.args[1] if name == "platform_audit" and len(node.args) > 1 else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.add(arg.value)
    return found


def test_every_audit_action_is_named_by_a_test():
    """Every action the code writes (the literal ones, the values of the dynamic ones listed in
    tests/test_audit_labels.py DYNAMIC, and the platform console's) is named as a string in a test other than the
    label check, so a test can fail when the event stops being written (17 actions were named nowhere). The
    reasons in NOT_AUDITED don't count: they name an action a route does not write."""
    from tests.test_audit_labels import DYNAMIC, written_actions

    literal, dynamic = written_actions()
    actions = literal | {value for expr in dynamic for value in DYNAMIC.get(expr, ())} | platform_actions()
    assert len(actions) > 200 and {"staff.granted", "teams_destination.tested"} <= actions
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in TESTS.glob("*.py") if path.name != "test_audit_labels.py"
    )
    start = text.index("NOT_AUDITED: dict[Key, str] = {")  # this file's reasons, as written (split literals too)
    text = text[:start] + text[text.index("\n}", start) :]
    named = {action for action in actions if f'"{action}"' in text or f"'{action}'" in text}
    assert sorted(actions - named) == [], "assert that a write writes these actions (tests/test_audit_writes.py)"
