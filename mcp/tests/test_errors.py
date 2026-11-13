"""Problem details from the API read as tool errors a model can act on, and nothing in them carries the token."""

from __future__ import annotations

import re

import httpx2
import pytest
from conftest import READ_ONLY, FakeSoftwarden, make_client, problem

from softwarden_mcp import tools
from softwarden_mcp.client import (
    ApiError,
    ConfigError,
    SoftwardenClient,
    SoftwardenError,
    explain,
    normalize_base,
    verify_setting,
)


def refusal(fake: FakeSoftwarden, path: str, answer: tuple[int, dict[str, str], object]) -> ApiError:
    fake.gets[path] = answer
    client = make_client(fake)
    try:
        with pytest.raises(ApiError) as caught:
            client.get(path)
    finally:
        client.close()
    assert READ_ONLY not in str(caught.value) and READ_ONLY[3:] not in str(caught.value)
    return caught.value


def test_a_bad_token_says_so(fake: FakeSoftwarden) -> None:
    client = make_client(fake, "sw_" + "z" * 40)
    with pytest.raises(ApiError) as caught:
        tools.renewals_with_owners(client)
    assert caught.value.status == 401 and caught.value.problem == "invalid-token"
    assert "refused the API token" in str(caught.value) and "Administration > API tokens" in str(caught.value)
    assert "zzzz" not in str(caught.value)


def test_a_missing_capability_names_what_the_edition_lacks(fake: FakeSoftwarden) -> None:
    error = refusal(
        fake, "composite/spend", problem(403, "capability-required", "Needs contracts.", capability="contracts")
    )
    assert str(error) == (
        "Your Softwarden edition doesn't include contracts (capability `contracts`), so this isn't available."
    )
    other = refusal(fake, "composite/data-gaps", problem(403, "capability-required", "x", capability="sso_audit"))
    assert "doesn't include sso audit (capability `sso_audit`)" in str(other)


def test_rate_limits_give_a_retry_hint(fake: FakeSoftwarden) -> None:
    status, headers, body = problem(429, "rate-limited", "Too many requests.")
    error = refusal(fake, "composite/search", (status, {**headers, "Retry-After": "17"}, body))
    assert str(error) == "Softwarden's rate limit was reached; retry in 17 seconds."
    assert "wait a minute and retry" in explain(429, {}, {})


def test_other_problems(fake: FakeSoftwarden) -> None:
    bad = problem(400, "invalid-request", "Some fields need attention.", errors={"within_days": ["Enter 0 to 730."]})
    assert (
        str(refusal(fake, "composite/renewals", bad)) == "Softwarden refused the request: within_days: Enter 0 to 730."
    )
    forbidden = refusal(fake, "composite/deadlines", problem(403, "forbidden", "Editors and Admins only."))
    assert str(forbidden) == "Your role in Softwarden can't do this. Editors and Admins only."
    assert "no such item" in str(refusal(fake, "people/x", problem(404, "not-found", "Not found.")))
    conflict = refusal(fake, "x/y", problem(409, "conflict", "Already decided."))
    assert str(conflict) == "Softwarden refused the change: Already decided."
    server = refusal(fake, "boom", (500, {"X-Request-ID": "req-1"}, {"type": "/problems/server-error"}))
    assert str(server) == "Softwarden had an internal error (request req-1); try later."
    assert "no API token" in explain(403, {"type": "/problems/not-signed-in"}, {})


def test_a_stale_write_never_hands_the_model_the_new_revision(fake: FakeSoftwarden) -> None:
    """With the current revision in the 412 text, the cheapest path for a model is to paste it and retry,
    overwriting an edit it never saw. The message says to read the item again and nothing else about the revision."""
    current = "5cacb658" + "0" * 24
    status, headers, body = problem(412, "precondition-failed", "Changed since.", revision=current)
    error = refusal(fake, "applications/x", (status, {**headers, "ETag": f'"{current}"'}, body))
    assert error.status == 412
    assert not re.search(r"[0-9a-fA-F]{32}", str(error)), str(error)
    assert current not in str(error) and "5cacb658" not in str(error)
    assert "Read it again with get_item" in str(error)
    assert current not in explain(412, {"revision": current}, {"etag": current})


def test_unreachable_and_redirects(fake: FakeSoftwarden) -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    client = SoftwardenClient("https://softwarden.test/", READ_ONLY, transport=httpx2.MockTransport(down))
    with pytest.raises(SoftwardenError, match=r"Couldn't reach Softwarden at https://softwarden.test \(ConnectError\)"):
        client.get("auth/me")

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    client = SoftwardenClient("https://softwarden.test", READ_ONLY, transport=httpx2.MockTransport(slow))
    with pytest.raises(SoftwardenError, match="didn't answer in time"):
        client.get("auth/me")

    def moved(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "https://elsewhere.test/"})

    client = SoftwardenClient("https://softwarden.test", READ_ONLY, transport=httpx2.MockTransport(moved))
    with pytest.raises(SoftwardenError, match="redirected the request"):
        client.get("auth/me")


def test_configuration() -> None:
    assert normalize_base("https://sw.example.com/api/v1/") == "https://sw.example.com"
    with pytest.raises(ConfigError, match="https://"):
        normalize_base("sw.example.com")
    with pytest.raises(ConfigError, match="SOFTWARDEN_TOKEN"):
        SoftwardenClient("https://sw.example.com", " ")
    with pytest.raises(ConfigError, match="SOFTWARDEN_URL"):
        SoftwardenClient.from_env({"SOFTWARDEN_TOKEN": READ_ONLY})
    made = SoftwardenClient.from_env(
        {"SOFTWARDEN_URL": "http://app:8000", "SOFTWARDEN_TOKEN": READ_ONLY, "SOFTWARDEN_HOST_HEADER": "sw.example.com"}
    )
    assert str(made.http.base_url) == "http://app:8000/api/v1/"
    assert made.http.headers["Host"] == "sw.example.com"
    assert made.http.headers["Authorization"] == f"Bearer {READ_ONLY}"
    made.close()
    assert verify_setting(None) is True and verify_setting("false") is False
    assert verify_setting("/etc/ssl/internal-ca.pem") == "/etc/ssl/internal-ca.pem"
