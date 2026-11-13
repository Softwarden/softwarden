"""Synthetic keys for the Okta and Google Workspace connections in the sweeps that create one of every
connection (tests/test_drf_resources.py, tests/test_api_lifecycle.py). One RSA key per test session; it signs
assertions only the in-process simulators read."""

from __future__ import annotations

import json
from functools import cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

GOOGLE_ADMIN = "admin@google.simulated.invalid"
OKTA_URL = "https://okta.simulated.invalid"


@cache
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def key_file(private_key: str | None = None) -> str:
    """A service account key file as Google Cloud downloads it, around `private_key` (the session's by default)."""
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "contoso-prj",
            "private_key_id": "0123456789abcdef0123456789abcdef01234567",
            "private_key": private_key or pem(),
            "client_email": "softwarden-discovery@contoso-prj.iam.gserviceaccount.com",
            "client_id": "112233445566778899001",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
