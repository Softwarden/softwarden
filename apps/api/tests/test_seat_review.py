"""Renewal and licences: licence pool utilisation, the "proposed seat count" review and the seat sync path
(money/licences.py). Observed seat counts are synthetic discovery observations of kind `license`, the shape the
Entra licences source produces; nothing here calls Microsoft."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from catalog import services
from core import capabilities as caps
from core.models import AuditEvent
from core.problems import Conflict, FieldErrors
from discovery.models import Connector, Observation
from money import contracts, licences
from money.models import LicensePool, SeatCountReview
from notify import digest
from notify.attention import UNCOUNTED, attention_queues
from notify.serializers import AttentionSeatCountSerializer

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]
E5 = "06ebc4ee-1bb5-47dd-8120-11324bc54e06"
E3 = "05e9a617-0261-4cee-bb44-138d3ef5d965"


def connector(org, name="Entra licences", enabled=True):
    found = Connector.objects.filter(organization=org, name=name).first()
    return found or Connector.objects.create(organization=org, source="entra", name=name, enabled=enabled)


def observe(org, sku_id, purchased, assigned, *, name="Entra licences", part="SPE_E5", gone=False, **data):
    """A licence observation as the source will record it (subscribedSkus: skuId, skuPartNumber, prepaid
    enabled units, consumed units)."""
    now = timezone.now()
    values = {"sku_id": sku_id, "sku_part_number": part, "seats_purchased": purchased, "seats_assigned": assigned}
    observation, _ = Observation.objects.update_or_create(
        connector=connector(org, name),
        kind="license",
        external_id=sku_id.lower(),
        defaults={
            "organization": org,
            "display_name": part,
            "data": {**values, **data},
            "fingerprint": f"{purchased}:{assigned}",
            "first_seen_at": now,
            "last_seen_at": now,
            "last_changed_at": now,
            "gone_at": now if gone else None,
        },
    )
    return observation


def pool(a, sku_name="Microsoft 365 E5", sku_id=E5, purchased=100, assigned=90, application=None, **extra):
    return contracts.create_pool(
        a["org"],
        a["users"]["editor"],
        {"application": str((application or a["app"]).id), "sku_name": sku_name, "sku_id": sku_id,
         "seats_purchased": purchased, "seats_assigned": assigned, **extra},
    )  # fmt: skip


def app(a, name, **extra):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", **extra})


# ---- utilisation -------------------------------------------------------------------------------------------------------


def test_utilisation_and_overuse_per_pool_and_in_totals(a, api):
    office = app(a, "Office")
    pool(a, purchased=100, assigned=90)
    pool(a, sku_name="microsoft 365 e5 (second order)", sku_id=E5.upper(), purchased=20, assigned=40)  # same SKU ID
    pool(a, sku_name="Visio", sku_id="", purchased=0, assigned=3)
    pool(a, sku_name="Visio", sku_id="", purchased=5, assigned=1, application=office)
    body = api(a["users"]["viewer"]).get("license-pools/totals").json_body
    rows = {(r["application"]["name"], r["sku_name"]): r for r in body["rows"]}
    e5 = rows[("orga App", "Microsoft 365 E5")]
    assert (e5["pools"], e5["seats_purchased"], e5["seats_assigned"], e5["seats_available"]) == (2, 120, 130, -10)
    assert (e5["utilization"], e5["overused"], e5["sku_id"]) == (108.3, True, E5)
    visio = rows[("orga App", "Visio")]
    assert (visio["utilization"], visio["overused"], visio["seats_available"]) == (None, True, -3)
    assert (
        rows[("Office", "Visio")]["utilization"] == 20.0
        and rows[("Office", "Visio")]["application"]["archived"] is False
    )
    assert body["total"] == {
        "pools": 4, "seats_purchased": 125, "seats_assigned": 134, "seats_available": -9, "utilization": 107.2,
        "overused": True, "overused_rows": 2,
    }  # fmt: skip
    pools = api(a["users"]["viewer"]).get("license-pools?overused=true").json_body["results"]
    assert {(p["sku_name"], p["utilization"]) for p in pools} == {
        ("microsoft 365 e5 (second order)", 200.0),
        ("Visio", None),
    }


def test_totals_of_an_empty_organization(a):
    assert licences.totals(a["org"]) == {
        "rows": [],
        "total": {"pools": 0, "seats_purchased": 0, "seats_assigned": 0, "seats_available": 0, "utilization": None,
                  "overused": False, "overused_rows": 0},
    }  # fmt: skip


# ---- the review queue --------------------------------------------------------------------------------------------------


def test_differences_become_proposals(a, api):
    org = a["org"]
    synced = pool(a, purchased=100, assigned=90, seat_sync=True)
    manual = pool(a, sku_name="Microsoft 365 E3", sku_id=E3, purchased=50, assigned=50)
    unbound_only = pool(a, sku_name="Copilot", sku_id="", purchased=10, assigned=10)  # no SKU ID: never matched
    observe(org, E5.upper(), 100, 97)  # case-insensitive SKU match; only assigned differs
    observe(org, E3, 60, 50, part="SPE_E3")
    observe(org, "c42b9cae-ea4f-4ab7-9717-81576235ccac", 25, 5, part="DEVELOPERPACK_E5")  # no pool has it
    observe(org, "gone-sku", 1, 1, part="GONE", gone=True)
    found = licences.proposals(org)
    assert [(p.pool.id, p.differences) for p in found] == [
        (manual.id, [{"field": "seats_purchased", "current": 50, "proposed": 60}]),
        (synced.id, [{"field": "seats_assigned", "current": 90, "proposed": 97}]),
    ]  # by application, then SKU name; syncing pools too, until the sync path runs
    body = api(a["users"]["viewer"]).get("license-pools/seat-review").json_body
    assert body["status"] == "open" and len(body["results"]) == 2
    row = body["results"][1]
    assert row["pool"]["id"] == str(synced.id) and row["pool"]["seat_sync"] is True
    assert (
        row["observation"]["display_name"] == "SPE_E5" and row["observation"]["connector"]["name"] == "Entra licences"
    )
    assert row["observed"] == {"seats_purchased": 100, "seats_assigned": 97} and row["dismissal"] is None
    assert [u["sku_part_number"] for u in body["unbound"]] == ["DEVELOPERPACK_E5"]
    one = api(a["users"]["viewer"]).get(f"license-pools/seat-review?pool={manual.id}").json_body
    assert [r["pool"]["id"] for r in one["results"]] == [str(manual.id)] and one["unbound"] == []
    listed = {p["id"]: p["seat_proposals"] for p in api(a["users"]["viewer"]).get("license-pools").json_body["results"]}
    assert listed == {str(synced.id): 1, str(manual.id): 1, str(unbound_only.id): 0}
    # A disabled connector's observations are left out; matching counts are no proposal.
    Connector.objects.filter(organization=org).update(enabled=False)
    assert licences.proposals(org) == []
    Connector.objects.filter(organization=org).update(enabled=True)
    observe(org, E3, 50, 50, part="SPE_E3")
    assert [p.pool.id for p in licences.proposals(org)] == [synced.id]


def test_bad_observed_values_never_propose_a_change(a):
    pool(a, purchased=100, assigned=90)
    observe(a["org"], E5, "lots", -1)
    assert licences.proposals(a["org"]) == []
    observe(a["org"], E5, True, 95)
    assert [p.differences for p in licences.proposals(a["org"])] == [
        [{"field": "seats_assigned", "current": 90, "proposed": 95}]
    ]


def test_seat_review_parameters(a, b, api):
    viewer = api(a["users"]["viewer"])
    refused = viewer.get("license-pools/seat-review?status=new&pool=x&colour=red")
    assert refused.status_code == 400 and set(refused.json_body["errors"]) == {"status", "pool", "colour"}
    theirs = contracts.create_pool(b["org"], b["users"]["editor"], {"application": str(b["app"].id), "sku_name": "X"})
    assert viewer.get(f"license-pools/seat-review?pool={theirs.id}").status_code == 404


# ---- applying and dismissing -------------------------------------------------------------------------------------------


def test_applying_is_an_audited_pool_edit_that_never_sets_last_synced(a, api):
    org = a["org"]
    held = pool(a, purchased=100, assigned=90)
    observation = observe(org, E5, 120, 97)
    editor = api(a["users"]["editor"])
    url = f"license-pools/{held.id}/seat-review/{observation.id}"
    assert api(a["users"]["viewer"]).post(f"{url}/apply").status_code == 403
    refused = editor.post(f"{url}/apply", {"counts": ["seats_assigned", "notes"]})
    assert refused.status_code == 400 and "counts" in refused.json_body["errors"]
    applied = editor.post(f"{url}/apply", {"counts": ["seats_assigned"]})
    assert applied.status_code == 200, applied.json_body
    assert (applied.json_body["seats_assigned"], applied.json_body["seats_purchased"]) == (97, 100)
    assert (
        applied.json_body["last_synced"] is None and applied.json_body["seat_proposals"] == 1
    )  # purchased still differs
    event = AuditEvent.objects.filter(action="license_pool.updated", target_id=held.id).latest("created_at")
    assert event.changes["seats_assigned"] == [90, 97]
    assert (event.changes["observation"], event.changes["source"]) == (str(observation.id), "Entra licences")
    assert event.actor_id == a["users"]["editor"].id
    assert SeatCountReview.objects.get(pool=held).decision == "applied"
    assert editor.post(f"{url}/apply").status_code == 200  # the rest
    held.refresh_from_db()
    assert (held.seats_purchased, held.seats_assigned, held.last_synced) == (120, 97, None)
    nothing = editor.post(f"{url}/apply")
    assert nothing.status_code == 409 and "already has the seat counts" in nothing.json_body["detail"]
    assert licences.proposals(org) == []


def test_dismissing_is_recorded_until_the_source_changes(a, api):
    org = a["org"]
    held = pool(a, purchased=100, assigned=90)
    observation = observe(org, E5, 100, 120)
    editor = api(a["users"]["editor"])
    url = f"license-pools/{held.id}/seat-review/{observation.id}/dismiss"
    assert api(a["users"]["viewer"]).post(url, {"reason": "x"}).status_code == 403
    refused = editor.post(url, {})
    assert refused.status_code == 400 and refused.json_body["errors"] == {
        "reason": ["Say why the pool keeps its counts."]
    }
    assert editor.post(url, {"reason": "x" * 501}).status_code == 400
    dismissed = editor.post(url, {"reason": "Includes 20 trial licences."})
    assert dismissed.status_code == 200 and dismissed.json_body["seat_proposals"] == 0
    held.refresh_from_db()
    assert held.seats_assigned == 90
    event = AuditEvent.objects.get(action="license_pool.seat_count_dismissed", target_id=held.id)
    assert event.changes["proposed"] == {"seats_assigned": 120} and event.changes["kept"] == {"seats_assigned": 90}
    assert event.changes["reason"] == "Includes 20 trial licences."
    assert editor.post(url, {"reason": "Again"}).status_code == 409
    viewer = api(a["users"]["viewer"])
    assert viewer.get("license-pools/seat-review").json_body["results"] == []
    hidden = viewer.get("license-pools/seat-review?status=dismissed").json_body["results"]
    assert [r["status"] for r in hidden] == ["dismissed"]
    assert hidden[0]["dismissal"]["reason"] == "Includes 20 trial licences."
    assert hidden[0]["dismissal"]["by"]["id"] == a["users"]["editor"].id
    assert [r["status"] for r in viewer.get("license-pools/seat-review?status=all").json_body["results"]] == [
        "dismissed"
    ]
    history = viewer.get(f"license-pools/{held.id}/audit").json_body["results"]
    assert any("proposed Seats assigned 120. Reason: Includes 20 trial licences." in e["summary"] for e in history)
    # The source's counts change: the proposal is back.
    observe(org, E5, 100, 125)
    assert [p.status for p in licences.proposals(org)] == ["open"]
    assert editor.post(url, {"reason": "Still trials."}).status_code == 200


def test_decisions_on_another_organizations_pool_or_observation_are_404(a, b, api):
    mine = pool(a)
    theirs = pool(b)
    their_observation = observe(b["org"], E5, 1, 1)
    my_observation = observe(a["org"], E5, 1, 1)
    editor = api(a["users"]["editor"])
    for url in (
        f"license-pools/{mine.id}/seat-review/{their_observation.id}/apply",
        f"license-pools/{theirs.id}/seat-review/{my_observation.id}/apply",
        f"license-pools/{theirs.id}/seat-review/{their_observation.id}/dismiss",
    ):
        assert editor.post(url, {"reason": "x"}).status_code == 404, url
    theirs.refresh_from_db()
    assert theirs.seats_assigned == 90
    other_sku = observe(a["org"], E3, 1, 1, part="SPE_E3")
    refused = editor.post(f"license-pools/{mine.id}/seat-review/{other_sku.id}/apply")
    assert refused.status_code == 409 and "doesn't report this pool's SKU" in refused.json_body["detail"]
    gone = observe(a["org"], E5, 1, 2, gone=True)
    refused = editor.post(f"license-pools/{mine.id}/seat-review/{gone.id}/apply")
    assert refused.status_code == 409 and "no longer reports" in refused.json_body["detail"]


def test_the_database_keeps_reviews_in_their_organization(a, b):
    from django.db import IntegrityError, connection, transaction

    observation = observe(b["org"], E5, 1, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        SeatCountReview.objects.create(
            organization=a["org"], pool=pool(a), observation=observation, decision="dismissed", fingerprint="x"
        )
        connection.check_constraints()


# ---- the sync path -------------------------------------------------------------------------------------------------------


def test_only_the_sync_path_sets_last_synced(a, api):
    org = a["org"]
    synced = pool(a, purchased=100, assigned=90, seat_sync=True)
    manual = pool(a, sku_name="Microsoft 365 E3", sku_id=E3, purchased=50, assigned=50)
    refused = api(a["users"]["editor"]).patch(f"license-pools/{synced.id}", {"last_synced": "2026-01-01T00:00:00Z"})
    assert refused.status_code == 400 and "last_synced" in refused.json_body["errors"]
    at = timezone.now() - timedelta(minutes=5)
    result = licences.sync_seats(org, observe(org, E5.upper(), 110, 104), now=at)
    assert result is not None and result.id == synced.id
    synced.refresh_from_db()
    assert (synced.seats_purchased, synced.seats_assigned, synced.last_synced) == (110, 104, at)
    event = AuditEvent.objects.get(action="license_pool.synced", target_id=synced.id)
    assert event.actor_id is None and event.changes["seats_assigned"] == [90, 104]
    assert licences.proposals(org, pools=[synced.id]) == []  # nothing left to review
    assert licences.sync_seats(org, observe(org, E3, 70, 50, part="SPE_E3")) is None  # a pool that doesn't sync
    manual.refresh_from_db()
    assert (manual.seats_purchased, manual.last_synced) == (50, None)
    assert [p.pool.id for p in licences.proposals(org)] == [manual.id]  # it is proposed instead
    # Unchanged counts: last_synced moves, nothing is audited.
    later = timezone.now()
    licences.sync_seats(org, observe(org, E5, 110, 104), now=later)
    synced.refresh_from_db()
    assert synced.last_synced == later
    assert AuditEvent.objects.filter(action="license_pool.synced").count() == 1
    with pytest.raises(ValueError):
        licences.sync_seats(org, Observation(organization=org, kind="application"))


# ---- discovery and Needs attention -------------------------------------------------------------------------------------


def test_licence_observations_are_not_discovery_candidates(a, api):
    from discovery import engine

    observation = observe(a["org"], E5, 1, 1)
    assert engine.candidate_counts(a["org"]) == {}
    body = api(a["users"]["viewer"]).get("discovery/candidates?status=all").json_body
    assert body["results"] == [] and body["counts"]["candidates"] == 0
    assert api(a["users"]["viewer"]).get(f"discovery/observations/{observation.id}").status_code == 404


def test_needs_attention_lists_pools_with_open_proposals(a, api):
    org = a["org"]
    held = pool(a, purchased=100, assigned=90)
    observe(org, E5, 100, 95)
    retired = app(a, "Old suite")
    pool(a, sku_name="Old", sku_id="old", application=retired)
    observe(org, "old", 1, 1, part="OLD")
    services.update_resource(org, a["users"]["editor"], retired, {"lifecycle": "retired"})
    queues, counts = attention_queues(org, SimpleNamespace(id=a["users"]["editor"].id), services.org_today(org))
    rows = queues["seat_counts"]
    assert [r["id"] for r in rows] == [str(held.id)]  # retired applications are left out
    assert rows[0]["why"] == "Entra licences reports 95 assigned; the pool doesn't match."
    assert rows[0]["steward"]["id"] == a["users"]["editor"].id and rows[0]["_flags"]["mine"] is True
    # Counted renders the queue: the badge and the page add up the same rows
    assert "seat_counts" not in UNCOUNTED and counts["all"] == sum(len(q) for q in queues.values())
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert set(body["seat_counts"][0]) == set(AttentionSeatCountSerializer().fields)
    observe(org, E5, 100, 96, name="Second tenant")
    assert licences.attention_rows(org)[0]["why"] == "2 sources report seat counts the pool doesn't match."


def test_a_seat_count_row_can_be_snoozed_and_joins_the_digest_when_not(a, api):
    org = a["org"]
    held = pool(a, purchased=100, assigned=90)
    observe(org, E5, 100, 95)
    editor = api(a["users"]["editor"])
    row = next(r for r in editor.get("needs-attention?scope=mine").json_body["seat_counts"] if r["id"] == str(held.id))
    today = services.org_today(org)
    # Not snoozed: in the steward's digest, under its own section, linking to the pool's seat review
    items, _ = digest.plan(org, today)
    assert row["id"] in [r["id"] for q, r, *_ in items[a["users"]["editor"].id] if q == "seat_counts"]
    assert digest.SECTION_NAMES["seat_counts"] == "Seat counts"
    assert digest._line("seat_counts", row) == (
        "Microsoft 365 E5 (orga App) — Entra licences reports 95 assigned; the pool doesn't match."
    )
    assert digest._path("seat_counts", row) == f"/license-pools/{held.id}?tab=review"
    until = (today + timedelta(days=7)).isoformat()
    made = editor.post(
        "needs-attention/snoozes",
        {"kind": "snooze", "subject_type": "seat_counts", "subject_id": row["id"], "until": until},
    )
    assert made.status_code == 201, made.json_body
    again = next(r for r in editor.get("needs-attention?scope=mine").json_body["seat_counts"] if r["id"] == row["id"])
    assert again["snooze"]["until"] == until  # shown on the row, never hidden
    items, _ = digest.plan(org, today)
    assert row["id"] not in [r["id"] for q, r, *_ in items.get(a["users"]["editor"].id, []) if q == "seat_counts"]


def test_without_the_contracts_capability_there_is_no_seat_queue(a, monkeypatch):
    pool(a, purchased=100, assigned=90)
    observe(a["org"], E5, 100, 95)
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS})
    assert licences.attention_rows(a["org"]) == []


def test_validation_of_dismiss_and_apply_inputs(a):
    held = pool(a)
    observation = observe(a["org"], E5, 1, 1)
    with pytest.raises(FieldErrors):
        licences.apply(a["org"], a["users"]["editor"], held, observation, {"counts": "seats_assigned"})
    with pytest.raises(FieldErrors):
        licences.dismiss(a["org"], a["users"]["editor"], held, observation, {"reason": 5})
    LicensePool.objects.filter(id=held.id).update(seats_purchased=1, seats_assigned=1)
    with pytest.raises(Conflict):
        licences.dismiss(a["org"], a["users"]["editor"], held, observation, {"reason": "x"})
