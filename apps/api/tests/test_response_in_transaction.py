"""A write view builds its response inside the write's transaction, as TenantViewSet does.

If rendering the stored record fails, the request fails and has written nothing, rather than committing a change the
client was told failed. Each test makes `detail_response` raise on one of the write views outside TenantViewSet's own
create/update that used to render after their transaction committed, then checks the write was rolled back."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from api.models import ApiToken
from catalog.models import Person
from core.models import AuditEvent
from imports.models import ImportRun
from money.models import Contract


class RenderFailed(Exception):
    pass


def failing_render(monkeypatch, viewset):
    def render(self, *args, **kwargs):
        raise RenderFailed

    monkeypatch.setattr(viewset, "detail_response", render)


def test_api_token_create(a, api, monkeypatch):
    from api.viewsets import ApiTokenViewSet
    from core.dates import org_today

    failing_render(monkeypatch, ApiTokenViewSet)
    body = {"user": a["users"]["editor"].id, "name": "Script", "read_only": True}
    body["expires_on"] = (org_today(a["org"]) + timedelta(days=30)).isoformat()
    with pytest.raises(RenderFailed):
        api(a["users"]["admin"]).post("api-tokens", body)
    assert not ApiToken.objects.exists()
    assert not AuditEvent.objects.filter(action="token.created").exists()


def test_contract_archive(a, api, monkeypatch):
    from money.contract_viewsets import ContractViewSet

    editor = api(a["users"]["editor"])
    contract = editor.post("contracts", {"name": "Support"}).json_body["id"]
    failing_render(monkeypatch, ContractViewSet)
    with pytest.raises(RenderFailed):
        editor.post(f"contracts/{contract}/archive")
    assert Contract.objects.get(id=contract).archived_at is None


def test_import_upload(a, api, monkeypatch):
    from imports.viewsets import ImportRunViewSet

    failing_render(monkeypatch, ImportRunViewSet)
    upload = SimpleUploadedFile("apps.csv", b"name,lifecycle\nZoom,active\n", content_type="text/csv")
    with pytest.raises(RenderFailed):
        api(a["users"]["editor"]).client.post("/api/v1/imports", {"file": upload, "kind": "application"})
    assert not ImportRun.objects.exists()


def test_person_anonymize(a, api, monkeypatch):
    from catalog.directory_viewsets import PersonViewSet

    person = Person.objects.create(organization=a["org"], name="Sam Doe", email="sam@example.test")
    failing_render(monkeypatch, PersonViewSet)
    with pytest.raises(RenderFailed):
        api(a["users"]["admin"]).post(f"people/{person.id}/anonymize", {"confirm_name": "Sam Doe"})
    assert Person.objects.get(id=person.id).name == "Sam Doe"
