"""A second factor (TOTP, core/totp.py) for platform staff, required for the console when
SOFTWARDEN_PLATFORM_TOTP is on (the default in cloud mode): enrollment at the first console sign-in, verification at
every sign-in and again at POST platform/impersonation, the operator's audited reset, and the secret as an encrypted
credentials slot. The per-account sign-in limit that goes with it is tests/test_shared_limits.py
(test_the_eleventh_failed_sign_in_for_one_account_in_a_minute_is_refused)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import struct
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from catalog import staff_second_factor
from core import credentials, totp
from core.models import PlatformAuditEvent, PlatformStaff
from tests.test_platform import PASSWORD, make_staff

START = 1_800_000_000 - (1_800_000_000 % 3600)
REQUIRED_TYPE = "/problems/second-factor-required"


@pytest.fixture
def clock(monkeypatch):
    """TOTP's and the counters' clocks, held at the start of an hour; move with now[0] += seconds."""
    from core import counters

    now = [float(START)]
    monkeypatch.setattr(totp, "clock", lambda: now[0])
    monkeypatch.setattr(counters, "clock", lambda: now[0])
    return now


@pytest.fixture
def required(settings):
    settings.SOFTWARDEN_PLATFORM_TOTP = True
    return settings


def post(client, path, body=None):
    return client.post("/api/v1/" + path, json.dumps(body or {}), content_type="application/json")


def sign_in(email=None, password=PASSWORD):
    client = Client()
    response = post(client, "auth/login", {"email": email or "ops@platform.test", "password": password})
    return client, response


def code(secret, now, offset=0):
    return totp.code_at(secret, int(now // totp.STEP) + offset)


def enroll(client, now):
    response = post(client, "platform/second-factor/enrollment")
    assert response.status_code == 201, response.content
    secret = response.json()["secret"]
    assert post(client, "platform/second-factor/verify", {"code": code(secret, now)}).status_code == 200
    return secret


# ---- the algorithm --------------------------------------------------------------------------------------------------


def test_totp_matches_the_rfc_6238_test_vectors():
    """RFC 6238 appendix B, SHA-1 (the 20-byte ASCII key "12345678901234567890"), last six digits."""
    secret = base64.b32encode(b"12345678901234567890").decode()
    vectors = {59: "287082", 1111111109: "081804", 1111111111: "050471", 1234567890: "005924", 2000000000: "279037"}
    for at, expected in vectors.items():
        assert totp.code_at(secret, at // 30) == expected
        assert totp.matching_step(secret, expected, now=at) == at // 30


def test_a_code_one_step_either_side_is_accepted_and_no_further():
    secret = totp.new_secret()
    assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20
    now = START + 15
    for offset in (-1, 0, 1):
        assert totp.matching_step(secret, code(secret, now, offset), now=now) == now // 30 + offset
    for offset in (-2, 2):
        assert totp.matching_step(secret, code(secret, now, offset), now=now) is None
    for bad in (None, 123456, "", "12345", "1234567", "abcdef", "１２３４５６"):
        assert totp.matching_step(secret, bad, now=now) is None


def test_the_provisioning_uri_is_what_authenticator_apps_read():
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", "ops@platform.test")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp" and parts.path == "/Softwarden:ops@platform.test"
    query = parse_qs(parts.query)
    assert query == {
        "secret": ["JBSWY3DPEHPK3PXP"],
        "issuer": ["Softwarden"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }
    # The same HOTP an independent implementation computes.
    key = base64.b32decode("JBSWY3DPEHPK3PXP")
    digest = hmac.new(key, struct.pack(">Q", 1), hashlib.sha1).digest()
    offset = digest[-1] & 15
    assert totp.code_at("JBSWY3DPEHPK3PXP", 1) == str(
        (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**6
    ).zfill(6)


# ---- the acceptance: no second factor, no platform session ---------------------------------------------------------


def test_staff_without_a_second_factor_get_no_platform_session(db, required, clock):
    make_staff()
    client, response = sign_in()
    assert response.status_code == 200
    body = response.json()
    assert body["platform_staff"] is True
    assert body["second_factor"] == {"required": True, "enrolled": False, "verified": False}
    for path in ("platform/organizations", "platform/audit-events", "platform/impersonation", "platform/capabilities"):
        refused = client.get("/api/v1/" + path)
        assert refused.status_code == 403, path
        assert refused.json()["type"] == REQUIRED_TYPE and refused.json()["enrolled"] is False
    # What it may reach: its session and the second factor's own routes.
    assert client.get("/api/v1/platform/session").status_code == 200
    assert client.get("/api/v1/platform/second-factor").json() == {
        "required": True,
        "enrolled": False,
        "verified": False,
    }


def test_enrollment_at_the_first_sign_in_then_a_code_at_every_sign_in(db, required, clock):
    staff = make_staff()
    client, _ = sign_in()
    started = post(client, "platform/second-factor/enrollment")
    assert started.status_code == 201
    secret, uri = started.json()["secret"], started.json()["otpauth_uri"]
    assert uri.startswith("otpauth://totp/") and f"secret={secret}" in uri
    row = PlatformStaff.objects.get(user=staff)
    assert row.totp_ciphertext and secret not in row.totp_ciphertext and row.totp_confirmed_at is None
    assert credentials.slot_value(row, "totp") == secret
    # Asking again before confirming replaces the secret; the first one no longer works.
    again = post(client, "platform/second-factor/enrollment").json()["secret"]
    assert again != secret
    wrong = post(client, "platform/second-factor/verify", {"code": code(secret, clock[0])})
    assert wrong.status_code == 400 and "code" in wrong.json()["errors"]
    assert client.get("/api/v1/platform/organizations").status_code == 403
    confirmed = post(client, "platform/second-factor/verify", {"code": code(again, clock[0])})
    assert confirmed.status_code == 200 and confirmed.json() == {"required": True, "enrolled": True, "verified": True}
    assert client.get("/api/v1/platform/organizations").status_code == 200
    # Enrolled: no second enrollment, and the secret is never shown again.
    assert post(client, "platform/second-factor/enrollment").status_code == 409
    assert again not in client.get("/api/v1/platform/second-factor").content.decode()
    # The next sign-in needs a code again; the code just used is refused (replay), the next step's passes.
    other, response = sign_in()
    assert response.json()["second_factor"] == {"required": True, "enrolled": True, "verified": False}
    assert other.get("/api/v1/platform/organizations").status_code == 403
    replay = post(other, "platform/second-factor/verify", {"code": code(again, clock[0])})
    assert replay.status_code == 400 and "already used" in replay.json()["errors"]["code"][0]
    clock[0] += 30
    assert post(other, "platform/second-factor/verify", {"code": code(again, clock[0])}).status_code == 200
    assert other.get("/api/v1/platform/organizations").status_code == 200
    actions = list(PlatformAuditEvent.objects.order_by("created_at").values_list("action", flat=True))
    assert actions.count("staff.second_factor_enrollment_started") == 2
    assert actions.count("staff.second_factor_enrolled") == 1
    assert actions.count("staff.second_factor_verified") == 1
    assert actions.count("staff.second_factor_failed") == 2
    for event in PlatformAuditEvent.objects.all():
        assert secret not in json.dumps(event.details) and again not in json.dumps(event.details)


def test_wrong_codes_are_limited_per_staff_account_across_processes(db, required, clock):
    make_staff()
    client, _ = sign_in()
    secret = enroll(client, clock[0])
    other, _ = sign_in()
    for _ in range(staff_second_factor.CODE_FAILURES):
        assert post(other, "platform/second-factor/verify", {"code": "000000"}).status_code == 400
    limited = post(other, "platform/second-factor/verify", {"code": code(secret, clock[0] + 30, 0)})
    assert limited.status_code == 429 and int(limited["Retry-After"]) >= 1
    assert other.get("/api/v1/platform/organizations").status_code == 403


def test_impersonation_needs_a_current_code(a, required, clock):
    staff = make_staff()
    client, _ = sign_in()
    secret = enroll(client, clock[0])
    member = a["users"]["viewer"]
    body = {"organization_id": str(a["org"].id), "user_id": member.id, "reason": "Ticket 7", "password": PASSWORD}
    missing = post(client, "platform/impersonation", body)
    assert missing.status_code == 400 and "code" in missing.json()["errors"]
    clock[0] += 30
    started = post(client, "platform/impersonation", {**body, "code": code(secret, clock[0])})
    assert started.status_code == 201, started.content
    # While impersonating, the console's own status and stop still work (the marker is kept aside, then restored).
    assert client.get("/api/v1/platform/impersonation").status_code == 200
    assert client.delete("/api/v1/platform/impersonation").status_code == 200
    assert client.get("/api/v1/platform/organizations").status_code == 200
    assert PlatformAuditEvent.objects.filter(action="impersonation.started", actor=staff).exists()


def test_not_required_outside_cloud_mode_by_default(db, settings, clock):
    import os

    from softwarden import settings as installed

    if "SOFTWARDEN_PLATFORM_TOTP" not in os.environ:  # the default follows the deployment mode
        assert installed.SOFTWARDEN_PLATFORM_TOTP is installed.SOFTWARDEN_CLOUD
    settings.SOFTWARDEN_PLATFORM_TOTP = False
    make_staff()
    client, response = sign_in()
    assert response.json()["second_factor"] == {"required": False, "enrolled": False, "verified": False}
    assert client.get("/api/v1/platform/organizations").status_code == 200


# ---- recovery and the slot -----------------------------------------------------------------------------------------


def test_the_operator_resets_a_lost_second_factor_and_it_is_audited(db, required, clock):
    staff = make_staff()
    client, _ = sign_in()
    enroll(client, clock[0])
    with pytest.raises(CommandError, match="reason"):
        call_command("platform_staff", "reset-second-factor", staff.email)
    out = io.StringIO()
    call_command(
        "platform_staff", "reset-second-factor", staff.email, "--reason", "Lost phone; identity checked", stdout=out
    )
    assert "reset" in out.getvalue()
    row = PlatformStaff.objects.get(user=staff)
    assert not row.totp_ciphertext and row.totp_confirmed_at is None
    event = PlatformAuditEvent.objects.get(action="staff.second_factor_reset")
    assert event.actor is None and event.details["via"] == "cli" and event.details["reason"].startswith("Lost phone")
    # The session it verified needs the second factor again; the staff member enrolls afresh.
    assert client.get("/api/v1/platform/organizations").status_code == 403
    assert post(client, "platform/second-factor/enrollment").status_code == 201
    out = io.StringIO()
    call_command("platform_staff", "list", stdout=out)
    assert "second factor: not set up" in out.getvalue()


def test_the_secret_is_a_registered_credentials_slot_and_rotates(db, required, clock, monkeypatch):
    from cryptography.fernet import Fernet

    assert ("core.PlatformStaff", ("totp",)) in credentials.SLOTS
    staff = make_staff()
    client, _ = sign_in()
    secret = enroll(client, clock[0])
    old_key = PlatformStaff.objects.get(user=staff).totp_key_id
    current = credentials._configured_keys()[0]
    monkeypatch.setenv(credentials.KEYS_ENV, ",".join([Fernet.generate_key().decode(), *current]))
    result = credentials.rotate_all()
    assert result.count >= 1 and not result.unreadable
    row = PlatformStaff.objects.get(user=staff)
    assert row.totp_key_id != old_key and credentials.slot_value(row, "totp") == secret


# ---- review follow-ups: counted before checked, not tied to the clock, the CLI, an unreadable secret ---------------


def test_wrong_codes_across_a_five_minute_clock_boundary_stay_refused(db, required, clock):
    """Five wrong codes 10 seconds before a 5-minute boundary: the count is not reset by the boundary, only by five
    minutes without a code (or an accepted one)."""
    clock[0] = START + 300 - 10
    make_staff()
    client, _ = sign_in()
    secret = enroll(client, clock[0])
    other, _ = sign_in()
    for _ in range(staff_second_factor.CODE_FAILURES):
        assert post(other, "platform/second-factor/verify", {"code": "000000"}).status_code == 400
    clock[0] += 15  # past the boundary
    limited = post(other, "platform/second-factor/verify", {"code": code(secret, clock[0], 0)})
    assert limited.status_code == 429 and 280 <= int(limited["Retry-After"]) <= 300
    clock[0] += staff_second_factor.CODE_WINDOW  # five quiet minutes
    assert post(other, "platform/second-factor/verify", {"code": code(secret, clock[0])}).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_parallel_wrong_codes_are_counted_before_any_is_checked(settings, monkeypatch):
    """A race: twenty wrong codes at once, each slow to check, let five through and refuse the rest
    (counting after checking let all twenty through)."""
    import threading
    import time

    from django.db import connection
    from rest_framework.throttling import ScopedRateThrottle

    from core import counters

    settings.SOFTWARDEN_PLATFORM_TOTP = True
    monkeypatch.setattr(
        ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "login": "1000/min"}
    )
    make_staff()
    first, _ = sign_in()
    assert post(first, "platform/second-factor/enrollment").status_code == 201
    clients = [sign_in()[0] for _ in range(20)]
    real = totp.matching_step

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(totp, "matching_step", slow)
    results: list[int] = []
    start = threading.Barrier(20, timeout=30)

    def go(client):
        try:
            start.wait()
            results.append(post(client, "platform/second-factor/verify", {"code": "000000"}).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        failures = staff_second_factor.CODE_FAILURES
        assert sorted(results) == [400] * failures + [429] * (20 - failures)
    finally:
        counters.reset()


def test_the_operator_can_enroll_from_the_host(db, required, clock):
    staff = make_staff()
    out = io.StringIO()
    call_command("platform_staff", "enroll", staff.email, stdout=out)
    uri = next(line.strip() for line in out.getvalue().splitlines() if line.strip().startswith("otpauth://"))
    secret = parse_qs(urlsplit(uri).query)["secret"][0]
    event = PlatformAuditEvent.objects.get(action="staff.second_factor_enrollment_started")
    assert event.actor is None and event.details["via"] == "cli" and secret not in json.dumps(event.details)
    client, _ = sign_in()
    assert post(client, "platform/second-factor/verify", {"code": code(secret, clock[0])}).status_code == 200
    with pytest.raises(CommandError, match="already set up"):
        call_command("platform_staff", "enroll", staff.email, stdout=io.StringIO())


def test_a_secret_no_key_opens_is_a_409_not_a_500(db, required, clock):
    staff = make_staff()
    client, _ = sign_in()
    enroll(client, clock[0])
    other, _ = sign_in()
    PlatformStaff.objects.filter(user=staff).update(totp_ciphertext="gAAAAA-not-a-fernet-token")
    response = post(other, "platform/second-factor/verify", {"code": "123456"})
    assert response.status_code == 409 and "reset-second-factor" in response.json()["detail"]
