"""Record field validation: bad input is a field error (400), never a stored value or a 500."""

import pytest

from catalog.models import Resource

BAD = [
    ("criticality", "bogus", "criticality"),
    ("criticality", "x" * 30, "criticality"),
    ("criticality", ["high"], "criticality"),
    ("name", "n" * 300, "name"),
    ("name", "   ", "name"),
    ("name", 42, "name"),
    ("documentation_url", "javascript:alert(1)", "documentation_url"),
    ("documentation_url", "http://example.test/" + "a" * 300, "documentation_url"),
    ("support_url", "ftp://example.test/help", "support_url"),
    ("description", "d" * 5001, "description"),
    ("purpose", "p" * 5001, "purpose"),
]


@pytest.mark.parametrize("field,value,error_field", BAD)
def test_application_patch_rejects_bad_values(a, api, field, value, error_field):
    editor = api(a["users"]["editor"])
    before = Resource.objects.values().get(id=a["app"].id)
    r = editor.patch(f"applications/{a['app'].id}", {field: value})
    assert r.status_code == 400
    assert r["Content-Type"].startswith("application/problem+json")
    assert error_field in r.json_body["errors"]
    assert Resource.objects.values().get(id=a["app"].id) == before


@pytest.mark.parametrize("field,value,error_field", BAD)
def test_application_create_rejects_bad_values(a, api, field, value, error_field):
    editor = api(a["users"]["editor"])
    count = Resource.objects.count()
    r = editor.post("applications", {"name": "Valid", "lifecycle": "active", field: value})
    assert r.status_code == 400
    assert error_field in r.json_body["errors"]
    assert Resource.objects.count() == count


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_url", "javascript:alert(1)"),
        ("repository_url", "https://example.test/" + "a" * 300),
        ("documentation_url", "data:text/html,x"),
        ("host_runtime", "h" * 201),
        ("criticality", "bogus"),
    ],
)
def test_component_patch_rejects_bad_values(a, api, field, value):
    editor = api(a["users"]["editor"])
    created = editor.post("components", {"name": "Nightly job", "lifecycle": "active"})
    assert created.status_code == 201
    r = editor.patch(f"components/{created.json_body['id']}", {field: value})
    assert r.status_code == 400
    assert field in r.json_body["errors"]


def test_good_values_and_clearing_still_work(a, api):
    editor = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    r = editor.patch(
        url,
        {"criticality": "high", "documentation_url": "https://example.test/docs", "name": "n" * 200},
    )
    assert r.status_code == 200
    r = editor.patch(url, {"criticality": "", "documentation_url": ""})
    assert r.status_code == 200
    assert r.json_body["criticality"] in ("", None)


def test_a_review_without_a_person_is_a_field_error(a, api):
    client = api(a["users"]["editor"])
    for body in ({"evidence": "Email from Dana"}, {"confirmed_by": "", "evidence": "Email from Dana"}):
        response = client.post(f"applications/{a['app'].id}/reviews", body)
        assert response.status_code == 400, response.content
        assert response.json_body["errors"]["confirmed_by"] == ["Choose who confirmed it."]
