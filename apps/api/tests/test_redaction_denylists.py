"""The redaction denylists (catalog/redaction.py) checked against a word list written out in the tests
(tests/conftest.py AUDIT_SECRET_WORDS and SNAPSHOT_SECRET_WORDS), never against the code's own lists. Each word is
pushed through the three places that redact: a snapshot's values (snapshots.plain), an audit payload
(services.audit) and the audit log's JSON lines export. A word removed from the code, or a redaction step removed,
fails here."""

import json

import pytest
from django.test import Client

from catalog import services, snapshots
from core.capabilities import set_edition
from core.models import AuditEvent
from tests.conftest import AUDIT_SECRET_WORDS, REDACTED_VALUE, SNAPSHOT_SECRET_WORDS, snapshot_problems
from tests.test_audit_export import body

SECRET = "s3cr3t-value"


def secret_payload(word: str) -> dict:
    """`word` in a key at the top, in another case inside a nested object, and inside a list of objects, beside keys
    that name no secret."""
    return {
        f"client_{word}": SECRET,
        "nested": {word.upper(): SECRET, "label": "kept"},
        "rows": [{f"{word}_value": SECRET, "position": 1}],
        "name": "kept",
    }


def redacted_payload(word: str) -> dict:
    return {
        f"client_{word}": REDACTED_VALUE,
        "nested": {word.upper(): REDACTED_VALUE, "label": "kept"},
        "rows": [{f"{word}_value": REDACTED_VALUE, "position": 1}],
        "name": "kept",
    }


def test_the_literal_lists_are_the_expected_words():
    """The expected words, so the lists in conftest can't be emptied without this failing."""
    assert len(AUDIT_SECRET_WORDS) == 11 and {"ciphertext", "api_key", "password", "token"} <= set(AUDIT_SECRET_WORDS)
    assert set(SNAPSHOT_SECRET_WORDS) - set(AUDIT_SECRET_WORDS) == {
        "hash",
        "key_id",
        "credential",
        "private_key",
        "salt",
        "webhook",
    }


@pytest.mark.parametrize("word", SNAPSHOT_SECRET_WORDS)
def test_a_snapshot_value_redacts_every_snapshot_word(word):
    assert snapshots.plain(secret_payload(word)) == redacted_payload(word)


@pytest.mark.parametrize("word", AUDIT_SECRET_WORDS)
def test_an_audit_payload_redacts_every_audit_word(a, word):
    services.audit(a["org"], a["users"]["admin"], "organization.updated", None, secret_payload(word))
    event = AuditEvent.objects.filter(organization=a["org"], action="organization.updated").latest("created_at")
    assert {k: v for k, v in event.changes.items() if k != "via"} == redacted_payload(word)
    assert SECRET not in json.dumps(event.changes)


def test_the_jsonl_export_redacts_every_snapshot_word_again(a):
    """A stored event no service could write (bulk_create skips the conftest sweep): the export masks every word in
    its payload and its snapshots on the way out."""
    a["org"], _ = set_edition(a["org"], "enterprise")
    payload = {word: secret_payload(word) for word in SNAPSHOT_SECRET_WORDS}
    AuditEvent.objects.bulk_create(
        [
            AuditEvent(
                organization=a["org"],
                action="connector.updated",
                changes=payload,
                prechange=payload,
                postchange=payload,
            )
        ]
    )
    client = Client()
    client.force_login(a["users"]["admin"])
    response = client.get("/api/v1/audit-events/export.jsonl")
    assert response.status_code == 200
    data = body(response).decode()
    assert SECRET not in data
    line = next(row for row in map(json.loads, data.splitlines()) if row["action"] == "connector.updated")
    for part in ("changes", "prechange", "postchange"):
        # each top-level key is itself a word, so the whole nested payload under it is masked
        assert line[part] == dict.fromkeys(SNAPSHOT_SECRET_WORDS, REDACTED_VALUE), part


def test_the_sweep_notices_an_unredacted_payload_or_snapshot_key():
    for word in AUDIT_SECRET_WORDS:
        event = AuditEvent(action="x.y", changes={"nested": {f"my_{word}": SECRET}})
        assert snapshot_problems(event) == [f"x.y: changes.nested.my_{word}"], word
    for word in SNAPSHOT_SECRET_WORDS:
        event = AuditEvent(action="x.y", changes={}, postchange={f"my_{word}": SECRET})
        assert snapshot_problems(event) == [f"x.y: postchange.my_{word}"], word
    assert snapshot_problems(AuditEvent(action="x.y", changes={"key_id": SECRET})) == []  # a payload's key ID is fine
