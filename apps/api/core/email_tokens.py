"""Single-use tokens sent by email (signup confirmation, invitations, password reset).

One handling for every emailed link, so each flow gets the same guarantees:

- The token is `secrets.token_urlsafe(TOKEN_BYTES)`: 43 URL-safe characters, 256 bits.
- Only its SHA-256 (`hash_token`) is stored. A stolen database or backup holds no usable link.
- The link carries it in the fragment, `{SOFTWARDEN_BASE_URL}<path>#<token>` (`fragment_link`): browsers never send
  a fragment, so it is never in a server's access log. The page reads it and sends it in a request body.
- `plausible` refuses anything that can't be a token before it reaches a query (wrong type, absurd length).

Single use and expiry are the caller's: each flow marks its own row used and checks its own deadline in the same
transaction that acts on it."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from django.conf import settings

TOKEN_BYTES = 32  # token_urlsafe(32): 43 characters
MIN_LENGTH = 20
MAX_LENGTH = 100


def new_token() -> tuple[str, str]:
    """(token, its SHA-256 hex digest). Store the digest; send the token once."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def plausible(token: Any) -> bool:
    return isinstance(token, str) and MIN_LENGTH <= len(token) <= MAX_LENGTH


def fragment_link(path: str, token: str) -> str:
    return f"{settings.SOFTWARDEN_BASE_URL}{path}#{token}"
