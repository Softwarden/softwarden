"""The settings pytest and mypy load (pyproject.toml): softwarden.settings in development mode.

SOFTWARDEN_DEBUG defaults to off, and with it off the settings refuse to start without a real
SOFTWARDEN_SECRET_KEY. The suite runs on the development key, so this turns development mode on unless the environment
already says otherwise. pytest-django imports the settings before any conftest runs, so a conftest is too late. It is
set in os.environ, not only here, so the subprocesses the tests start (manage.py, django.setup()) inherit it; tests
that need production mode set SOFTWARDEN_DEBUG=0 in the subprocess's environment (tests/test_init_secrets.py).
This module is in tests/, which the image doesn't carry (.dockerignore), so no installation can point at it."""

import os

os.environ.setdefault("SOFTWARDEN_DEBUG", "1")

from softwarden.settings import *  # noqa: E402, F403

# An installation requires If-Match from a signed-in session by default. The suite's
# client is a signed-in session (tests/conftest.py Api), and the suite tests each route's own behavior, conditional and
# not, so it runs with the rule off; tests/test_if_match_required.py turns it on (and checks the installation default is
# on), including a sweep of every conditional route.
SOFTWARDEN_REQUIRE_IF_MATCH = False

# No test ever resolves real DNS (core/dns_txt.py answers from its SIMULATED table), even
# a test that turns DEBUG off.
SOFTWARDEN_DNS_RESOLVER = "simulated"
