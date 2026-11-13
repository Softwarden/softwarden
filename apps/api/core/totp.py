"""Time-based one-time passwords (RFC 6238, TOTP over RFC 4226 HOTP) for platform staff.

Standard library only (hmac, hashlib, base64): the algorithm is twenty lines and every authenticator app speaks it, so
no dependency is added. HMAC-SHA1, 6 digits, a 30-second step: the parameters every authenticator app defaults to
and the only ones some support. A code is accepted for the current step and one step either side (SKEW), so a phone
clock up to about 30 seconds off still works; the caller refuses a step it has already accepted (replay).

The secret is 20 random bytes (160 bits, RFC 4226's recommendation), shown once as base32 and as an otpauth:// URI
for a QR code, and stored only Fernet-encrypted (core/credentials.py slot `totp` on PlatformStaff).

`clock` is the one source of time here, so tests can move it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30  # seconds
DIGITS = 6
SKEW = 1  # steps accepted either side of now
SECRET_BYTES = 20
ISSUER = "Softwarden"


def clock() -> float:
    return time.time()


def new_secret() -> str:
    """A new secret, base32 without padding (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    """RFC 4226 HOTP of the counter `step`: dynamic truncation of HMAC-SHA1, DIGITS decimal digits."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((clock() if now is None else now) // STEP)


def matching_step(secret: str, code: object, now: float | None = None) -> int | None:
    """The step (current, or one either side) whose code `code` is, compared in constant time; None when it is none
    of them or isn't six digits."""
    if not isinstance(code, str):
        return None
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    step = current_step(now)
    found = None
    for candidate in range(step - SKEW, step + SKEW + 1):
        if hmac.compare_digest(code_at(secret, candidate), code):
            found = candidate
    return found


def provisioning_uri(secret: str, account: str) -> str:
    """The otpauth:// URI an authenticator app reads from a QR code (Key Uri Format)."""
    label = quote(f"{ISSUER}:{account}", safe="@:")
    query = urlencode({"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"
