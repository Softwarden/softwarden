"""The credential keyring (core/credentials.py): encryption with a key
id, the key sources and their order, the SOFTWARDEN_SECRETS_KEY fallback, rotation, and secrets never leaving."""

import hashlib
import io
import json
import logging

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError

from core import connections, credentials, jobs
from core.models import AuditEvent, MicrosoftConnection
from discovery import engine as discovery
from discovery.models import Connector
from tests.conftest import TEST_CREDENTIAL_KEY

pytestmark = pytest.mark.django_db

SECRET = "synthetic-client-secret-value"


def connector(a, secret=SECRET):
    """A Microsoft connection holding `secret` (connectors keep no secret of their own)."""
    return connections.create_connection(
        a["org"],
        a["users"]["admin"],
        connections.MICROSOFT,
        {"name": "Entra", "tenant_id": "simulated", "secret": secret},
    )


# ---- encrypt, decrypt, key id -------------------------------------------------------------------------------------


def test_round_trip_records_the_primary_key_id():
    token, kid = credentials.encrypt(SECRET)
    assert SECRET not in token
    assert (
        kid == credentials.key_id(TEST_CREDENTIAL_KEY) == hashlib.sha256(TEST_CREDENTIAL_KEY.encode()).hexdigest()[:12]
    )
    assert credentials.decrypt(token) == SECRET
    assert credentials.status() == {
        "available": True,
        "primary": kid,
        "count": 1,
        "source": "SOFTWARDEN_CREDENTIAL_KEYS",
        "error": "",
    }


def test_tampered_ciphertext_is_refused_without_echoing_anything():
    token, _ = credentials.encrypt(SECRET)
    altered = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    with pytest.raises(credentials.CredentialError) as e:
        credentials.decrypt(altered)
    assert SECRET not in str(e.value) and altered not in str(e.value)


def test_a_different_key_cannot_read_it(monkeypatch):
    token, _ = credentials.encrypt(SECRET)
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.generate_key())
    with pytest.raises(credentials.CredentialError, match="can't be read"):
        credentials.decrypt(token)


def test_without_a_key_nothing_is_stored(monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    assert not credentials.available()
    with pytest.raises(credentials.CredentialError, match="isn't configured"):
        credentials.encrypt(SECRET)
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt("anything")


def test_empty_values_are_refused():
    with pytest.raises(credentials.CredentialError):
        credentials.encrypt("")
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt("")


def test_an_invalid_key_is_named_by_position_not_value(monkeypatch):
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{TEST_CREDENTIAL_KEY},not-a-fernet-key")
    with pytest.raises(credentials.CredentialError) as e:
        credentials.encrypt(SECRET)
    assert "#2" in str(e.value) and "not-a-fernet-key" not in str(e.value)
    assert credentials.status()["available"] is False


def test_development_key_only_with_debug(monkeypatch, settings):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    settings.DEBUG = True
    token, kid = credentials.encrypt(SECRET)
    assert kid == credentials.key_id(credentials.DEV_KEY) and credentials.status()["source"] == "development"
    settings.DEBUG = False
    with pytest.raises(credentials.CredentialError):
        credentials.decrypt(token)


def test_the_development_key_is_refused_as_a_production_primary_key(monkeypatch, settings):
    """With DEBUG off, keys = DEV_KEY reports unavailable with that message and nothing is
    encrypted under it; it still decrypts, so rotation can move data off it."""
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    settings.DEBUG = True
    token, kid = credentials.encrypt(SECRET)  # data written by a development stack
    assert kid == credentials.key_id(credentials.DEV_KEY)
    settings.DEBUG = False
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.DEV_KEY)
    state = credentials.status()
    assert state["available"] is False and "development credential key" in state["error"]
    with pytest.raises(credentials.CredentialError, match="development credential key"):
        credentials.encrypt(SECRET)
    assert credentials.decrypt(token) == SECRET  # decrypt-only
    from core import doctor

    # Review follow-up: doctor's hint is the way off the development key, not "set a key".
    [check] = doctor.check_credential_keys(with_db=False)
    assert check.status == doctor.FAIL and check.fix == doctor.DEV_KEY_ROTATION


def test_rotation_moves_every_secret_off_the_development_key(a, monkeypatch, settings):
    """Keys = new,DEV_KEY lets rotation finish with no row on the development key, and doctor fails while one
    is still there."""
    from core import doctor
    from core.models import Organization

    settings.DEBUG = True
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.DEV_KEY)
    conn = connector(a)
    org = a["org"]
    credentials.slot_set(org, "licence_key", "synthetic-licence")
    org.save()
    dev = credentials.key_id(credentials.DEV_KEY)
    assert conn.secret_key_id == org.licence_key_key_id == dev
    settings.DEBUG = False
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{credentials.DEV_KEY}")
    assert credentials.status()["available"] and credentials.primary_key_id() == credentials.key_id(new)
    failing = [c for c in doctor.check_credential_keys() if c.status == doctor.FAIL]
    assert [c.name for c in failing] == ["stored_secrets_dev_key"] and "2 stored secret(s)" in failing[0].message
    call_command("rotate_credential_keys", stdout=io.StringIO())
    assert not MicrosoftConnection.objects.filter(secret_key_id=dev).exists()
    assert not Organization.objects.filter(licence_key_key_id=dev).exists()
    assert not [c for c in doctor.check_credential_keys() if c.status == doctor.FAIL]
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", new)
    conn.refresh_from_db()
    assert credentials.slot_value(conn, "secret") == SECRET


# ---- where keys come from ---------------------------------------------------------------------------------------


def test_key_file_wins_over_the_environment(monkeypatch, tmp_path):
    from_file = credentials.generate_key()
    older = credentials.generate_key()
    path = tmp_path / "keys"
    path.write_text(f"{from_file}\n\n{older}\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS_FILE", str(path))
    _token, kid = credentials.encrypt(SECRET)
    assert kid == credentials.key_id(from_file)
    state = credentials.status()
    assert state["source"] == "SOFTWARDEN_CREDENTIAL_KEYS_FILE" and state["count"] == 2


def test_an_unreadable_key_file_is_an_error_not_a_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS_FILE", str(tmp_path / "missing"))
    with pytest.raises(credentials.CredentialError, match="Can't read"):
        credentials.encrypt(SECRET)


def test_generated_file_is_used_when_nothing_is_set(monkeypatch, tmp_path):
    generated = credentials.generate_key()
    (tmp_path / "credential_keys").write_text(generated + "\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    _token, kid = credentials.encrypt(SECRET)
    assert kid == credentials.key_id(generated) and credentials.status()["source"] == "generated"
    # The environment still wins over the generated file.
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", TEST_CREDENTIAL_KEY)
    assert credentials.encrypt(SECRET)[1] == credentials.key_id(TEST_CREDENTIAL_KEY)


def test_legacy_secrets_key_still_decrypts_but_never_encrypts(monkeypatch):
    legacy = credentials.generate_key()
    old_ciphertext = Fernet(legacy.encode()).encrypt(SECRET.encode()).decode()  # as a legacy core/secrets.py stored it
    monkeypatch.setenv("SOFTWARDEN_SECRETS_KEY", legacy)
    assert credentials.decrypt(old_ciphertext) == SECRET
    assert credentials.encrypt(SECRET)[1] == credentials.key_id(TEST_CREDENTIAL_KEY)
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    assert credentials.decrypt(old_ciphertext) == SECRET  # still readable ...
    with pytest.raises(credentials.CredentialError):  # ... but no longer a key to write with
        credentials.encrypt(SECRET)


# ---- rotation ---------------------------------------------------------------------------------------------------


def test_rotation_re_encrypts_every_slot_once_and_audits_counts(a, monkeypatch):
    conn = connector(a)
    org = a["org"]
    credentials.slot_set(org, "licence_key", "synthetic-licence")
    org.save()
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{TEST_CREDENTIAL_KEY}")
    before = MicrosoftConnection.objects.get(pk=conn.pk).secret_ciphertext

    out = io.StringIO()
    call_command("rotate_credential_keys", "--dry-run", stdout=out)
    assert "Would re-encrypt 2 secret(s)" in out.getvalue() and credentials.key_id(new) in out.getvalue()
    assert MicrosoftConnection.objects.get(pk=conn.pk).secret_ciphertext == before
    assert not AuditEvent.objects.filter(action="credentials.rotated").exists()

    out = io.StringIO()
    call_command("rotate_credential_keys", stdout=out)
    assert "Re-encrypted 2 secret(s)" in out.getvalue()
    for text in (SECRET, "synthetic-licence", before, new, TEST_CREDENTIAL_KEY):
        assert text not in out.getvalue()
    conn.refresh_from_db()
    org.refresh_from_db()
    assert conn.secret_key_id == org.licence_key_key_id == credentials.key_id(new)
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", new)  # the old key can go now
    assert credentials.slot_value(conn, "secret") == SECRET
    assert credentials.slot_value(org, "licence_key") == "synthetic-licence"
    event = AuditEvent.objects.get(action="credentials.rotated")
    assert event.organization == org and event.actor is None
    assert event.changes == {"count": 2, "key": credentials.key_id(new), "via": "cli"}

    out = io.StringIO()
    call_command("rotate_credential_keys", stdout=out)
    assert "Re-encrypted 0 secret(s)" in out.getvalue()


def test_rotation_adopts_pre_keyring_connection_secrets(a, monkeypatch):
    """A secret stored by an earlier version (SOFTWARDEN_SECRETS_KEY, no key id) gets the primary key and its id."""
    legacy = credentials.generate_key()
    conn = connector(a)
    MicrosoftConnection.objects.filter(pk=conn.pk).update(
        secret_ciphertext=Fernet(legacy.encode()).encrypt(SECRET.encode()).decode(), secret_key_id=""
    )
    monkeypatch.setenv("SOFTWARDEN_SECRETS_KEY", legacy)
    call_command("rotate_credential_keys", stdout=io.StringIO())
    monkeypatch.delenv("SOFTWARDEN_SECRETS_KEY")
    conn.refresh_from_db()
    assert conn.secret_key_id == credentials.key_id(TEST_CREDENTIAL_KEY)
    assert credentials.slot_value(conn, "secret") == SECRET


def _orphan(conn, plaintext=b"orphaned-synthetic-secret"):
    """Give `conn` a ciphertext no configured key opens (its key was dropped from the ring)."""
    token = Fernet(credentials.generate_key().encode()).encrypt(plaintext).decode()
    MicrosoftConnection.objects.filter(pk=conn.pk).update(secret_ciphertext=token)
    return token


def test_one_unreadable_secret_does_not_block_rotating_the_rest(a, b, monkeypatch):
    """The other secrets end under the new key, the orphan is named (never its value), and --dry-run finds it."""
    good, bad = connector(a), connector(b)
    org = a["org"]
    credentials.slot_set(org, "licence_key", "synthetic-licence")
    org.save()
    orphan = _orphan(bad)
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{TEST_CREDENTIAL_KEY}")

    out = io.StringIO()
    call_command("rotate_credential_keys", "--dry-run", stdout=out)
    text = out.getvalue()
    assert "Would re-encrypt 2 secret(s)" in text and "1 can't be read" in text
    assert f"core.MicrosoftConnection {bad.pk} (organization {b['org'].pk}) slot secret" in text
    assert "/discovery/connections" in text
    assert MicrosoftConnection.objects.get(pk=bad.pk).secret_ciphertext == orphan
    assert not AuditEvent.objects.filter(action="credentials.rotated").exists()

    out = io.StringIO()
    call_command("rotate_credential_keys", stdout=out)  # exits 0 without --strict
    text = out.getvalue()
    assert "Re-encrypted 2 secret(s)" in text and "1 can't be read" in text and str(bad.pk) in text
    for secret in (SECRET, "synthetic-licence", "orphaned-synthetic-secret", orphan, new, TEST_CREDENTIAL_KEY):
        assert secret not in text
    good.refresh_from_db()
    org.refresh_from_db()
    bad.refresh_from_db()
    assert good.secret_key_id == org.licence_key_key_id == credentials.key_id(new)
    assert bad.secret_ciphertext == orphan  # left as it was, for an Admin to enter again
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", new)
    assert credentials.slot_value(good, "secret") == SECRET
    assert AuditEvent.objects.get(action="credentials.rotated", organization=org).changes == {
        "count": 2,
        "key": credentials.key_id(new),
        "via": "cli",
    }
    assert AuditEvent.objects.get(action="credentials.rotated", organization=b["org"]).changes == {
        "count": 0,
        "key": credentials.key_id(new),
        "unreadable": 1,
        "reenter_at": ["Discovery > Connections (/discovery/connections)"],
        "via": "cli",
    }


def test_the_rotation_event_tells_each_organizations_admins_what_to_enter_again(a, b, api, monkeypatch):
    """An organization with a secret rotation skipped learns it from its own audit log."""
    connector(a)
    _orphan(connector(b))
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{credentials.generate_key()},{TEST_CREDENTIAL_KEY}")
    call_command("rotate_credential_keys", stdout=io.StringIO())
    ok = api(a["users"]["admin"]).get("audit-events?action=credentials.rotated").json_body["results"]
    assert ok[0]["summary"] == "1 secret re-encrypted"
    skipped = api(b["users"]["admin"]).get("audit-events?action=credentials.rotated").json_body["results"]
    assert skipped[0]["summary"] == (
        "0 secrets re-encrypted; 1 can't be read with any configured key: enter it again at "
        "Discovery > Connections (/discovery/connections)"
    )


def test_strict_rotation_still_rotates_the_rest_but_exits_non_zero(a, b, monkeypatch):
    good, bad = connector(a), connector(b)
    _orphan(bad)
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{TEST_CREDENTIAL_KEY}")
    out = io.StringIO()
    with pytest.raises(CommandError, match="1 stored secret"):
        call_command("rotate_credential_keys", "--strict", stdout=out)
    assert str(bad.pk) in out.getvalue()
    good.refresh_from_db()
    assert good.secret_key_id == credentials.key_id(new)


def test_rotation_needs_a_key(monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    with pytest.raises(CommandError, match="No credential encryption key"):
        call_command("rotate_credential_keys", stdout=io.StringIO())


def test_rotate_secrets_is_a_deprecated_alias(a, monkeypatch):
    conn = connector(a)
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{TEST_CREDENTIAL_KEY}")
    err = io.StringIO()
    call_command("rotate_secrets", stdout=io.StringIO(), stderr=err)
    assert "deprecated" in err.getvalue()
    conn.refresh_from_db()
    assert conn.secret_key_id == credentials.key_id(new)


def test_generate_credential_key_prints_a_key_and_its_id():
    out, err = io.StringIO(), io.StringIO()
    call_command("generate_credential_key", stdout=out, stderr=err)
    key = out.getvalue().strip()
    Fernet(key.encode())
    assert credentials.key_id(key) in err.getvalue() and key not in err.getvalue()


def test_every_registered_slot_has_its_three_columns():
    for model, prefixes in credentials.slot_models():
        names = {f.name for f in model._meta.concrete_fields}
        for prefix in prefixes:
            assert {f"{prefix}_ciphertext", f"{prefix}_key_id", f"{prefix}_set_at"} <= names, (model, prefix)


def test_every_ciphertext_column_is_a_registered_slot():
    """The other way round: a `*_ciphertext` column of any local model that isn't in SLOTS would be skipped by
    key rotation, setup_status and the restore checks without a word."""
    from core.local_apps import local_models

    registered = {(label, prefix) for label, prefixes in credentials.SLOTS for prefix in prefixes}
    found = {
        (model._meta.label, field.name.removesuffix("_ciphertext"))
        for model in local_models()
        for field in model._meta.concrete_fields
        if field.name.endswith("_ciphertext")
    }
    assert found - registered == set(), "add the slot to core/credentials.py SLOTS"
    assert len(found) >= 15


# ---- never out ----------------------------------------------------------------------------------------------------


def test_secret_and_ciphertext_never_in_api_audit_or_logs(a, api, caplog):
    caplog.set_level(logging.DEBUG)
    admin = api(a["users"]["admin"])
    r = admin.post("connections/microsoft", {"name": "Entra", "tenant_id": "simulated", "secret": SECRET})
    assert r.status_code == 201, r.json_body
    stored = MicrosoftConnection.objects.get(pk=r.json_body["id"])
    assert stored.secret_key_id == credentials.key_id(TEST_CREDENTIAL_KEY)
    tested = admin.post(f"connections/microsoft/{stored.id}/test")
    c = admin.post("connectors", {"name": "Entra", "microsoft_connection": str(stored.id)})
    assert c.status_code == 201, c.json_body
    discovery.run(Connector.objects.get(pk=c.json_body["id"]))
    seen = [
        json.dumps(r.json_body),
        json.dumps(tested.json_body),
        json.dumps(admin.get("connectors").json_body),
        json.dumps(admin.get(f"connectors/{c.json_body['id']}").json_body),
        json.dumps(admin.get("connections/microsoft").json_body),
        json.dumps(list(AuditEvent.objects.values_list("changes", flat=True))),
        caplog.text,
    ]
    for text in seen:
        for forbidden in (SECRET, stored.secret_ciphertext, TEST_CREDENTIAL_KEY):
            assert forbidden not in text


def test_job_errors_redact_keys_from_the_generated_file(monkeypatch, tmp_path):
    generated = credentials.generate_key()
    (tmp_path / "credential_keys").write_text(generated + "\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    text = jobs.safe_error(RuntimeError(f"failed with {generated}"))
    assert generated not in text and "[redacted]" in text
