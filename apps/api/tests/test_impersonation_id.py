"""Impersonation id: a write names the impersonation its tab is in (X-Softwarden-Impersonation: <id> or
`none`), so a tab left open from an earlier impersonation, or from before Stop, can't write as the wrong member or as
the staff account (core/impersonation.py)."""

from __future__ import annotations

import json
import uuid

import pytest

from catalog.models import Resource
from core import impersonation
from core.models import AuditEvent, Membership
from tests.test_platform import make_staff, start
from tests.test_problems import is_problem

ENDED = "/problems/impersonation-ended"
STARTED = "/problems/impersonation-started"
RESTRICTED = "/problems/impersonation-restricted"


@pytest.fixture(autouse=True)
def empty_cache():
    """Starting an impersonation shares the sign-in throttle, which counts in the process cache across tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db, b):
    user = make_staff()
    Membership.objects.create(user=user, organization=b["org"], role="admin")  # the staff account's own organization
    return user


def write(client, method: str, url: str, header: str | None, data: dict | None = None):
    extra = {} if header is None else {"HTTP_X_SOFTWARDEN_IMPERSONATION": header}
    return getattr(client.client, method)(
        "/api/v1/" + url, json.dumps(data or {}), content_type="application/json", **extra
    )


def create_app(client, name: str, header: str | None):
    return write(client, "post", "applications", header, {"name": name, "lifecycle": "active"})


def current_id(client) -> str:
    return client.client.session[impersonation.SESSION_KEY]["id"]


def test_start_and_auth_me_return_the_id(a, staff, api):
    client = api(staff)
    started = start(client, a["org"], a["users"]["editor"]).json()
    ident = started["impersonation"]["id"]
    assert uuid.UUID(ident) and ident == current_id(client)
    me = client.get("auth/me").json_body
    assert me["impersonation"]["id"] == ident
    assert client.get("platform/impersonation").json_body["impersonation"]["id"] == ident


def test_the_matching_id_passes_in_either_form(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    ident = current_id(client)
    assert create_app(client, "Dashed", ident).status_code == 201
    assert create_app(client, "Hex", uuid.UUID(ident).hex).status_code == 201
    made = Resource.objects.filter(name__in=["Dashed", "Hex"])
    assert {r.organization_id for r in made} == {a["org"].id}
    assert AuditEvent.objects.filter(action="resource.created", impersonated_by=staff).count() == 2


def test_a_stale_id_from_an_earlier_impersonation_writes_nothing(a, b, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    first = current_id(client)
    assert client.delete("platform/impersonation").status_code == 200
    start(client, a["org"], a["users"]["admin"])  # the second, as someone else
    second = current_id(client)
    assert first != second
    body = is_problem(create_app(client, "From the first tab", first), 409, ENDED)
    assert "nothing was done" in body["detail"]
    is_problem(write(client, "patch", f"applications/{a['app'].id}", first, {"description": "x"}), 409, ENDED)
    is_problem(create_app(client, "Not an id", "garbage"), 409, ENDED)
    assert not Resource.objects.filter(name__in=["From the first tab", "Not an id"]).exists()
    assert impersonation.SESSION_KEY in client.client.session  # the refusal doesn't end the current one
    assert create_app(client, "From the second tab", second).status_code == 201


def test_an_id_after_stop_writes_nothing_and_keeps_the_mark(a, b, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    ident = current_id(client)
    assert client.delete("platform/impersonation").status_code == 200
    is_problem(create_app(client, "After stop", ident), 409, ENDED)
    assert not Resource.objects.filter(name="After stop").exists()
    # After an expiry a read noticed, a refused stale id leaves the mark for a header-less (older bundle) tab.
    start(client, a["org"], a["users"]["editor"])
    ident = current_id(client)
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = "2000-01-01T00:00:00+00:00"
    session[impersonation.SESSION_KEY] = state
    session.save()
    client.get("platform/session")
    assert impersonation.ENDED_NOTICE in client.client.session
    is_problem(create_app(client, "Stale tab", ident), 409, ENDED)
    assert impersonation.ENDED_NOTICE in client.client.session
    is_problem(create_app(client, "Old bundle tab", None), 409, ENDED)  # the mark still does its job, once
    assert impersonation.ENDED_NOTICE not in client.client.session
    assert create_app(client, "Staff's own", None).status_code == 201
    assert Resource.objects.get(name="Staff's own").organization_id == b["org"].id
    assert not Resource.objects.filter(name__in=["Stale tab", "Old bundle tab"]).exists()


def test_none_during_an_impersonation_is_refused_as_started(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    body = is_problem(create_app(client, "From the console tab", "none"), 409, STARTED)
    assert "Reload" in body["detail"]
    is_problem(create_app(client, "Upper case", "NONE"), 409, STARTED)
    assert not Resource.objects.filter(name__in=["From the console tab", "Upper case"]).exists()
    assert client.get("applications").status_code == 200  # reads never carry a claim that matters


def test_none_when_not_impersonating_passes_and_leaves_the_mark(a, b, staff, api):
    client = api(staff)
    assert create_app(client, "Mine", "none").status_code == 201
    start(client, a["org"], a["users"]["editor"])
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = "2000-01-01T00:00:00+00:00"
    session[impersonation.SESSION_KEY] = state
    session.save()
    client.get("platform/session")
    # A tab that says it isn't impersonating is the staff account's own: it writes, and the mark stays for any
    # header-less client.
    assert create_app(client, "Mine again", "none").status_code == 201
    assert impersonation.ENDED_NOTICE in client.client.session
    is_problem(create_app(client, "Header-less", None), 409, ENDED)


def test_without_the_header_nothing_changes(a, b, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    assert create_app(client, "No header during", None).status_code == 201
    assert Resource.objects.get(name="No header during").organization_id == a["org"].id
    assert client.delete("platform/impersonation").status_code == 200
    assert create_app(client, "No header after", None).status_code == 201
    assert Resource.objects.get(name="No header after").organization_id == b["org"].id


def test_stop_works_with_a_stale_id_or_none(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    first = current_id(client)
    client.delete("platform/impersonation")
    start(client, a["org"], a["users"]["admin"])
    response = write(client, "delete", "platform/impersonation", first)
    assert response.status_code == 200 and response.json()["active"] is False
    start(client, a["org"], a["users"]["admin"])
    assert write(client, "delete", "platform/impersonation", "none").status_code == 200
    # Other console writes while impersonating keep the platform permission's refusal, whatever the header.
    start(client, a["org"], a["users"]["admin"])
    is_problem(
        write(client, "post", f"platform/organizations/{a['org'].id}/suspend", "none", {"reason": "x"}), 403, RESTRICTED
    )


@pytest.mark.parametrize("header", ["none", "stale", "garbage", None])
def test_signing_out_always_works(a, staff, api, header):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    value = str(uuid.uuid4()) if header == "stale" else header
    assert write(client, "post", "auth/logout", value).status_code == 204
    assert (
        AuditEvent.objects.get(organization=a["org"], action="impersonation.stopped").changes["reason"] == "signed_out"
    )
    # Signed out now: signing out again is never an impersonation refusal (only "not signed in").
    is_problem(write(client, "post", "auth/logout", value), 403, "/problems/not-signed-in")


def test_a_session_without_staff_is_unaffected_by_none(a, api):
    editor = api(a["users"]["editor"])
    assert create_app(editor, "Ordinary", "none").status_code == 201
    is_problem(create_app(editor, "Ordinary stale", str(uuid.uuid4())), 409, ENDED)


def test_a_stale_id_on_a_read_is_ignored(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    response = client.client.get("/api/v1/applications", HTTP_X_SOFTWARDEN_IMPERSONATION=str(uuid.uuid4()))
    assert response.status_code == 200


def test_a_stale_id_on_a_console_write_when_not_impersonating_passes(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    ident = current_id(client)
    client.delete("platform/impersonation")
    response = write(client, "post", f"platform/organizations/{a['org'].id}/suspend", ident, {"reason": "Unpaid"})
    assert response.status_code == 200 and response.json()["suspended_at"]


@pytest.mark.parametrize("header", ["current", "none", "stale", None])
def test_the_write_that_notices_the_end_is_refused_whatever_the_header(a, staff, api, header):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    ident = current_id(client)
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = "2000-01-01T00:00:00+00:00"
    session[impersonation.SESSION_KEY] = state
    session.save()
    value = {"current": ident, "none": "none", "stale": str(uuid.uuid4()), None: None}[header]
    is_problem(create_app(client, "As the end is noticed", value), 409, ENDED)
    assert not Resource.objects.filter(name="As the end is noticed").exists()
    assert impersonation.SESSION_KEY not in client.client.session


@pytest.mark.parametrize("header", ["none", "missing", "stale", None])
def test_a_record_without_an_id_matches_no_header(a, staff, api, header):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    del state["id"]
    session[impersonation.SESSION_KEY] = state
    session.save()
    value = {"none": "none", "missing": "missing", "stale": str(uuid.uuid4()), None: None}[header]
    response = create_app(client, "Id-less", value)
    if header is None:
        assert response.status_code == 201  # no header: as before
    else:
        is_problem(response, 409, STARTED if header == "none" else ENDED)
        assert not Resource.objects.filter(name="Id-less").exists()
