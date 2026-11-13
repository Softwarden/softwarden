"""Three-way kind handling: an integration never shows up in the record queues, lists, counts or reviews."""

import pytest
from django.utils import timezone

from catalog import services
from catalog.models import RECORD_KINDS, Kind, Resource
from catalog.state import all_gaps, ownership_gaps


@pytest.fixture
def integration(a):
    """An active integration with no owners and no criticality: the shape that would be a gap on a record. It runs
    between two instances of the application fixture, so the record count stays at one."""
    editor = a["users"]["editor"]
    prod = services.create_instance(a["org"], editor, a["app"], {"name": "Production"})
    sandbox = services.create_instance(a["org"], editor, a["app"], {"name": "Sandbox"})
    return services.create_integration(
        a["org"],
        editor,
        {
            "name": "Payroll feed",
            "lifecycle": "active",
            "source": str(a["app"].id),
            "source_instance": str(prod.id),
            "target": str(a["app"].id),
            "target_instance": str(sandbox.id),
        },
    )


def test_record_kinds_exclude_integration():
    assert Kind.INTEGRATION not in RECORD_KINDS and Kind.INTEGRATION in Kind.values


def test_needs_attention_ignores_integrations(a, api, integration):
    own = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    ids = {row["id"] for row in own["ownership_gaps"] + own["reviews"]}
    assert str(integration.id) not in ids
    assert str(a["app"].id) in ids  # the application fixture is a gap, so the query itself still works
    assert own["records"] == 1  # the application only


def test_needs_attention_records_is_zero_with_only_an_integration(a, api, integration):
    Resource.objects.filter(organization=a["org"], kind=Kind.APPLICATION).update(archived_at=timezone.now())
    body = api(a["users"]["viewer"]).get("needs-attention?scope=all").json_body
    assert body["records"] == 0 and body["ownership_gaps"] == [] and body["reviews"] == []


@pytest.mark.parametrize("path", ["applications", "components"])
def test_record_lists_exclude_integrations(a, api, integration, path):
    body = api(a["users"]["viewer"]).get(path).json_body
    rows = body["results"] if isinstance(body, dict) else body
    assert str(integration.id) not in {r["id"] for r in rows}


@pytest.mark.parametrize("path", ["applications", "components"])
def test_integration_ids_are_not_found_on_record_endpoints(a, api, integration, path):
    editor = api(a["users"]["editor"])
    for suffix in ("", "/reviews", "/audit"):
        assert editor.get(f"{path}/{integration.id}{suffix}").status_code == 404
    assert editor.patch(f"{path}/{integration.id}", {"name": "x"}).status_code == 404
    assert editor.post(f"{path}/{integration.id}/archive").status_code == 404


def test_responsible_counts_ignore_integrations(a, api, integration):
    Resource.objects.filter(id=integration.id).update(technical_owner=a["people"]["dana"])
    people = api(a["users"]["admin"]).get("people").json_body
    dana = next(p for p in people if p["id"] == str(a["people"]["dana"].id))
    assert dana["responsible"] == 0


def test_create_resource_rejects_integration_with_kind_text(a):
    before = Resource.objects.count()
    with pytest.raises(services.FieldErrors) as caught:
        services.create_resource(
            a["org"], a["users"]["editor"], {"name": "X", "lifecycle": "active", "kind": "integration"}
        )
    assert caught.value.errors["kind"] == ["Choose application, component or ai_agent."]
    assert Resource.objects.count() == before


def test_an_integration_has_exactly_its_own_extension_row(a, integration):
    assert integration.extension is not None and integration.extension.resource_id == integration.id
    services.check_one_extension(Resource.objects.get(id=integration.id))


def test_extension_checks_refuse_a_missing_or_foreign_row(a):
    bare = Resource.objects.create(
        organization=a["org"], kind=Kind.INTEGRATION, name="Bare", slug="bare", lifecycle="active"
    )
    with pytest.raises(ValueError):
        services.check_one_extension(bare)  # an integration without its row
    with pytest.raises(ValueError):
        services.create_extension(bare)  # an integration can't be made without its two ends
    app = Resource.objects.get(id=a["app"].id)
    app.kind = Kind.INTEGRATION
    with pytest.raises(ValueError):
        services.check_one_extension(app)  # has an application row, which isn't its kind


def test_an_integration_is_never_a_gap(integration):
    assert ownership_gaps(integration) == ["No technical owner", "No support team"]
    assert "No criticality" not in all_gaps(integration)
