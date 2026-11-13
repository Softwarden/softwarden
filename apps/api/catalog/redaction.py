"""What never enters an audit payload or snapshot. Shared by catalog/services.audit and catalog/snapshots."""

# Never stored in audit payloads: secrets by key name at any depth. A Person's identifying fields are handled
# where Persons are audited (services.PERSON_FIELDS) and snapshotted (snapshots.SPECS leaves them out).
AUDIT_DENYLIST = (
    "password",
    "token",
    "secret",
    "session",
    "csrf",
    "cookie",
    "authorization",
    "api_key",
    "ciphertext",
    "licence_key",
    "license_key",  # US spelling; plain "license" would redact license_model
)
# Snapshots copy whole rows, so they also refuse anything that looks like key material or a credential, even if a
# future column slipped into a snapshot's field list (tests/test_audit_snapshots.py checks every snapshot the suite
# writes against the same words).
SNAPSHOT_DENYLIST = (*AUDIT_DENYLIST, "hash", "key_id", "credential", "private_key", "salt", "webhook")
REDACTED = "[redacted]"


def secret_like(key, words=AUDIT_DENYLIST) -> bool:
    return any(word in str(key).lower() for word in words)


def redact(value, words=AUDIT_DENYLIST):
    if isinstance(value, dict):
        return {k: REDACTED if secret_like(k, words) else redact(v, words) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, words) for v in value]
    return value
