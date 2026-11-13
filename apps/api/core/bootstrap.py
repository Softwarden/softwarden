"""First-run setup.

A fresh installation has no account and ships no default credential. The app container's start command runs
`bootstrap_admin --prepare`, which writes a random one-time token to a private file in the `bootstrap` volume
(settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE, 0600, valid one hour from its mtime). An operator with host access reads
it with `bootstrap_admin` and enters it at /setup, which creates the first organization and Admin (`POST
/api/v1/setup`, core/setup_views.py). The token never reaches a web client that doesn't already have it.

Setup is open only while BootstrapState says so and no organization or user exists; the first organization or user
created by any path closes it for good (signals in core/apps.py). Bad tokens are limited per client address.

In cloud mode (SOFTWARDEN_DEPLOYMENT=cloud) there is no token and no web setup: `bootstrap_admin --create` makes the
operator's platform staff account, in no organization (web_setup_open).

The credential encryption key is shown once: the first `bootstrap_admin` reveal prints it with backup instructions
and leaves a marker file next to the token; later runs print only its key id and how to read it again.
"""

import os
import secrets
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from . import counters
from .models import BootstrapState, Organization

TOKEN_TTL = 3600  # seconds
TOKEN_LENGTH = 43  # secrets.token_urlsafe(32)
FAILURE_LIMIT = 5  # bad tokens per client address ...
FAILURE_WINDOW = 300  # ... per five minutes
KEY_SHOWN_MARKER = "credential_key_shown"


def pending() -> bool:
    """Setup is open: the state row exists without completed_at, and no organization or user exists. Fails closed."""
    return (
        BootstrapState.objects.filter(pk=1, completed_at__isnull=True).exists()
        and not Organization.objects.exists()
        and not get_user_model().objects.exists()
    )


def web_setup_open() -> bool:
    """The /setup page and its token are offered: setup is pending and this isn't a hosted (cloud) installation. In cloud
    mode first-run setup creates the operator's platform staff account from the host only (`bootstrap_admin --create`,
    catalog/admin_services.bootstrap_platform_staff): a public page that creates an organization and its Admin would
    be the wrong first account for a multi-organization service."""
    return not settings.SOFTWARDEN_CLOUD and pending()


def close() -> None:
    """Mark setup completed (idempotent). Called by the setup service and by the creation signals."""
    BootstrapState.objects.filter(pk=1, completed_at__isnull=True).update(completed_at=timezone.now())


# ---- the token file --------------------------------------------------------------------------------------------


def token_path() -> Path:
    return Path(settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE)


def read_token() -> str:
    """The current token, or "" when there is none, it is malformed, or it is older than TOKEN_TTL."""
    path = token_path()
    try:
        age = time.time() - path.stat().st_mtime
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""
    return token if 0 <= age < TOKEN_TTL and len(token) == TOKEN_LENGTH else ""


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)  # 0600; atomic replace avoids partial reads
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_token() -> str:
    """The valid token while setup is open (reusing an unexpired one, else writing a new one); "" once setup is
    closed, when any leftover token file is removed, and always "" in cloud mode, where there is no web setup
    (web_setup_open). Raises OSError when the file can't be written."""
    if settings.SOFTWARDEN_CLOUD and pending():
        discard_token()
        return ""
    if not pending():
        if BootstrapState.objects.filter(pk=1, completed_at__isnull=True).exists():
            close()  # e.g. accounts loaded by a restore or bulk load that sent no signal
        discard_token()
        return ""
    token = read_token()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    _write_private(token_path(), token)
    return token


def discard_token() -> None:
    token_path().unlink(missing_ok=True)


def matches(value) -> bool:
    token = read_token()
    return bool(token) and isinstance(value, str) and value.isascii() and secrets.compare_digest(token, value.strip())


# ---- bad-token limit (the shared counters) ---------------------------------------------------------------

FAILURE_SCOPE = "setup-token"


def blocked_for(ip: str) -> int:
    """Seconds this address must still wait, or 0. Counted in the shared counters (core/counters.py): one limit
    for the installation, whichever worker serves the request, and an atomic count that a burst can't lose.
    The window is a fixed FAILURE_WINDOW (aligned to the clock), so the block lasts at most that long."""
    seen = counters.peek(FAILURE_SCOPE, ip, FAILURE_WINDOW)
    return seen.retry_after if seen.count >= FAILURE_LIMIT else 0


def record_failure(ip: str) -> None:
    """Count a bad token in the address's current window."""
    counters.hit(FAILURE_SCOPE, ip, FAILURE_WINDOW)


# ---- showing the credential key once ---------------------------------------------------------------------------


def key_shown_marker() -> Path:
    return token_path().parent / KEY_SHOWN_MARKER


def key_already_shown() -> bool:
    return key_shown_marker().exists()


def mark_key_shown() -> None:
    _write_private(key_shown_marker(), timezone.now().isoformat())
