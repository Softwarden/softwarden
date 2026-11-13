"""Encryption of UI-entered secrets.

Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`) through MultiFernet for rotation; nothing here is home-made
cryptography. Keys live outside the database and apart from Django's SECRET_KEY. The keyring, read fresh on every
call, is, in order:

1. Credential keys, newest (primary) first, from the first of these that is set:
   SOFTWARDEN_CREDENTIAL_KEYS_FILE (a file, one key per line), SOFTWARDEN_CREDENTIAL_KEYS (comma-separated), or the
   generated `credential_keys` file in SOFTWARDEN_SECRETS_DIR (softwarden/init_secrets.py).
   With none of them and DEBUG on (local runserver only), DEV_KEY.
2. Decrypt-only fallbacks: the keys of the legacy SOFTWARDEN_SECRETS_KEY, so connector
   secrets stored before the keyring still open; and, with DEBUG on, DEV_KEY.

New values are encrypted with the primary key; any key in the ring decrypts. A key's id is the first 12 hex digits of
its SHA-256: stored next to each ciphertext, it tells which key encrypted it without revealing the key. Rotate by
putting a new key first and running `manage.py rotate_credential_keys`; remove an old key only once no backup you may
restore needs it.

A secret field `x` is a slot of three columns: `x_ciphertext`, `x_key_id`, `x_set_at`. SLOTS
lists every slot in the installation; rotation and `setup_status` read it, and a model that adds a slot adds a line.
A value that can't be decrypted raises CredentialError, whose message never contains a secret.
"""

import hashlib
import os
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.apps import apps
from django.conf import settings
from django.utils import timezone

from softwarden.env import read_secret_file

KEYS_ENV = "SOFTWARDEN_CREDENTIAL_KEYS"
KEYS_FILE_ENV = "SOFTWARDEN_CREDENTIAL_KEYS_FILE"
LEGACY_ENV = "SOFTWARDEN_SECRETS_KEY"
GENERATED_FILE = "credential_keys"
# Local development only (DEBUG on, nothing configured), like settings.DEV_SECRET_KEY. The container image runs with
# DEBUG off and a generated key. Same value as an earlier version's core/secrets.DEV_KEY, so development data stays readable.
DEV_KEY = "c29mdHdhcmRlbi1kZXYtb25seS1zZWNyZXRzLWtleSE="

# Every secret slot: (app_label.Model, (slot prefix, ...)). The one place that lists them.
SLOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("core.Organization", ("licence_key",)),
    ("core.MicrosoftConnection", ("secret",)),
    ("core.HaloConnection", ("secret",)),
    ("core.OktaConnection", ("secret",)),  # the service app's private key
    ("core.GoogleWorkspaceConnection", ("secret",)),  # the service account key's private key and key ID
    ("notify.EmailSettings", ("graph_secret", "smtp_password")),
    ("notify.TeamsDestination", ("url",)),
    ("notify.WebhookDestination", ("url", "signing_secret", "previous_signing_secret")),
    ("discovery.AssistantSettings", ("api_key",)),
    ("core.SsoSettings", ("secret", "draft_secret")),
    ("tickets.TicketConnection", ("secret",)),  # a Jira API token or a ServiceNow password
    ("core.PlatformStaff", ("totp",)),  # A staff account's TOTP secret; installation-wide, no organization
)


class CredentialError(Exception):
    """A secret couldn't be stored or read. The message is safe to show and log: it never contains the value."""


def key_id(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()[:12]


def _split(raw: str) -> list[str]:
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def _configured_keys() -> tuple[list[str], str]:
    """(credential keys newest first, where they came from). Raises CredentialError for an unreadable key file."""
    path = (os.environ.get(KEYS_FILE_ENV) or "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                return _split(handle.read()), KEYS_FILE_ENV
        except OSError as exc:
            raise CredentialError(f"Can't read {KEYS_FILE_ENV} ({exc.strerror}).") from None
    raw = (os.environ.get(KEYS_ENV) or "").strip()
    if raw:
        return _split(raw), KEYS_ENV
    generated = read_secret_file(GENERATED_FILE)
    if generated:
        return _split(generated), "generated"
    if settings.DEBUG:
        return [DEV_KEY], "development"
    return [], ""


def _fernet(key: str, label: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise CredentialError(f"{label} isn't a valid Fernet key (32 url-safe base64 bytes).") from None


DEV_KEY_ID = key_id(DEV_KEY)
DEV_KEY_REFUSED = (
    "The development credential key is configured as a credential key, and it is public (it is in the source). "
    "With DEBUG off it only decrypts: put a new key first (manage.py generate_credential_key), keep this one after "
    "it, and run manage.py rotate_credential_keys."
)


def _dev_key_refused(keys: list[str]) -> bool:
    """True when DEBUG is off and the configured keys include DEV_KEY."""
    return not settings.DEBUG and any(key_id(k) == DEV_KEY_ID for k in keys)


def _ring() -> tuple[list[tuple[str, Fernet]], list[tuple[str, Fernet]]]:
    """(primary ring, decrypt-only fallbacks), each a list of (key id, Fernet).

    With DEBUG off, DEV_KEY is never in the primary ring even when configured: it is public, so nothing new is
    encrypted under it, but it stays a decrypt-only fallback so rotation can move an install's data off it."""
    keys, _source = _configured_keys()
    legacy = _split(os.environ.get(LEGACY_ENV) or "")
    if _dev_key_refused(keys):
        keys = [k for k in keys if key_id(k) != DEV_KEY_ID]
        legacy.append(DEV_KEY)
    primary = [(key_id(k), _fernet(k, f"Credential key #{n}")) for n, k in enumerate(keys, start=1)]
    seen = {kid for kid, _ in primary}
    fallback = []
    if settings.DEBUG:
        legacy.append(DEV_KEY)
    for n, k in enumerate(legacy, start=1):
        if key_id(k) not in seen:
            seen.add(key_id(k))
            fallback.append((key_id(k), _fernet(k, f"{LEGACY_ENV} key #{n}")))
    return primary, fallback


def configured_key_values() -> list[str]:
    """Every key in the ring, for redacting error messages (core/jobs.py). Never raises; never shown."""
    try:
        keys, _ = _configured_keys()
    except CredentialError:
        keys = []
    return keys + _split(os.environ.get(LEGACY_ENV) or "")


def configured_key_ids() -> list[str]:
    """The key id of every key that can decrypt (primary ring and decrypt-only fallbacks), for `manage.py doctor`'s
    count of secrets whose key is gone. Ids only, never a key. [] when the keyring can't be read."""
    try:
        primary, fallback = _ring()
    except CredentialError:
        return []
    return [kid for kid, _ in (*primary, *fallback)]


def status() -> dict:
    """{"available", "primary" key id, "count", "source", "error"} for setup pages and commands. Never raises."""
    try:
        primary, fallback = _ring()
        keys, source = _configured_keys()
    except CredentialError as exc:
        return {"available": False, "primary": "", "count": 0, "source": "", "error": str(exc)}
    if not primary:
        return {
            "available": False,
            "primary": "",
            "count": len(fallback),
            "source": "",
            "error": DEV_KEY_REFUSED
            if _dev_key_refused(keys)
            else f"No credential encryption key is configured ({KEYS_ENV}); secrets can't be stored.",
        }
    return {
        "available": True,
        "primary": primary[0][0],
        "count": len(primary) + len(fallback),
        "source": source,
        "error": "",
    }


def available() -> bool:
    return status()["available"]


def primary_key_id() -> str:
    return status()["primary"]


def encrypt(plaintext: str) -> tuple[str, str]:
    """(token, key id) for a non-empty secret, encrypted with the primary key."""
    if not plaintext:
        raise CredentialError("Nothing to store.")
    primary, _fallback = _ring()
    if not primary and _dev_key_refused(_configured_keys()[0]):
        raise CredentialError(f"Secrets can't be stored. {DEV_KEY_REFUSED}")
    if not primary:
        raise CredentialError(
            f"Secrets can't be stored: the credential encryption key isn't configured. Set {KEYS_ENV} (see the "
            "install guide, 'Credential encryption keys')."
        )
    kid, fernet = primary[0]
    return fernet.encrypt(plaintext.encode()).decode(), kid


def _multi() -> MultiFernet:
    primary, fallback = _ring()
    ring = primary + fallback
    if not ring:
        raise CredentialError("A stored secret can't be read: the credential encryption key isn't configured.")
    return MultiFernet([f for _kid, f in ring])


def decrypt(token: str) -> str:
    if not token:
        raise CredentialError("No secret is stored.")
    multi = _multi()
    try:
        return multi.decrypt(token.encode()).decode()
    except InvalidToken:
        raise CredentialError(
            "The stored secret can't be read with the configured keys (the key that encrypted it is missing, or the "
            "stored value was altered). Enter it again."
        ) from None


def rotate(token: str) -> tuple[str, str]:
    """Re-encrypt a stored token with the primary key: (token, primary key id)."""
    primary, _fallback = _ring()
    if not primary:
        raise CredentialError(f"No credential encryption key is configured ({KEYS_ENV}).")
    try:
        rotated = _multi().rotate(token.encode()).decode()
    except InvalidToken:
        raise CredentialError("A stored secret can't be read with any configured key.") from None
    return rotated, primary[0][0]


def generate_key() -> str:
    return Fernet.generate_key().decode()


# ---- slots ------------------------------------------------------------------------------------------------------


def slot_configured(obj, prefix: str) -> bool:
    return bool(getattr(obj, f"{prefix}_ciphertext"))


def slot_set(obj, prefix: str, plaintext: str) -> None:
    """Encrypt and store a new value on `obj` (the caller saves, inside its audited service). Raises
    CredentialError."""
    token, kid = encrypt(plaintext)
    setattr(obj, f"{prefix}_ciphertext", token)
    setattr(obj, f"{prefix}_key_id", kid)
    setattr(obj, f"{prefix}_set_at", timezone.now())


def slot_value(obj, prefix: str) -> str:
    """The decrypted value. Raises CredentialError; never falls back to another secret."""
    return decrypt(getattr(obj, f"{prefix}_ciphertext"))


def slot_models():
    """(model class, prefixes) for every registered slot."""
    return [(apps.get_model(label), prefixes) for label, prefixes in SLOTS]


def stored_count() -> int:
    """How many secrets are stored (non-empty ciphertext), for `setup_status`. Reads no value."""
    total = 0
    for model, prefixes in slot_models():
        for prefix in prefixes:
            total += model.objects.exclude(**{f"{prefix}_ciphertext": ""}).count()
    return total


def unreadable_count(organization_id=None) -> int:
    """Stored secrets whose key id is in no configured key: a restore into a stack with a new secrets volume, or a key
    dropped from the ring. Reads key ids only, never a value. 0 when the keyring can't be read (that is its own
    problem, reported by `status()`). Used by `manage.py doctor` (stored_secrets), the System page's
    `stored_secrets_unreadable` warning and ops/restore.sh. With `organization_id`, that organization's secrets only
    (the System page's organization view)."""
    known = configured_key_ids()
    if not known:
        return 0
    total = 0
    for model, prefixes in slot_models():
        rows = model.objects.all()
        if organization_id is not None:
            if model._meta.label == "core.Organization":
                rows = rows.filter(pk=organization_id)
            elif any(f.name == "organization" for f in model._meta.concrete_fields):
                rows = rows.filter(organization_id=organization_id)
            else:
                continue  # an installation-wide slot (a staff account's TOTP secret) is no organization's
        for prefix in prefixes:
            total += rows.exclude(**{f"{prefix}_ciphertext": ""}).exclude(**{f"{prefix}_key_id__in": known}).count()
    return total


def count_under_key(kid: str) -> int:
    """How many stored secrets carry key id `kid` (doctor's check for the development key). Ids only."""
    total = 0
    for model, prefixes in slot_models():
        for prefix in prefixes:
            total += model.objects.exclude(**{f"{prefix}_ciphertext": ""}).filter(**{f"{prefix}_key_id": kid}).count()
    return total


# Where an Admin enters each slot's secret again: rotation names the page for a secret no key can open.
REENTER_AT: dict[str, str] = {
    "core.Organization": "no page sets the license key; ask whoever issued it for it again",
    "core.MicrosoftConnection": "Discovery > Connections (/discovery/connections)",
    "core.HaloConnection": "Discovery > Connections (/discovery/connections)",
    "core.OktaConnection": "Discovery > Connections (/discovery/connections)",
    "core.GoogleWorkspaceConnection": "Discovery > Connections (/discovery/connections)",
    "notify.EmailSettings": "Admin > Notification channels (/admin/notification-channels)",
    "notify.TeamsDestination": "Admin > Notification channels (/admin/notification-channels)",
    "notify.WebhookDestination": "Admin > Webhooks (/admin/webhooks)",
    "discovery.AssistantSettings": "Admin > Assistant (/admin/assistant)",
    "core.SsoSettings": "Admin > Sign-in (/admin/sign-in)",
    "tickets.TicketConnection": "Admin > Ticket connections (/admin/ticket-connections)",
    "core.PlatformStaff": (
        "the staff member enrolls again after `manage.py platform_staff reset-second-factor <email> --reason ...`"
    ),
}


@dataclass(frozen=True)
class UnreadableSlot:
    """A stored secret no configured key opens. Identifiers only, never a value or a ciphertext."""

    model: str  # app_label.Model, as in SLOTS
    organization_id: str
    row_id: str
    slot: str

    @property
    def reenter_at(self) -> str:
        return REENTER_AT.get(self.model, "the page where it was entered")


@dataclass(frozen=True)
class RotationResult:
    count: int  # re-encrypted (with dry_run: would be)
    primary: str  # the primary key id
    unreadable: list[UnreadableSlot] = field(default_factory=list)


def rotate_all(dry_run: bool = False) -> RotationResult:
    """Re-encrypt every stored secret not under the primary key, in one transaction with the rows locked; audited
    once per organization that had any (counts and key ids only, never a value).

    A secret no configured key opens doesn't stop the others: it is left as it is and listed in the result by
    model, organization, row and slot, so one stale row can't hold the whole installation on an old key. With
    `dry_run` every pending secret is decrypted, not only counted, so the unreadable ones are listed too; nothing
    changes. Raises CredentialError only when there is no usable keyring, and then nothing is changed."""
    from django.db import transaction

    from catalog.services import audit
    from core.models import Organization

    state = status()
    if not state["available"]:
        raise CredentialError(state["error"])
    primary = state["primary"]
    pending = 0
    unreadable: list[UnreadableSlot] = []
    per_org: dict = {}
    unreadable_per_org: dict = {}
    with transaction.atomic():
        for label, prefixes in SLOTS:
            model = apps.get_model(label)
            for obj in model.objects.select_for_update().order_by("pk"):
                # An installation-wide slot (a platform staff account's TOTP secret) has no organization: it is
                # rotated and listed like the others, and audited in no organization's log.
                org_id = obj.pk if isinstance(obj, Organization) else getattr(obj, "organization_id", None)
                fields = []
                for prefix in prefixes:
                    if not slot_configured(obj, prefix) or getattr(obj, f"{prefix}_key_id") == primary:
                        continue
                    try:
                        token, kid = rotate(getattr(obj, f"{prefix}_ciphertext"))
                    except CredentialError:
                        unreadable.append(UnreadableSlot(label, str(org_id or ""), str(obj.pk), prefix))
                        unreadable_per_org[org_id] = unreadable_per_org.get(org_id, 0) + 1
                        continue
                    pending += 1
                    if not dry_run:
                        setattr(obj, f"{prefix}_ciphertext", token)
                        setattr(obj, f"{prefix}_key_id", kid)
                        fields += [f"{prefix}_ciphertext", f"{prefix}_key_id"]
                if fields:
                    obj.save(update_fields=fields)
                    per_org[org_id] = per_org.get(org_id, 0) + len(fields) // 2
        if not dry_run:
            for org in Organization.objects.filter(
                pk__in=[k for k in per_org.keys() | unreadable_per_org.keys() if k is not None]
            ):
                changes: dict = {"count": per_org.get(org.pk, 0), "key": primary, "via": "cli"}
                if unreadable_per_org.get(org.pk):
                    # So this organization's Admins learn from their audit log what to enter again, and where.
                    changes["unreadable"] = unreadable_per_org[org.pk]
                    changes["reenter_at"] = sorted(
                        {u.reenter_at for u in unreadable if u.organization_id == str(org.pk)}
                    )
                audit(org, None, "credentials.rotated", None, changes)
    return RotationResult(pending, primary, unreadable)
