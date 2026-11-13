"""Whether this server may use the simulators: the one place that decides.

Simulated tenants and hosts never leave the machine and return synthetic Contoso data: the Microsoft tenant ID
`simulated` (connections, discovery, Graph email), and the reserved hosts `halo.simulated.invalid`,
`okta.simulated.invalid`, an administrator in `google.simulated.invalid`, and `jira.` / `servicenow.simulated.invalid`
(ticket connections). On a production install they would fill the catalog with data that looks real, so they are
refused unless `allowed()`:

- `development()`: DEBUG is on, or this is the Playwright server (SOFTWARDEN_E2E=1 with a database whose name ends in
  `_e2e`, the test api/urls.e2e_enabled uses). The sign-in simulators (core/oidc_simulator.py, ee/saml) follow this
  rule only: they sign anyone in as any email, so an operator flag must never turn them on (an access problem, not a
  data one).
- `allowed()`: `development()`, or the operator's explicit SOFTWARDEN_ALLOW_SIMULATED=1 (the Docker demo).

When it is false, simulated values are refused on save (a field error naming the flag), at the connection test, when a
run, a ticket refresh or an email starts, and in each transport selection (a backstop). Connections saved while it was
true are kept; they just don't run.
"""

from __future__ import annotations

import importlib.util
import os

from django.conf import settings

FLAG = "SOFTWARDEN_ALLOW_SIMULATED"
# The Microsoft tenant ID that selects the synthetic tenant, everywhere (connections, discovery, Graph email, SSO).
SIMULATED = "simulated"
MESSAGE = (
    "Simulated tenants and hosts are for demos and tests, and this server doesn't allow them. To use them, the "
    f"server's operator sets {FLAG}=1."
)
NEXT = f"Use a real tenant or address, or set {FLAG}=1 on a demo or test server."


class NotAllowed(Exception):
    """A simulator was asked for while simulation isn't allowed. The message is MESSAGE: safe to show and log."""

    def __init__(self, message: str = MESSAGE):
        super().__init__(message)


def e2e() -> bool:
    """The Playwright server: SOFTWARDEN_E2E=1, the throwaway `_e2e` database, and a source checkout. The image leaves
    out the test-only views (api/e2e_views.py,.dockerignore), so no environment makes an installed image the
    Playwright server. settings.py refuses SOFTWARDEN_E2E=1 in cloud mode or with secure cookies."""
    return (
        os.environ.get("SOFTWARDEN_E2E") == "1"
        and str(settings.DATABASES["default"]["NAME"]).endswith("_e2e")
        and importlib.util.find_spec("api.e2e_views") is not None
    )


def development() -> bool:
    """DEBUG or the Playwright server: the rule for the sign-in simulators too."""
    return bool(settings.DEBUG or e2e())


def allowed() -> bool:
    """Whether simulated connections may be saved, tested and run here."""
    return development() or bool(getattr(settings, "SOFTWARDEN_ALLOW_SIMULATED", False))


def require() -> None:
    """NotAllowed unless `allowed()`."""
    if not allowed():
        raise NotAllowed()


def command_refused(command: str) -> str:
    """Why a demo command that needs the simulators stops before writing anything."""
    return (
        f"{command} uses the simulated tenant, and this server doesn't allow simulated tenants and hosts (DEBUG is off "
        f"and this isn't the Playwright server). Nothing was changed. On a demo or test server, set {FLAG}=1 for the "
        f"app and the worker (for Docker Compose: {FLAG}=1 in .env, then docker compose up -d) and run it again."
    )


def refused(name: str) -> str:
    """Why a stored simulated connection called `name` can't be used now (run, refresh, send)."""
    return f"The connection “{name}” uses a simulated tenant or host. {MESSAGE} The connection is kept."
