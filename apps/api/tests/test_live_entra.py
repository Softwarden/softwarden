"""Live acceptance against a real tenant (acceptance check: "Against a real Entra tenant, a run lists enterprise
applications and registrations with credential expiry, Microsoft first-party apps are skipped by default, a second run
reports unchanged"). Every test here is marked `live`: skipped unless pytest runs with `--live` and the credentials are
in the environment (tests/conftest.py), so the default gate never reaches the network. Run it against a
test tenant you are allowed to read, never production (docs/DEVELOPMENT.md, "Live tests"):

    SOFTWARDEN_LIVE_TENANT_ID=... SOFTWARDEN_LIVE_CLIENT_ID=... SOFTWARDEN_LIVE_CLIENT_SECRET=... \
        uv run pytest --live -m live tests/test_live_entra.py

Optional: SOFTWARDEN_LIVE_SUBSCRIPTION_ID (an Azure subscription the app has Reader on) and
SOFTWARDEN_LIVE_DATAVERSE_URL (an environment where the app has an application user) run the Azure and Power Platform
reads too. The tests only read. The secret is stored the normal way (Fernet, the test credential key) and the tests
check it never appears in the run, the observations, the audit events or the log.

Renaming an app ("changed" candidate) and deleting one ("no longer seen") need a write in the tenant, which these
tests never make; docs/DEVELOPMENT.md lists those two as manual steps on the dev tenant."""

import json
import logging
import os

import pytest

from core import connections
from core.capabilities import set_edition
from core.models import AuditEvent
from discovery import access, entra
from discovery import engine as discovery
from discovery.models import Connector, ConnectorRun, DiscoveredCredential, Observation, RunStatus

pytestmark = pytest.mark.live


def env(name):
    return os.environ.get(name, "").strip()


@pytest.fixture
def live(a, caplog):
    caplog.set_level(logging.DEBUG)
    org, admin = a["org"], a["users"]["admin"]
    conn = connections.create_connection(
        org,
        admin,
        connections.MICROSOFT,
        {
            "name": "Live dev tenant",
            "tenant_id": env("SOFTWARDEN_LIVE_TENANT_ID"),
            "client_id": env("SOFTWARDEN_LIVE_CLIENT_ID"),
            "secret": env("SOFTWARDEN_LIVE_CLIENT_SECRET"),
        },
    )
    tested = connections.run_test(org, admin, conn)
    assert tested.test_ok, tested.test_result
    yield {"org": org, "admin": admin, "connection": tested, "a": a}
    secret = env("SOFTWARDEN_LIVE_CLIENT_SECRET")
    stored = json.dumps(
        [
            list(ConnectorRun.objects.values("error", "counts")),
            list(Observation.objects.values("display_name", "data")),
            [[e.changes, e.prechange, e.postchange] for e in AuditEvent.objects.filter(organization=org)],
            list(Connector.objects.values("access_check")),
        ],
        default=str,
    )
    assert secret not in stored and secret not in caplog.text


def connector(live, source="entra", **scope):
    data = {"source": source, "name": f"Live {source}", "microsoft_connection": str(live["connection"].id)}
    if scope:
        data["scope"] = scope
    return Connector.objects.get(pk=discovery.create_connector(live["org"], live["admin"], data).pk)


def test_the_access_check_passes_on_the_dev_tenant(live):
    row = access.check_access(live["org"], live["admin"], connector(live))
    assert row.access_check["ok"], row.access_check["steps"]
    assert row.access_check["missing"] == []


def test_entra_lists_apps_with_credential_expiry_skips_first_party_and_a_second_run_is_unchanged(live):
    c = connector(live)
    first = discovery.run(c)
    assert first.status == RunStatus.SUCCEEDED, first.error
    seen = Observation.objects.filter(connector=c)
    assert seen.filter(data__enterprise_app=True).exists(), "no enterprise application listed"
    assert seen.filter(data__registration=True).exists(), "no app registration listed"
    # Microsoft first-party apps are skipped by default: counted, never observed.
    assert first.counts["skipped"].get("microsoft", 0) > 0, first.counts
    first_party = [t.lower() for t in entra.SCOPE_DEFAULTS["first_party_tenants"]]
    assert not seen.filter(data__publisher_tenant__in=first_party).exists()
    # Credential expiry: the app registration this connection signs in with has at least its own client secret.
    own = seen.filter(data__app_id=env("SOFTWARDEN_LIVE_CLIENT_ID").lower()).first()
    assert own is not None, "the connection's own app registration wasn't listed"
    credentials = DiscoveredCredential.objects.filter(observation=own, removed_at__isnull=True)
    assert credentials.filter(credential_type="secret", expires_on__isnull=False).exists()

    second = discovery.run(Connector.objects.get(pk=c.pk))
    assert second.status == RunStatus.SUCCEEDED, second.error
    assert (second.counts["new"], second.counts["changed"], second.counts["gone"]) == (0, 0, 0), second.counts
    assert second.counts["unchanged"] == first.counts["kept"]


def test_entra_licences_read_the_subscribed_skus(live):
    run = discovery.run(connector(live, "entra_licenses"))
    assert run.status == RunStatus.SUCCEEDED, run.error


def test_azure_reads_the_chosen_subscription(live):
    subscription = env("SOFTWARDEN_LIVE_SUBSCRIPTION_ID")
    if not subscription:
        pytest.skip("set SOFTWARDEN_LIVE_SUBSCRIPTION_ID (Reader on that subscription) to read Azure")
    live["org"], _ = set_edition(live["org"], "enterprise")
    c = connector(live, "azure", subscriptions=[subscription])
    check = access.check_access(live["org"], live["admin"], c).access_check
    assert check["ok"], check["steps"]
    run = discovery.run(Connector.objects.select_related("organization").get(pk=c.pk))
    assert run.status == RunStatus.SUCCEEDED, run.error


def test_power_platform_reads_the_chosen_environment(live):
    url = env("SOFTWARDEN_LIVE_DATAVERSE_URL")
    if not url:
        pytest.skip("set SOFTWARDEN_LIVE_DATAVERSE_URL (an environment with the app's application user)")
    live["org"], _ = set_edition(live["org"], "enterprise")
    c = connector(live, "powerplatform", environment_urls=[url])
    check = access.check_access(live["org"], live["admin"], c).access_check
    assert check["ok"], check["steps"]
    run = discovery.run(Connector.objects.select_related("organization").get(pk=c.pk))
    assert run.status == RunStatus.SUCCEEDED, run.error
