"""The empty-listing guard on every source whose listing defines what is "no longer seen" (Entra, Entra licences,
Azure, Power Platform, Halo; Okta's is in test_okta_google.py). A listing that comes back empty while the connector has
live observations is far more likely incomplete than true: the run fails with the guard's message, no observation gets
`gone_at`, no discovered credential is removed and no "no longer seen" row appears. All on the simulated tenants."""

import pytest

from core.models import AuditEvent, Job
from discovery import engine as discovery
from discovery.models import Connector, ConnectorRun, DiscoveredCredential, Observation, RunStatus
from tests.conftest import drain_jobs
from tests.test_discovery_sources import licence_connector
from tests.test_discovery_sources import make_connector as make_entra
from tests.test_discovery_sources_azure import URLS, configure, make

AZURE_LISTS = ["/workflows", "/sites", "/automationAccounts", "/runbooks"]


def state(connector):
    observations = sorted(
        Observation.objects.filter(connector=connector).values_list("id", "gone_at", "out_of_scope", "last_seen_at")
    )
    credentials = sorted(
        DiscoveredCredential.objects.filter(observation__connector=connector).values_list("id", "removed_at")
    )
    return observations, credentials


def assert_refused(connector, empty):
    """A good run, then a run whose listings (`empty`, the simulator's `simulated_empty`) come back empty."""
    first = discovery.run(connector)
    assert first.status == RunStatus.SUCCEEDED, first.error
    live = Observation.objects.filter(connector=connector, gone_at__isnull=True).count()
    assert live
    before = state(connector)
    configure(connector, simulated_empty=empty)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED
    assert "came back empty" in run.error and f"{live} live observation(s)" in run.error, run.error
    assert "Nothing was changed" in run.error
    assert run.failure_code == "empty_listing"  # the refusal has a code, not only words
    assert state(connector) == before
    assert not Observation.objects.filter(connector=connector, gone_at__isnull=False).exists()
    assert not Observation.objects.filter(discovery.missing_q(), connector=connector).exists()
    return run


@pytest.fixture
def ent(a):
    from core import capabilities as caps

    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a


def live_credentials(connector):
    return DiscoveredCredential.objects.filter(observation__connector=connector, removed_at__isnull=True).count()


def test_entra_an_empty_listing_is_refused_and_keeps_observations_and_credentials(a):
    connector = make_entra(a["org"], a["users"]["admin"])
    run = assert_refused(connector, ["/servicePrincipals", "/applications"])
    assert "Entra's application listing came back empty" in run.error
    assert 'run it once with "accept empty listing"' in run.error  # the way through when it's true
    assert run.error.endswith("loses its record links and dismissals.")  # the whole refusal fits the run's error
    assert live_credentials(connector) == 7  # as after the good run: none removed
    # The next good run changes nothing either: the observations were never marked gone.
    configure(connector, simulated_empty=[])
    again = discovery.run(connector)
    assert again.status == RunStatus.SUCCEEDED and again.counts["gone"] == 0 and "returned" not in again.counts


def test_the_refusal_has_a_code_in_the_api_and_other_failures_have_none(a, api):
    """Clients key on `failure_code`, never on the words. The run list, the run and the
    connector's last_run carry `empty_listing`; a failure the guard didn't cause has an empty code."""
    connector = make_entra(a["org"], a["users"]["admin"])
    refused = assert_refused(connector, ["/servicePrincipals", "/applications"])
    viewer = api(a["users"]["viewer"])
    runs = viewer.get(f"connectors/{connector.id}/runs").json_body
    assert runs[0]["id"] == str(refused.id) and runs[0]["failure_code"] == "empty_listing"
    assert viewer.get(f"connectors/{connector.id}").json_body["last_run"]["failure_code"] == "empty_listing"
    assert viewer.get(f"discovery/runs/{refused.id}").json_body["failure_code"] == "empty_listing"
    # Another failure (Graph refuses the listing) has no code.
    configure(connector, simulated_empty=[], simulated_errors={"/v1.0/servicePrincipals": 400})
    other = discovery.run(Connector.objects.get(pk=connector.pk))
    assert other.status == RunStatus.FAILED and other.failure_code == ""
    assert viewer.get(f"connectors/{connector.id}").json_body["last_run"]["failure_code"] == ""


# ---- one listing of several empty (review follow-up) ------------------------------------------------------------------


def refused_one_listing(connector, empty, listing):
    """A good run, then a run where only `empty` comes back empty: failed naming `listing`, nothing changed."""
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    before, credentials = state(connector), live_credentials(connector)
    configure(connector, simulated_empty=empty)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED, run.counts
    assert listing in run.error and "came back empty" in run.error and "Nothing was changed" in run.error, run.error
    assert state(connector) == before and live_credentials(connector) == credentials
    return run


def test_entra_only_the_enterprise_app_listing_empty_is_refused(a):
    connector = make_entra(a["org"], a["users"]["admin"])
    refused_one_listing(connector, ["/servicePrincipals"], "Entra's enterprise application listing")


def test_entra_only_the_app_registration_listing_empty_keeps_the_credentials(a):
    """No observation would go (each is still an enterprise app), but the registrations' credentials would."""
    connector = make_entra(a["org"], a["users"]["admin"])
    refused_one_listing(connector, ["/applications"], "Entra's app registration listing")


def test_azure_only_one_resource_list_empty_is_refused(ent):
    connector = make(ent["org"], ent["users"]["admin"], "azure")
    run = refused_one_listing(connector, ["/sites"], "Azure Resource Manager's list of web apps in subscription")
    assert run.error.endswith("loses its record links and dismissals."), len(run.error)


def test_powerplatform_only_one_environments_agents_empty_is_refused(ent):
    """The agents come back empty while the cloud flows list: the agents would go."""
    scope = {"environment_urls": URLS, "include_cloud_flows": True}
    connector = make(ent["org"], ent["users"]["admin"], "powerplatform", scope)
    refused_one_listing(connector, ["/bots"], "Dataverse's agent listing in")


def test_entra_licences_an_empty_listing_is_refused(a):
    entra_connector = make_entra(a["org"], a["users"]["admin"])
    assert_refused(licence_connector(a, entra_connector), ["/subscribedSkus"])


def test_azure_an_empty_listing_is_refused(ent):
    assert_refused(make(ent["org"], ent["users"]["admin"], "azure"), AZURE_LISTS)


def test_powerplatform_an_empty_listing_is_refused(ent):
    connector = make(ent["org"], ent["users"]["admin"], "powerplatform", {"environment_urls": URLS})
    assert_refused(connector, ["/bots", "/workflows"])


def test_halo_an_empty_listing_is_refused(ent):
    assert_refused(make(ent["org"], ent["users"]["admin"], "halo"), True)


def test_an_empty_listing_on_a_new_connector_is_a_successful_empty_run(a):
    """Nothing live to lose: an empty tenant is a true answer the first time."""
    connector = make_entra(a["org"], a["users"]["admin"])
    configure(connector, simulated_empty=["/servicePrincipals", "/applications"])
    run = discovery.run(connector)
    assert run.status == RunStatus.SUCCEEDED and run.counts["seen"] == 0


def test_an_empty_listing_after_everything_is_already_gone_is_not_refused(a):
    """Every observation already gone: an empty listing loses nothing, so the run succeeds."""
    connector = make_entra(a["org"], a["users"]["admin"])
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    Observation.objects.filter(connector=connector).update(gone_at=discovery.timezone.now())
    configure(connector, simulated_empty=["/servicePrincipals", "/applications"])
    assert discovery.run(connector).status == RunStatus.SUCCEEDED


# ---- accepting an empty listing for one run (review follow-up 2) -----------------------------------------------------


def test_an_admin_accepts_an_empty_listing_for_one_run(ent, api):
    """The web apps really were deleted: refused, then one Run now with accept_empty_listing marks them gone (on the
    run and the audit event), and the next ordinary run succeeds with nothing more to lose."""
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "azure")
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    configure(connector, simulated_empty=["/sites"])
    assert discovery.run(connector).status == RunStatus.FAILED
    sites = Observation.objects.filter(connector=connector, gone_at__isnull=True, data__scope__endswith="/type:sites")
    expected = sites.count()
    assert expected

    client = api(admin)
    for role in ("editor", "viewer"):  # running a connector is Admin only, with or without the flag
        response = api(ent["users"][role]).post(f"connectors/{connector.id}/run", {"accept_empty_listing": True})
        assert response.status_code == 403
    assert client.post(f"connectors/{connector.id}/run", {"accept_empty_listing": "yes"}).status_code == 400
    response = client.post(f"connectors/{connector.id}/run", {"accept_empty_listing": True})
    assert response.status_code == 202, response.json_body
    job = Job.objects.get(id=response.json_body["active_run"]["job_id"])
    assert job.payload == {"connector_id": str(connector.id), "accept_empty_listing": True}
    event = AuditEvent.objects.filter(organization=org, action="connector.run_requested").latest("created_at")
    assert event.changes["accept_empty_listing"] is True and event.actor_id == admin.id
    drain_jobs()
    accepted = ConnectorRun.objects.filter(connector=connector).latest("created_at")
    assert accepted.status == RunStatus.SUCCEEDED, accepted.error
    assert accepted.counts["accept_empty_listing"] is True and accepted.counts["gone"] == expected
    assert not sites.exists()

    # Only that run: the next ordinary run is guarded again, and has nothing left to lose from the empty list.
    again = discovery.run(Connector.objects.get(pk=connector.pk))
    assert again.status == RunStatus.SUCCEEDED and again.counts["gone"] == 0
    assert "accept_empty_listing" not in again.counts
    configure(connector, simulated_empty=["/workflows"])
    assert discovery.run(Connector.objects.get(pk=connector.pk)).status == RunStatus.FAILED


def test_an_ordinary_run_now_records_no_acceptance(ent, api):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "halo")
    response = api(admin).post(f"connectors/{connector.id}/run")
    assert response.status_code == 202
    assert Job.objects.get(id=response.json_body["active_run"]["job_id"]).payload == {"connector_id": str(connector.id)}
    event = AuditEvent.objects.filter(organization=org, action="connector.run_requested").latest("created_at")
    assert "accept_empty_listing" not in event.changes


def test_accepting_also_lets_the_whole_listing_guard_through(a):
    """The direct run takes the flag too; with it, an Entra tenant whose lists both came back empty goes gone."""
    connector = make_entra(a["org"], a["users"]["admin"])
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    configure(connector, simulated_empty=["/servicePrincipals", "/applications"])
    run = discovery.run(connector, accept_empty_listing=True)
    assert run.status == RunStatus.SUCCEEDED and run.counts["gone"] > 0 and live_credentials(connector) == 0
