"""The demo accounts `seed_demo` makes (catalog/management/commands/seed_demo.py) and the published password they start
with. On a server outside development a demo account that still has that password is anyone's to sign in
as, so `doctor` and the System page say so. Nothing here changes an account."""

from __future__ import annotations

import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password

DEMO_PASSWORD = "softwarden-demo-2026"  # noqa: S105  (published: local synthetic accounts only)
# The accounts seed_demo makes (its USERS and PLATFORM_STAFF; tests/test_seed_demo.py keeps them in step). Only these
# are checked: each check is a full password hash.
DEMO_USERNAMES = ("admin@example.test", "editor@example.test", "viewer@example.test", "platform@example.test")

# (user id, stored hash) -> whether it is the demo password. A changed password is a new key, so an answer is never
# stale; the hash work is done once per account and password per process instead of on every System page load.
_SEEN: dict[tuple[int, str], bool] = {}
_LOCK = threading.Lock()


def _is_demo(pk: int, encoded: str) -> bool:
    key = (pk, encoded)
    with _LOCK:
        if key in _SEEN:
            return _SEEN[key]
    found = check_password(DEMO_PASSWORD, encoded)  # no setter: never rehashes or saves
    with _LOCK:
        if len(_SEEN) > 1000:
            _SEEN.clear()
        _SEEN[key] = found
    return found


def accounts_with_demo_password() -> list[str]:
    """The active demo accounts (DEMO_USERNAMES) whose password is still DEMO_PASSWORD, outside DEBUG only (in
    development they are expected). Their usernames, sorted; never the password or its hash."""
    if settings.DEBUG:
        return []
    rows = (
        get_user_model()
        .objects.filter(is_active=True, username__in=DEMO_USERNAMES)
        .order_by("username")
        .values_list("pk", "username", "password")
    )
    return [username for pk, username, encoded in rows if encoded and _is_demo(pk, encoded)]
