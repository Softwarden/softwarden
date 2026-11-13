"""Editions and capabilities (docs/CAPABILITIES.md)."""

import importlib
import io
import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core import capabilities as caps
from core.models import AuditEvent, Edition, Organization
from core.problems import FieldErrors
from discovery import engine as discovery_engine

# Okta, Google Workspace, ServiceNow and Halo are preview features (launch scope, off by default): this module
# covers them with SOFTWARDEN_PREVIEW_FEATURES on. tests/test_preview_features.py covers them off.
pytestmark = pytest.mark.usefixtures("preview_on")

ROOT = Path(__file__).resolve().parent.parent
APPS = (
    "core",
    "catalog",
    "money",
    "notify",
    "discovery",
    "imports",
    "catalog_code",
    "offboarding",
    "api",
    "eol",
    "tickets",
    "softwarden",
    "ee",
)
TOP_LEVEL = ("e2e_server.py", "e2e_db.py", "manage.py", "gunicorn.conf.py")

PLAN_ENTERPRISE = {
    # SAML single sign-on and SCIM provisioning, from the optional `ee` package. Configurable audit retention isn't
    # built, so no edition offers it (docs/CAPABILITIES.md).
    "sso.saml",
    "scim",
}

# What an enterprise organization has here: without the optional `ee` package, no organization has SAML or SCIM.
EE_ABSENT = frozenset() if caps.ee_installed() else caps.EE_ONLY
ENTERPRISE_HAS = frozenset(PLAN_ENTERPRISE) - EE_ABSENT

# Billing (blocked: no Stripe account) and self-service deletion.
PLAN_CLOUD = {"billing", "org.self_delete"}

# In both cloud and enterprise: nightly per-organization backups.
PLAN_CLOUD_AND_ENTERPRISE = {"org.tenant_backups"}


def set_edition(org, edition):
    org, _ = caps.set_edition(org, edition)
    return org


# ---- the mapping ---------------------------------------------------------------------------------------------------


def test_editions_map_to_the_plans_capability_sets():
    assert caps.ENTERPRISE_ONLY == PLAN_ENTERPRISE
    assert caps.CLOUD_ONLY == PLAN_CLOUD
    assert caps.CLOUD_AND_ENTERPRISE == PLAN_CLOUD_AND_ENTERPRISE
    shared = PLAN_CLOUD_AND_ENTERPRISE
    assert caps.EDITION_CAPABILITIES[Edition.CLOUD] == caps.COMMUNITY | PLAN_CLOUD | shared
    assert caps.EDITION_CAPABILITIES[Edition.ENTERPRISE] == caps.COMMUNITY | PLAN_ENTERPRISE | shared
    assert set(caps.EDITION_CAPABILITIES) == set(Edition.values)
    assert caps.GATED == PLAN_ENTERPRISE | PLAN_CLOUD | PLAN_CLOUD_AND_ENTERPRISE
    assert not caps.COMMUNITY & caps.GATED
    assert "discovery.entra" in caps.COMMUNITY and "imports" in caps.COMMUNITY


def test_capabilities_follow_the_organizations_edition(a):
    org = a["org"]
    assert org.edition == Edition.COMMUNITY
    assert caps.capabilities(org) == caps.COMMUNITY
    assert caps.capabilities(set_edition(org, "cloud")) == caps.COMMUNITY | PLAN_CLOUD | PLAN_CLOUD_AND_ENTERPRISE
    assert (
        caps.capabilities(set_edition(org, "enterprise")) == caps.COMMUNITY | ENTERPRISE_HAS | PLAN_CLOUD_AND_ENTERPRISE
    )
    assert isinstance(caps.capabilities(org), frozenset)


def test_an_unknown_edition_value_gets_community(a):
    org = a["org"]
    org.edition = "platinum"  # not reachable through the service; the function still fails closed
    assert caps.capabilities(org) == caps.COMMUNITY


def test_an_unknown_capability_name_is_a_programming_error(a):
    with pytest.raises(ValueError, match="Unknown capability"):
        caps.has_capability(a["org"], "audit.exprot")


# ---- me -------------------------------------------------------------------------------------------------------------


def test_me_returns_edition_and_sorted_capabilities(a, b, api):
    me = api(a["users"]["viewer"]).get("auth/me").json_body
    assert me["edition"] == "community"
    assert me["capabilities"] == sorted(caps.COMMUNITY)
    assert me["organization"]["slug"] == a["org"].slug
    set_edition(a["org"], "enterprise")
    me = api(a["users"]["viewer"]).get("auth/me").json_body
    assert me["edition"] == "enterprise"
    assert me["capabilities"] == sorted(caps.COMMUNITY | ENTERPRISE_HAS | PLAN_CLOUD_AND_ENTERPRISE)
    # Another organization's edition is its own.
    other = api(b["users"]["viewer"]).get("auth/me").json_body
    assert other["edition"] == "community" and "scim" not in other["capabilities"]


def test_me_follows_the_current_organization(a, b, api):
    from core.models import Membership, Role

    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role=Role.VIEWER)
    set_edition(b["org"], "enterprise")
    client = api(user)
    assert client.get("auth/me").json_body["edition"] == "community"
    client.post("auth/me/organization", {"organization_id": str(b["org"].id)})
    me = client.get("auth/me").json_body
    assert me["edition"] == "enterprise" and "org.tenant_backups" in me["capabilities"]


def test_the_licence_key_never_leaves_the_server(a, api):
    Organization.objects.filter(pk=a["org"].pk).update(licence_key_ciphertext="gAAAA-not-a-real-ciphertext")
    client = api(a["users"]["admin"])
    for url in ("auth/me", "organization"):
        body = client.get(url).content.decode()
        assert "licence" not in body and "gAAAA" not in body


def test_licence_key_values_are_redacted_from_audit_payloads(a):
    from catalog import services

    changes = {"licence_key": "k", "licence_key_ciphertext": "c", "license_key": "k", "license_model": "subscription"}
    services.audit(a["org"], None, "organization.updated", a["org"], changes)
    event = AuditEvent.objects.filter(organization=a["org"], action="organization.updated").get()
    assert event.changes == {
        "licence_key": "[redacted]",
        "licence_key_ciphertext": "[redacted]",
        "license_key": "[redacted]",  # US spelling
        "license_model": "subscription",  # an ordinary record field, not a secret
    }


# ---- the edition is set only by an operator -------------------------------------------------------------------------


def test_set_edition_is_audited_once_and_only_when_it_changes(a):
    org = a["org"]
    org, changed = caps.set_edition(org, "enterprise")
    assert changed and Organization.objects.get(pk=org.pk).edition == "enterprise"
    events = AuditEvent.objects.filter(organization=org, action="organization.edition_changed")
    assert [e.changes for e in events] == [{"edition": ["community", "enterprise"], "via": "cli"}]
    assert events[0].target_type == "Organization" and events[0].target_id == org.id
    _, changed = caps.set_edition(org, "enterprise")
    assert not changed and events.count() == 1


def test_set_edition_refuses_an_unknown_edition(a):
    with pytest.raises(FieldErrors):
        caps.set_edition(a["org"], "platinum")
    assert Organization.objects.get(pk=a["org"].pk).edition == "community"


def test_set_edition_command(a, b):
    out = io.StringIO()
    call_command("set_edition", a["org"].slug, "enterprise", stdout=out)
    assert "is now enterprise" in out.getvalue()
    assert Organization.objects.get(pk=a["org"].pk).edition == "enterprise"
    assert Organization.objects.get(pk=b["org"].pk).edition == "community"
    out = io.StringIO()
    call_command("set_edition", a["org"].slug, "enterprise", stdout=out)
    assert "nothing changed" in out.getvalue()
    out = io.StringIO()
    call_command("set_edition", "--list", stdout=out)
    assert f"{a['org'].slug}\tenterprise" in out.getvalue() and f"{b['org'].slug}\tcommunity" in out.getvalue()
    with pytest.raises(CommandError, match="No organization has the slug"):
        call_command("set_edition", "no-such-org", "cloud")
    with pytest.raises(CommandError):
        call_command("set_edition", a["org"].slug, "platinum")
    with pytest.raises(CommandError):
        call_command("set_edition", a["org"].slug)


def test_an_admin_cannot_change_the_edition_through_settings(a, api):
    client = api(a["users"]["admin"])
    response = client.patch(
        "organization", {"name": "Renamed", "edition": "enterprise", "slug": "x", "licence_key_ciphertext": "k"}
    )
    assert response.status_code == 200
    assert response.json_body["edition"] == "community" and response.json_body["slug"] == a["org"].slug
    org = Organization.objects.get(pk=a["org"].pk)
    assert (org.name, org.edition, org.licence_key_ciphertext) == ("Renamed", "community", "")
    event = AuditEvent.objects.filter(organization=org, action="organization.updated").get()
    assert event.changes == {"name": "Renamed"}  # keys the endpoint ignores aren't recorded as changes


def test_a_settings_save_does_not_undo_an_operators_edition_change(a):
    from catalog import admin_services

    stale = Organization.objects.get(pk=a["org"].pk)
    set_edition(a["org"], "enterprise")
    admin_services.update_organization(stale, a["users"]["admin"], {"timezone": "Europe/London"})
    assert Organization.objects.get(pk=a["org"].pk).edition == "enterprise"


# ---- slugs ----------------------------------------------------------------------------------------------------------


def test_slugs_come_from_the_name_and_never_collide(db):
    first = Organization.objects.create(name="Acme Corp")
    second = Organization.objects.create(name="Acme  corp!")
    third = Organization.objects.create(name="ACME CORP")
    blank = Organization.objects.create(name="!!!")
    assert [first.slug, second.slug, third.slug, blank.slug] == [
        "acme-corp",
        "acme-corp-2",
        "acme-corp-3",
        "organization",
    ]
    first.name = "Renamed"
    first.save()
    first.refresh_from_db()
    assert first.slug == "acme-corp"  # stable when the name changes
    long = Organization.objects.create(name="x" * 200)
    assert len(long.slug) <= 60


def test_a_slug_taken_between_the_read_and_the_insert_is_retried(db, monkeypatch):
    """Two organizations of one name created at once: the read saw the
    slug free, another insert took it, so the insert is retried in a savepoint with the next slug."""
    from collections.abc import Iterator

    from core import models as core_models

    Organization.objects.create(name="Acme Corp")
    real = core_models.unique_org_slug
    stale: Iterator[set[str]] = iter(
        [set()]
    )  # the first choice ignores what is taken, as a read before the other insert would

    def raced(name, taken):
        return real(name, next(stale, taken))

    monkeypatch.setattr(core_models, "unique_org_slug", raced)
    second = Organization.objects.create(name="Acme Corp")
    assert second.slug == "acme-corp-2"
    # A clash on something other than the slug is raised as it is.
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        Organization.objects.create(id=second.id, name="Someone else")


def test_the_migration_backfills_unique_slugs(db):
    migration = importlib.import_module("core.migrations.0004_organization_edition")
    from django.apps import apps

    for name in ("Beta", "beta", "???", "Alpha"):
        Organization.objects.create(name=name)
    for org in Organization.objects.all():  # placeholders; the backfill rewrites every row
        Organization.objects.filter(pk=org.pk).update(slug=f"tmp-{org.pk}")
    migration.backfill_slugs(apps, None)
    slugs = sorted(Organization.objects.values_list("slug", flat=True))
    assert slugs == ["alpha", "beta", "beta-2", "organization"]


# ---- one API test per gated capability ------------------------------------------------------------------------------

TEST_VIEW = "test-view"  # tests/capability_urls.py; no shipped endpoint exists yet for these
CONNECTOR = "connectors"  # POST /api/v1/connectors with that source (discovery/engine.py create_connector)
ORGANIZATION = "organization"  # PATCH /api/v1/organization turning the setting on (Admin; catalog/compliance.py)
GET_AS_ADMIN = "get-as-admin"  # GET /api/v1/<path> as an Admin (a download: streamed, so read without json_body)
PUBLIC_GET = "public-get"  # GET /api/v1/<path> signed out (a named organization's public route; may not be JSON)
POST_AS_ADMIN = "post-as-admin"  # POST /api/v1/<path> with an empty body as an Admin: past the gate, field errors (400)
PATCH_AS_ADMIN = "patch-as-admin"  # PATCH /api/v1/<path> with <body> as an Admin: past the gate, 200

# Every gated capability and the endpoint that proves its gate. Adding a capability to ENTERPRISE_ONLY or CLOUD_ONLY
# without listing it here fails test_every_gated_capability_is_covered. Move an entry from TEST_VIEW to its real
# endpoint when the feature ships.
COVERAGE = {
    # Choosing the SAML preset (core/sso.py). Not the public SP metadata any more: without SAML set up it
    # answers the same 404 as an unknown organization, so it reveals nothing, the capability included.
    "sso.saml": (PATCH_AS_ADMIN, ("sso/settings", {"provider": "saml"})),
    # The SCIM settings on the organization (catalog/admin_services.py); the SCIM routes themselves come from the
    # optional `ee` package.
    "scim": (PATCH_AS_ADMIN, ("organization", {"scim_default_role": "editor"})),
    "billing": (TEST_VIEW, None),
    "org.self_delete": (POST_AS_ADMIN, "organization/deletion"),  # (catalog/lifecycle_views.py)
    # No route of its own; the nightly schedule checks it (tests/test_tenant_backups.py) and backups are
    # listed with the exports.
    "org.tenant_backups": (TEST_VIEW, None),
}


def test_every_gated_capability_is_covered():
    assert set(COVERAGE) == caps.GATED


def edition_with(capability):
    return next(e for e in Edition.values if capability in caps.EDITION_CAPABILITIES[e])


def call(api, a, capability):
    how, source = COVERAGE[capability]
    if how == CONNECTOR:
        return api(a["users"]["admin"]).post("connectors", {"source": source, "name": f"{source} connector"})
    if how == ORGANIZATION:
        return api(a["users"]["admin"]).patch("organization", {source: True})
    if how == PUBLIC_GET:
        response = api().client.get(f"/api/v1/{source}")
        response.json_body = response.json() if "json" in response["Content-Type"] else None
        return response
    if how == POST_AS_ADMIN:
        return api(a["users"]["admin"]).post(source, {})
    if how == PATCH_AS_ADMIN:
        assert isinstance(source, tuple)
        path, body = source
        return api(a["users"]["admin"]).patch(path, body)
    if how == GET_AS_ADMIN:
        response = api(a["users"]["admin"]).client.get(f"/api/v1/{source}")
        response.json_body = None if response.streaming else response.json()
        return response
    return api(a["users"]["viewer"]).get(f"_test/capability/{capability}")


@pytest.fixture
def test_urls(settings):
    settings.ROOT_URLCONF = "tests.capability_urls"


@pytest.mark.parametrize("capability", sorted(COVERAGE))
def test_a_gated_capability_is_refused_in_community_with_a_problem_naming_it(capability, a, api, test_urls):
    response = call(api, a, capability)
    assert response.status_code == 403
    assert response["Content-Type"] == "application/problem+json"
    body = response.json_body
    assert body["type"] == "/problems/capability-required"
    assert body["status"] == 403
    assert body["capability"] == capability
    assert capability in body["detail"]
    assert body["title"] == "Capability required"  # one title per problem type (core/problems.py)


@pytest.mark.parametrize("capability", sorted(COVERAGE))
def test_a_gated_capability_passes_the_gate_in_an_edition_that_has_it(capability, a, api, test_urls):
    if capability in EE_ABSENT:
        pytest.skip("needs the optional ee package (SAML and SCIM), which isn't installed")
    set_edition(a["org"], edition_with(capability))
    response = call(api, a, capability)
    assert response.status_code != 403, response.json_body
    if COVERAGE[capability][0] in (TEST_VIEW, ORGANIZATION, GET_AS_ADMIN, PUBLIC_GET, PATCH_AS_ADMIN):
        assert response.status_code == 200
    elif COVERAGE[capability][0] == POST_AS_ADMIN:  # past the gate, the request's fields are checked
        assert response.status_code == 400 and response.json_body["errors"]
    elif COVERAGE[capability][1] in discovery_engine.SOURCES:
        # A built source: past the gate, the connector form asks for its connection.
        assert response.status_code == 400 and "microsoft_connection" in response.json_body["errors"]
    else:  # the source isn't built yet: past the gate, the connector form refuses it as unsupported
        assert response.status_code == 400 and "source" in response.json_body["errors"]


def test_the_test_only_route_is_not_shipped(a, api):
    # billing still has only the test-only route (scim had it until SCIM got a real one)
    assert api(a["users"]["viewer"]).client.get("/api/v1/_test/capability/billing").status_code == 404


def test_community_sources_still_need_no_upgrade(a, api):
    """Every discovery source is community: the gate lets its connector through to ordinary validation (Okta and
    Google Workspace with the installation's preview features on, as in this module)."""
    admin = api(a["users"]["admin"])
    for source in ("entra", "azure", "powerplatform", "halo", "okta", "google_workspace"):
        response = admin.post("connectors", {"source": source, "name": source})
        assert response.status_code == 400 and "capability" not in response.json_body, source
    for kind in ("halo", "okta", "google_workspace"):
        assert admin.get(f"connections/{kind}").status_code == 200, kind


@pytest.mark.parametrize(
    "name",
    [
        "discovery.entra",
        "discovery.azure",
        "discovery.powerplatform",
        "discovery.halo",
        "discovery.okta",
        "discovery.google",
        "assistant",
        "audit.export",
        "compliance_profile",
    ],
)
def test_every_feature_in_the_repository_is_in_every_edition(name):
    assert name in caps.COMMUNITY and name not in caps.GATED
    assert all(name in capabilities for capabilities in caps.EDITION_CAPABILITIES.values())


def test_okta_and_google_discovery_stay_previews():
    for name in ("discovery.okta", "discovery.google"):
        assert name in caps.PREVIEW_ONLY and name in caps.HIDDEN_WITHOUT_PREVIEW


def test_role_is_checked_before_capability(a, api):
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.azure", effect="revoke", reason="t")
    caps.forget_organization(a["org"].pk)
    response = api(a["users"]["viewer"]).post("connectors", {"source": "azure"})
    assert response.status_code == 403 and "capability" not in response.json_body


# ---- nothing compares editions --------------------------------------------------------------------------------------

# core/limits.py holds the per-edition limit defaults beside the capability sets (limits).
ALLOWED = {ROOT / "core" / "capabilities.py", ROOT / "core" / "limits.py", ROOT / "core" / "models.py"}
COMPARISONS = [
    re.compile(r"\bedition\b\s*(==|!=|\bin\b|\bnot in\b|\bis\b)"),
    re.compile(r"(==|!=|\bin\b)\s*[\w.]*\bedition\b"),
    re.compile(r"\bEdition\.(COMMUNITY|CLOUD|ENTERPRISE)\b"),
    re.compile(r"""(==|!=)\s*["'](community|cloud|enterprise)["']"""),
    re.compile(r"""["'](community|cloud|enterprise)["']\s*(==|!=)"""),
    re.compile(r"\bedition(__\w+)?=(?!=)"),  # ORM lookups: filter(edition=...), exclude(edition__in=...)
    re.compile(r"\bmatch\b.*\bedition\b"),  # match org.edition:
    re.compile(r"\bEdition\("),  # Edition("enterprise")
]


def edition_comparisons():
    found = []
    paths = [p for app in APPS for p in (ROOT / app).rglob("*.py")] + [ROOT / name for name in TOP_LEVEL]
    for path in paths:
        if path in ALLOWED or "migrations" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(p.search(code) for p in COMPARISONS):
                found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    return found


def test_no_code_outside_capabilities_compares_the_edition():
    """Features check a capability name (core/capabilities.py), never the edition."""
    assert edition_comparisons() == []


def test_the_edition_scan_catches_a_comparison(tmp_path):
    samples = [
        "if org.edition == 'enterprise':",
        "if org.edition in (Edition.CLOUD,):",
        "allowed = Edition.ENTERPRISE",
        'if "cloud" == org.edition:',
        "Organization.objects.filter(edition='enterprise')",
        "orgs.exclude(edition__in=paid)",
        "match org.edition:",
        "if Edition(org.edition) is Edition.CLOUD:",
        'wanted = Edition("cloud")',
    ]
    assert all(any(p.search(s) for p in COMPARISONS) for s in samples)
    legitimate = [
        '"edition": org.edition,',
        "edition = serializers.ChoiceField(Edition.values)",
        'parser.add_argument("new_edition", choices=Edition.values)',
        "org, changed = capabilities.set_edition(found, new_edition)",
    ]
    assert not any(p.search(s) for s in legitimate for p in COMPARISONS)


# ---- every route's capability, from the resolver and the ledger ------------------------------------------------

# The expectation is the capability column of ROLE_LEDGER (tests/test_permissions.py), where every organization route
# has an explicit decision ("-" for none), so a new route at a new prefix can't pass without one: the ledger test fails
# until it has a line. Held both ways here: a route declaring a capability its line lacks fails, and every capability a
# line names is asked for as an organization without it.
NOBODY = "00000000-0000-4000-8000-0000000f0066"


def declared_capability(callback) -> str | None:
    """The capability a routed view declares: in its role policy, on its class, or passed to as_view()."""
    from core.drf import declared_policy

    found = declared_policy(callback)
    initkwargs = getattr(callback, "initkwargs", None) or {}
    cls = getattr(callback, "cls", None)
    return (
        (found.capability if found else None)
        or initkwargs.get("required_capability")
        or getattr(cls, "required_capability", None)
    )


def declared_capabilities() -> dict[tuple[str, str], str | None]:
    """{(method, route): declared capability} for every DRF route."""
    from django.urls import get_resolver

    from tests.test_url_coverage import methods_and_actions, walk

    found = {}
    for route, callback in walk(get_resolver().url_patterns):
        if getattr(callback, "cls", None) is not None:
            for method, _action in methods_and_actions(callback):
                found[method, route] = declared_capability(callback)
    return found


def test_every_declared_capability_is_in_the_ledger():
    from tests.test_permissions import capability_ledger

    expected = capability_ledger()
    declared = declared_capabilities()
    assert len(expected) > 400 and set(expected) <= set(declared)
    unexpected = [
        f"{method.upper()} {route} declares {name}, its ROLE_LEDGER line says {'+'.join(sorted(expected[method, route])) or '-'}"
        for (method, route), name in declared.items()
        if name and (method, route) in expected and name not in expected[method, route]
    ]
    assert unexpected == [], "\n".join(unexpected)


def test_every_gated_route_refuses_an_organization_without_it(a, settings, monkeypatch):
    """Every ledger route with a capability, every method, as the Admin of an organization without it: a community
    organization for an enterprise or cloud capability, a platform revoke for a community one. The answer is the
    capability problem naming it, before the route reads anything (ids that name nothing, an empty body)."""
    import json

    from rest_framework.views import APIView

    from core.models import CapabilityOverride
    from tests.test_permissions import capability_ledger
    from tests.test_tenancy_walk import PARAM, json_client, send

    settings.API_READ_RATE = settings.API_WRITE_RATE = ""
    monkeypatch.setattr(APIView, "check_throttles", lambda self, request: None)
    org = a["org"]
    assert org.edition == Edition.COMMUNITY
    client = json_client(a["users"]["admin"], raise_request_exception=False)
    wrong, checked = [], 0
    for (method, route), names in sorted(capability_ledger().items()):
        for name in sorted(names):
            revoke = name in caps.COMMUNITY
            if revoke:
                CapabilityOverride.objects.create(
                    organization=org, capability=name, effect="revoke", reason="capability walk"
                )
                caps.forget_organization(org.pk)
            path = "/" + PARAM.sub(lambda m: {"int": "987654321", "slug": "none"}.get(m.group(1), NOBODY), route)
            response = send(client, method, path)
            checked += 1
            problem = "problem+json" in response.get("Content-Type", "")  # a list, a file: not a refusal
            body = json.loads(response.content) if problem else {}
            if (response.status_code, body.get("type"), body.get("capability")) != (
                403,
                "/problems/capability-required",
                name,
            ):
                wrong.append(f"{method.upper()} /{route} without {name}: {response.status_code} {body.get('type')}")
            if revoke:
                CapabilityOverride.objects.filter(organization=org, capability=name).delete()
                caps.forget_organization(org.pk)
    assert checked > 150
    assert wrong == [], "\n".join(wrong)


@pytest.mark.urls("tests.e2e_urls")
def test_the_e2e_route_turns_a_capability_off_and_on_for_the_callers_organization(a, b, api):
    admin = api(a["users"]["admin"])
    assert api(a["users"]["editor"]).post("e2e/capability-override", {"capability": "assistant"}).status_code == 403
    assert admin.post("e2e/capability-override", {"capability": "no.such"}).status_code == 400
    for not_a_bool in ("false", "true", 0, 1, None, [False]):
        refused = admin.post("e2e/capability-override", {"capability": "assistant", "revoked": not_a_bool})
        assert refused.status_code == 400, not_a_bool
    assert caps.has_capability(a["org"], "assistant")
    assert admin.post("e2e/capability-override", {"capability": "assistant", "revoked": True}).status_code == 200
    caps.forget_organization(a["org"].pk)  # this test's own cached read
    assert not caps.has_capability(a["org"], "assistant") and caps.has_capability(b["org"], "assistant")
    refused = admin.get("assistant/settings")
    assert refused.status_code == 403 and refused.json_body["capability"] == "assistant"
    from core.models import AuditEvent

    assert AuditEvent.objects.filter(organization=a["org"], action="organization.capability_revoked").exists()
    assert admin.post("e2e/capability-override", {"capability": "assistant", "revoked": False}).status_code == 200
    assert AuditEvent.objects.filter(organization=a["org"], action="organization.capability_override_removed").exists()
    caps.forget_organization(a["org"].pk)
    assert caps.has_capability(a["org"], "assistant")
