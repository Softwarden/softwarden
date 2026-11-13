"""API tokens: creation, bearer authentication, role caps, audit."""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from api.models import ApiToken
from core.dates import org_today
from core.models import AuditEvent, Membership

APP = {"name": "Token Test App", "lifecycle": "active"}


def expiry(org, days=30):
    return (org_today(org) + timedelta(days=days)).isoformat()


class Bearer:
    """A client that sends only the Authorization header."""

    def __init__(self, secret, enforce_csrf=True):
        self.client = Client(enforce_csrf_checks=enforce_csrf, HTTP_AUTHORIZATION=f"Bearer {secret}")

    def get(self, url, **extra):
        return self.client.get("/api/v1/" + url, **extra)

    def post(self, url, data):
        return self.client.post("/api/v1/" + url, json.dumps(data), content_type="application/json")

    def patch(self, url, data):
        return self.client.patch("/api/v1/" + url, json.dumps(data), content_type="application/json")


@pytest.fixture
def mint(a, api):
    def go(role, read_only=True, **kw):
        body = api(a["users"]["admin"]).post(
            "api-tokens",
            {
                "user": a["users"][role].id,
                "name": "Script",
                "read_only": read_only,
                "expires_on": expiry(a["org"], kw.pop("days", 30)),
            },
        )
        assert body.status_code == 201, body.json_body
        return body.json_body

    return go


def test_create_returns_secret_once_and_stores_only_the_hash(a, api, mint):
    created = mint("editor", read_only=False)
    secret = created["secret"]
    assert secret.startswith("sw_") and len(secret) == 43
    assert created["prefix"] == secret[:11]
    assert set(created) == {
        "id",
        "name",
        "user",
        "prefix",
        "read_only",
        "expires_on",
        "last_used_at",
        "revoked_at",
        "created_at",
        "created_by",
        "secret",
    }
    assert created["user"]["id"] == a["users"]["editor"].id
    assert created["created_by"]["id"] == a["users"]["admin"].id
    stored = ApiToken.objects.get(id=created["id"])
    assert stored.token_hash != secret and len(stored.token_hash) == 64

    listing = api(a["users"]["admin"]).get("api-tokens")
    assert listing.status_code == 200
    assert [t["id"] for t in listing.json_body] == [created["id"]]
    assert "secret" not in listing.json_body[0] and "token_hash" not in listing.json_body[0]
    assert secret not in json.dumps(listing.json_body)


def test_create_validates_owner_name_and_expiry(a, b, api):
    admin = api(a["users"]["admin"])
    ok = {"user": a["users"]["editor"].id, "name": "x", "read_only": True}
    org = a["org"]
    for bad, field in (
        ({**ok, "expires_on": None}, "expires_on"),
        ({**ok, "expires_on": expiry(org, 0)}, "expires_on"),  # today is not in the future
        ({**ok, "expires_on": expiry(org, -3)}, "expires_on"),
        ({**ok, "expires_on": expiry(org, 366)}, "expires_on"),
        ({**ok, "expires_on": "soon"}, "expires_on"),
        ({**ok, "expires_on": expiry(org), "name": " "}, "name"),
        ({**ok, "expires_on": expiry(org), "user": b["users"]["editor"].id}, "user"),  # another organization
        ({**ok, "expires_on": expiry(org), "user": 999999}, "user"),
        ({**ok, "expires_on": expiry(org), "user": None}, "user"),
        ({**ok, "expires_on": expiry(org), "read_only": "yes"}, "read_only"),
    ):
        response = admin.post("api-tokens", bad)
        assert response.status_code == 400, bad
        assert field in response.json_body["errors"], bad
    assert admin.post("api-tokens", {**ok, "expires_on": expiry(org, 365)}).status_code == 201

    viewer = a["users"]["viewer"]
    viewer.is_active = False
    viewer.save()
    assert admin.post("api-tokens", {**ok, "user": viewer.id, "expires_on": expiry(org)}).status_code == 400


def test_token_reads_as_its_owner_and_updates_last_used(a, mint):
    created = mint("viewer")
    assert created["last_used_at"] is None
    bearer = Bearer(created["secret"])
    response = bearer.get("applications")
    assert response.status_code == 200
    me = bearer.get("auth/me").json()
    assert me["user"]["email"] == "viewer@orga.test" and me["role"] == "viewer"
    token = ApiToken.objects.get(id=created["id"])
    assert token.last_used_at is not None
    # At most one write a minute.
    first = token.last_used_at
    bearer.get("applications")
    assert ApiToken.objects.get(id=created["id"]).last_used_at == first
    ApiToken.objects.filter(id=created["id"]).update(last_used_at=timezone.now() - timedelta(minutes=2))
    bearer.get("applications")
    last_used_at = ApiToken.objects.get(id=created["id"]).last_used_at
    assert first is not None and last_used_at is not None
    assert last_used_at > first - timedelta(minutes=1)


def test_read_only_token_is_refused_on_writes(a, mint):
    bearer = Bearer(mint("editor", read_only=True)["secret"])
    assert bearer.get("applications").status_code == 200
    for response in (
        bearer.post("applications", APP),
        bearer.patch(f"applications/{a['app'].id}", {"description": "x"}),
        bearer.client.delete(f"/api/v1/applications/{a['app'].id}"),
    ):
        assert response.status_code == 403
        assert response.json()["detail"] == "This token is read-only."


def test_writable_token_writes_as_an_editor_but_never_above_the_role(a, mint):
    editor = Bearer(mint("editor", read_only=False)["secret"])  # CSRF checks are on for the client
    created = editor.post("applications", APP)
    assert created.status_code == 201
    assert created.json()["name"] == APP["name"]
    viewer = Bearer(mint("viewer", read_only=False)["secret"])
    assert viewer.get("applications").status_code == 200
    refused = viewer.post("applications", {**APP, "name": "Another"})
    assert refused.status_code == 403 and "read-only" not in refused.json()["detail"]
    assert viewer.get("api-tokens").status_code == 403  # not an Admin


def test_a_token_cannot_manage_tokens(a, mint):
    admin = Bearer(mint("admin", read_only=False)["secret"])
    assert admin.get("api-tokens").status_code == 403
    assert admin.post("api-tokens", {}).status_code == 403


def test_revoked_expired_and_deactivated_owner_get_401(a, api, mint):
    admin = api(a["users"]["admin"])
    revoked = mint("editor")
    expired = mint("editor", days=5)
    deactivated = mint("viewer")

    assert admin.post(f"api-tokens/{revoked['id']}/revoke").status_code == 200
    ApiToken.objects.filter(id=expired["id"]).update(expires_on=org_today(a["org"]) - timedelta(days=1))
    assert admin.patch(f"users/{a['users']['viewer'].id}", {"active": False}).status_code == 200

    for created, text in ((revoked, "revoked"), (expired, "expired"), (deactivated, "no longer active")):
        response = Bearer(created["secret"]).get("applications")
        assert response.status_code == 401, text
        assert response["Content-Type"] == "application/problem+json"
        assert text in response.json()["detail"]
    assert Bearer("sw_" + "x" * 40).get("applications").status_code == 401


def test_expiry_day_itself_is_valid(a, mint):
    created = mint("viewer")
    ApiToken.objects.filter(id=created["id"]).update(expires_on=org_today(a["org"]))
    assert Bearer(created["secret"]).get("applications").status_code == 200


def test_owner_without_membership_in_the_token_organization_gets_401(a, b, mint):
    created = mint("viewer")
    Membership.objects.filter(user=a["users"]["viewer"]).update(organization=b["org"])
    assert Bearer(created["secret"]).get("applications").status_code == 401
    Membership.objects.filter(user=a["users"]["viewer"]).delete()
    assert Bearer(created["secret"]).get("applications").status_code == 401


def test_secret_in_query_string_or_cookie_or_other_scheme_is_ignored(a, mint):
    secret = mint("viewer")["secret"]
    anon = Client()
    for response in (
        anon.get("/api/v1/applications", {"token": secret}),
        anon.get("/api/v1/applications", {"access_token": secret, "api_key": secret}),
        anon.get("/api/v1/applications", HTTP_COOKIE=f"token={secret}; sw_token={secret}"),
        anon.get("/api/v1/applications", HTTP_AUTHORIZATION=f"Token {secret}"),
        anon.get("/api/v1/applications", HTTP_AUTHORIZATION=f"Basic {secret}"),
        anon.get("/api/v1/applications", HTTP_AUTHORIZATION=secret),
    ):
        assert response.status_code in (401, 403)
    assert ApiToken.objects.get(prefix=secret[:11]).last_used_at is None
    cookie_client = Client()
    cookie_client.cookies["sessionid"] = secret
    assert cookie_client.get("/api/v1/applications").status_code in (401, 403)


def test_non_token_bearer_values_fall_through_unauthenticated():
    response = Client().get("/api/v1/applications", HTTP_AUTHORIZATION="Bearer not-ours")
    assert response.status_code in (401, 403)


def test_session_still_works_and_enforces_csrf(a, api):
    signed_in = api(a["users"]["editor"], enforce_csrf=True)
    assert signed_in.get("applications").status_code == 200
    assert signed_in.post("applications", APP).status_code == 403  # no CSRF header, as before


def test_audit_records_create_and_revoke_without_the_secret(a, api, mint):
    created = mint("editor", read_only=False)
    api(a["users"]["admin"]).post(f"api-tokens/{created['id']}/revoke")
    events = AuditEvent.objects.filter(organization=a["org"], action__startswith="token.").order_by("created_at")
    assert [e.action for e in events] == ["token.created", "token.revoked"]
    for event in events:
        assert event.actor_id == a["users"]["admin"].id
        assert str(event.target_id) == created["id"]
        assert event.changes == {
            "name": "Script",
            "owner": a["users"]["editor"].id,
            "read_only": False,
            "expires_on": created["expires_on"],
        }
    everything = json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)), default=str)
    assert created["secret"] not in everything
    assert ApiToken.objects.get(id=created["id"]).token_hash not in everything


def test_revoke_twice_is_harmless_and_audited_once(a, api, mint):
    created = mint("viewer")
    admin = api(a["users"]["admin"])
    first = admin.post(f"api-tokens/{created['id']}/revoke").json_body
    assert first["revoked_at"] is not None
    assert admin.post(f"api-tokens/{created['id']}/revoke").status_code == 200
    assert AuditEvent.objects.filter(action="token.revoked").count() == 1


# ---- role matrix and tenancy ----------------------------------------------------------------------------


@pytest.mark.parametrize("role,allowed", [("admin", True), ("editor", False), ("viewer", False)])
def test_role_matrix_for_token_management(a, api, mint, role, allowed):
    created = mint("viewer")
    client = api(a["users"][role])
    body = {"user": a["users"]["viewer"].id, "name": "n", "expires_on": expiry(a["org"])}
    calls = (
        client.get("api-tokens"),
        client.post("api-tokens", body),
        client.post(f"api-tokens/{created['id']}/revoke"),
    )
    assert [c.status_code for c in calls] == ([200, 201, 200] if allowed else [403, 403, 403])


def test_signed_out_is_refused(a, mint):
    created = mint("viewer")
    anon = Client()
    assert anon.get("/api/v1/api-tokens").status_code in (401, 403)
    assert anon.post(f"/api/v1/api-tokens/{created['id']}/revoke").status_code in (401, 403)


def test_cross_tenant_tokens_are_invisible(a, b, api, mint):
    created = mint("viewer")
    other = api(b["users"]["admin"])
    assert other.get("api-tokens").json_body == []
    assert other.post(f"api-tokens/{created['id']}/revoke").status_code == 404
    assert ApiToken.objects.get(id=created["id"]).revoked_at is None
    assert other.post("api-tokens/00000000-0000-0000-0000-000000000000/revoke").status_code == 404
    # A token never reaches another organization's records.
    secret = created["secret"]
    assert Bearer(secret).get(f"applications/{b['app'].id}").status_code == 404


def test_openapi_describes_the_token_endpoints(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths = doc["paths"]
    assert {"get", "post"} <= set(paths["/api/v1/api-tokens"])
    assert "post" in paths["/api/v1/api-tokens/{id}/revoke"]
    assert {"201", "400", "403"} <= set(paths["/api/v1/api-tokens"]["post"]["responses"])
    assert {"200", "403", "404"} <= set(paths["/api/v1/api-tokens/{id}/revoke"]["post"]["responses"])
    assert "secret" in doc["components"]["schemas"]["ApiTokenCreated"]["properties"]
    assert "secret" not in doc["components"]["schemas"]["ApiToken"]["properties"]


# ---- A token names itself in the audit log and doesn't outlive its creator's Admin role ----------------------


@pytest.fixture
def admin2(a):
    """A second Admin of organization a."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="admin2@orga.test", email="admin2@orga.test", password="correct-horse-battery"
    )
    Membership.objects.create(user=user, organization=a["org"], role="admin")
    return user


def mint_for_admin1(a, api, creator):
    body = api(creator).post(
        "api-tokens",
        {"user": a["users"]["admin"].id, "name": "Planted", "read_only": False, "expires_on": expiry(a["org"])},
    )
    assert body.status_code == 201, body.json_body
    return body.json_body


def test_an_audit_event_written_through_a_token_names_the_token(a, api, mint):
    created = mint("editor", read_only=False)
    response = Bearer(created["secret"]).post("applications", APP)
    assert response.status_code == 201
    event = AuditEvent.objects.get(organization=a["org"], action="resource.created", target_id=response.json()["id"])
    assert event.actor_id == a["users"]["editor"].id
    assert event.changes["api_client"] == "Script" and event.changes["api_client_id"] == created["id"]
    log = api(a["users"]["admin"]).get("audit-events?action=resource.created").json_body["results"]
    assert log[0]["summary"].endswith("(with API token Script)")
    # A later request signed in with a session isn't attributed to the token.
    session = api(a["users"]["editor"]).post("applications", {**APP, "name": "By session"})
    later = AuditEvent.objects.get(action="resource.created", target_id=session.json_body["id"])
    assert "api_client" not in later.changes and "api_client_id" not in later.changes


@pytest.mark.parametrize("change", [{"role": "editor"}, {"active": False}])
def test_a_token_minted_for_another_member_dies_when_its_creator_loses_admin(a, api, admin2, change):
    created = mint_for_admin1(a, api, admin2)
    assert Bearer(created["secret"]).get("users").status_code == 200
    own = (
        api(admin2)
        .post("api-tokens", {"user": admin2.id, "name": "Mine", "read_only": True, "expires_on": expiry(a["org"])})
        .json_body
    )
    assert api(a["users"]["admin"]).patch(f"users/{admin2.id}", change).status_code == 200
    assert Bearer(created["secret"]).get("users").status_code == 401
    assert ApiToken.objects.get(id=created["id"]).revoked_at is not None
    event = AuditEvent.objects.get(action="token.revoked", target_id=created["id"])
    assert event.actor_id == a["users"]["admin"].id and event.changes["reason"] == "creator_no_longer_admin"
    # Their own token isn't one they made for someone else: it follows their own role and state, as before.
    assert ApiToken.objects.get(id=own["id"]).revoked_at is None


def test_a_token_minted_for_another_member_dies_when_its_creator_is_removed(a, b, api, admin2):
    Membership.objects.create(user=admin2, organization=b["org"], role="viewer")
    created = mint_for_admin1(a, api, admin2)
    assert api(a["users"]["admin"]).delete(f"users/{admin2.id}").status_code == 204
    assert Bearer(created["secret"]).get("users").status_code == 401
    assert ApiToken.objects.get(id=created["id"]).revoked_at is not None
    assert AuditEvent.objects.get(action="token.revoked", target_id=created["id"]).changes["reason"] == (
        "creator_removed"
    )


def test_a_creator_demoted_outside_the_member_service_still_stops_the_token(a, api, admin2):
    """Backstop: whatever path changes the creator's role (SCIM role mapping, a future one), authentication refuses a
    token they made for someone else once they are no longer an active Admin of the token's organization."""
    created = mint_for_admin1(a, api, admin2)
    Membership.objects.filter(user=admin2, organization=a["org"]).update(role="editor")
    response = Bearer(created["secret"]).get("users")
    assert response.status_code == 401 and "created" in response.json()["detail"]
