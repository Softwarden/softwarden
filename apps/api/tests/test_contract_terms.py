"""Contract term statuses: the daily schedule that moves terms along their dates, and the
renew-action follow-ups from the renewal and licences review (default end dates at month ends and 29 February, a
start sent without an end, custom field values carried to the next term and to copied pools, the refusal for a
cancelled next term, and the symmetric link rule). The one source of truth is checked after every test."""

from datetime import UTC, date, datetime, timedelta

import pytest

from catalog import customfields, quality
from core import jobs
from core.models import AuditEvent
from core.problems import Conflict, FieldErrors
from money import contracts, renewal_action, renewals, terms
from money.models import Contract, LicensePool, RenewalCycle

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]
D = date


def make(a, name="Term", **data):
    return contracts.create_contract(a["org"], a["users"]["editor"], {"name": name, **data})


def contract_check(org, record) -> str:
    result = quality.evaluate(org, [record.id], rules=[quality.BY_ID["contract"]])[record.id]
    return "fail" if result.failing else ("pass" if result.passing else "n/a")


# ---- A. the daily status job ---------------------------------------------------------------------------------------------


def test_signed_becomes_active_on_its_start_date(a):
    signed = make(a, status="signed", start_date="2027-04-01", end_date="2028-03-31")
    assert terms.advance_terms(a["org"], D(2027, 3, 31)) == {"started": 0, "renewed": 0, "expired": 0, "failed": 0}
    assert terms.advance_terms(a["org"], D(2027, 4, 1)) == {"started": 1, "renewed": 0, "expired": 0, "failed": 0}
    signed.refresh_from_db()
    assert signed.status == "active"
    event = AuditEvent.objects.filter(target_id=signed.id, action="contract.updated").latest("created_at")
    assert event.changes == {"status": ["signed", "active"], "by": "term dates"} and event.actor_id is None


def test_a_renewed_term_becomes_renewed_after_its_end_date(a):
    old = make(a, "Old", create_renewal_obligation=True, start_date="2026-04-01", end_date="2027-03-31",
               auto_renew="no")  # fmt: skip
    outcome = renewal_action.renew(a["org"], a["users"]["editor"], old, {"decision": "renew"})
    new = outcome.successor
    assert new is not None and new.status == "signed"
    assert terms.advance_terms(a["org"], D(2027, 3, 31)) == {
        "started": 0,
        "renewed": 0,
        "expired": 0,
        "failed": 0,
    }  # last day
    assert terms.advance_terms(a["org"], D(2027, 4, 1)) == {"started": 1, "renewed": 1, "expired": 0, "failed": 0}
    old.refresh_from_db()
    new.refresh_from_db()
    assert (old.status, new.status) == ("renewed", "active")
    assert contracts.driver(old.renewal_obligation).id == new.id  # type: ignore[union-attr]


@pytest.mark.parametrize(("auto_renew", "expected"), [("no", "expired"), ("unknown", "expired"), ("yes", "active")])
def test_an_ended_term_without_a_next_term(a, auto_renew, expected):
    term = make(a, end_date="2027-03-31", auto_renew=auto_renew, status="pending_renewal")
    terms.advance_terms(a["org"], D(2027, 4, 1))
    term.refresh_from_db()
    assert term.status == ("pending_renewal" if expected == "active" else expected)  # auto-renewing: rolls over


def test_a_whole_past_signed_term_goes_through_active(a):
    term = make(a, status="signed", start_date="2026-01-01", end_date="2026-06-30", auto_renew="no")
    assert terms.advance_terms(a["org"], D(2026, 11, 5)) == {"started": 1, "renewed": 0, "expired": 1, "failed": 0}
    term.refresh_from_db()
    assert term.status == "expired"
    pairs = [e.changes["status"] for e in AuditEvent.objects.filter(target_id=term.id, action="contract.updated")
             .order_by("created_at")]  # fmt: skip
    assert pairs == [["signed", "active"], ["active", "expired"]]


def test_left_alone(a):
    org = a["org"]
    archived = contracts.archive_contract(org, a["users"]["editor"], make(a, "Archived", end_date="2026-01-31"))
    open_ended = make(a, "Open")
    cancelled = make(a, "Cancelled", end_date="2026-01-31", status="cancelled")
    future = make(a, "Future", end_date="2028-01-31")
    assert terms.advance_terms(org, D(2027, 1, 1)) == {"started": 0, "renewed": 0, "expired": 0, "failed": 0}
    for c in (archived, open_ended, cancelled, future):
        before = c.status
        c.refresh_from_db()
        assert c.status == before


def test_rerunning_changes_nothing(a):
    make(a, status="signed", start_date="2026-01-01", end_date="2026-06-30")
    first = terms.advance_terms(a["org"], D(2026, 11, 5))
    events = AuditEvent.objects.count()
    assert first == {"started": 1, "renewed": 0, "expired": 1, "failed": 0}
    assert terms.advance_terms(a["org"], D(2026, 11, 5)) == {"started": 0, "renewed": 0, "expired": 0, "failed": 0}
    assert AuditEvent.objects.count() == events


def test_an_expired_term_stops_driving_its_renewal(a):
    term = make(a, create_renewal_obligation=True, end_date="2027-03-31", auto_renew="no")
    obligation = term.renewal_obligation
    assert contracts.driver(obligation).id == term.id  # type: ignore[union-attr]
    terms.advance_terms(a["org"], D(2027, 4, 1))
    assert contracts.driver(obligation) is None
    cycle = RenewalCycle.objects.get(obligation=obligation)
    assert cycle.renewal_date == D(2027, 3, 31)  # the renewal keeps the dates it had and is editable again


def test_each_organization_on_its_own_date(a, b):
    """At 2027-03-31 23:30 UTC it is already 1 April in Auckland and still 31 March in Los Angeles."""
    a["org"].timezone = "Pacific/Auckland"
    a["org"].save()
    b["org"].timezone = "America/Los_Angeles"
    b["org"].save()
    mine = make(a, status="signed", start_date="2027-04-01", end_date="2028-03-31")
    theirs = contracts.create_contract(
        b["org"], b["users"]["editor"], {"name": "Theirs", "status": "signed", "start_date": "2027-04-01",
                                         "end_date": "2028-03-31"}
    )  # fmt: skip
    now = datetime(2027, 3, 31, 23, 30, tzinfo=UTC)
    assert terms.scheduled_advance(now) == {"started": 1, "renewed": 0, "expired": 0, "failed": 0, "deadlines": 0}
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert (mine.status, theirs.status) == ("active", "signed")
    assert terms.scheduled_advance(now + timedelta(hours=8))["started"] == 1  # Los Angeles reaches 1 April
    theirs.refresh_from_db()
    assert theirs.status == "active"


def test_the_schedule_is_registered_and_listed(a, api):
    assert jobs.SCHEDULES["contracts.advance_terms"].func is terms.scheduled_advance
    jobs.sync_schedules()
    body = api(a["users"]["admin"]).get("system").json_body
    row = next(s for s in body["schedules"] if s["name"] == "contracts.advance_terms")
    assert row["cron"] == "10 * * * *" and row["label"].startswith("Move contract terms")


def test_an_expired_term_no_longer_counts_for_data_quality(a):
    org = a["org"]
    term = make(a, end_date="2027-03-31", auto_renew="no")
    contracts.add_item(org, a["users"]["editor"], term, {"record": str(a["app"].id)})
    assert contract_check(org, a["app"]) == "pass"
    terms.advance_terms(org, D(2027, 4, 1))
    assert contract_check(org, a["app"]) == "fail"


# ---- B. default end dates -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (D(2026, 4, 1), D(2027, 3, 31), D(2028, 3, 31)),  # a year, across 29 February
        (D(2024, 2, 29), D(2025, 2, 28), D(2026, 2, 28)),  # month ends: a year
        (D(2024, 1, 31), D(2024, 4, 30), D(2024, 7, 31)),  # month ends: three months
        (D(2024, 8, 31), D(2025, 2, 28), D(2025, 8, 31)),  # month ends: six months
        (D(2026, 1, 15), D(2026, 7, 14), D(2027, 1, 14)),  # whole months mid-month
        (D(2026, 1, 10), D(2026, 2, 20), D(2026, 4, 3)),  # neither: the same number of days
        (None, D(2026, 12, 31), D(2027, 12, 31)),  # no start: a year
    ],
)
def test_next_end(start, end, expected):
    assert renewal_action.next_end(start, end, end + timedelta(days=1)) == expected


def test_a_start_sent_without_an_end_takes_an_end_from_it(a):
    old = make(a, start_date="2026-04-01", end_date="2027-03-31")
    new = renewal_action.renew(
        a["org"], a["users"]["editor"], old, {"decision": "renew", "start_date": "2027-05-01"}
    ).successor
    assert new is not None and (new.start_date, new.end_date) == (D(2027, 5, 1), D(2028, 4, 30))


# ---- C. and E. custom fields carried over ---------------------------------------------------------------------------------


def field(a, name, applies_to, **extra):
    return customfields.create_field(
        a["org"], a["users"]["admin"], {"name": name, "label": name.title(), "type": "text", "applies_to": applies_to,
                                        **extra}
    )  # fmt: skip


def test_custom_field_values_go_to_the_next_term_and_copied_pools(a):
    org, editor = a["org"], a["users"]["editor"]
    field(a, "budget_code", ["contract"])
    field(a, "tenant", ["license_pool"])
    old = make(a, end_date="2027-03-31", custom_field_data={"budget_code": "B-12"})
    pool = contracts.create_pool(
        org, editor, {"application": str(a["app"].id), "contract": str(old.id), "sku_name": "E5",
                      "custom_field_data": {"tenant": "Contoso"}}
    )  # fmt: skip
    field(a, "tenant_region", ["license_pool"], required=True, default="EU")  # required since; has a default
    new = renewal_action.renew(org, editor, old, {"decision": "renew", "pool_action": "copy"}).successor
    assert new is not None and new.custom_field_data == {"budget_code": "B-12"}
    copy = LicensePool.objects.get(contract=new)
    assert copy.custom_field_data == {"tenant": "Contoso", "tenant_region": "EU"}
    assert pool.custom_field_data == {"tenant": "Contoso"}


def test_a_required_field_added_since_must_be_given(a):
    org, editor = a["org"], a["users"]["editor"]
    old = make(a, end_date="2027-03-31")
    field(a, "owner_code", ["contract"], required=True)
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(org, editor, old, {"decision": "renew"})
    assert refused.value.errors == {"custom_field_data.owner_code": ["Owner_Code is required."]}
    new = renewal_action.renew(
        org, editor, old, {"decision": "renew", "custom_field_data": {"owner_code": "OC-1"}}
    ).successor
    assert new is not None and new.custom_field_data == {"owner_code": "OC-1"}


def test_a_required_pool_field_with_no_value_refuses_the_copy(a):
    org, editor = a["org"], a["users"]["editor"]
    old = make(a, end_date="2027-03-31")
    contracts.create_pool(org, editor, {"application": str(a["app"].id), "contract": str(old.id), "sku_name": "E5"})
    field(a, "cost_owner", ["license_pool"], required=True)
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(org, editor, old, {"decision": "renew", "pool_action": "copy"})
    assert refused.value.errors == {
        "pools": [
            "License pool E5 can't be copied: Cost_Owner is required. Fill it on the pool, or move the pool instead."
        ]
    }
    assert not Contract.objects.filter(predecessor=old).exists()  # nothing written
    moved = renewal_action.renew(org, editor, old, {"decision": "renew"}).successor  # moving doesn't re-validate
    assert moved is not None and LicensePool.objects.get(sku_name="E5").contract_id == moved.id


# ---- E. the refusal for a cancelled next term -----------------------------------------------------------------------------


def test_a_cancelled_next_term_says_to_create_a_new_contract(a):
    org, editor = a["org"], a["users"]["editor"]
    old = make(a, end_date="2027-03-31")
    new = renewal_action.renew(org, editor, old, {"decision": "renew"}).successor
    assert new is not None
    renewal_action.renew(org, editor, new, {"decision": "cancel", "decision_notes": "Changed our minds."})
    with pytest.raises(Conflict) as refused:
        renewal_action.renew(org, editor, old, {"decision": "renew"})
    assert str(refused.value.detail) == (
        "This term already has a next term, Term, which is canceled. A term is renewed once: create a new contract "
        "and link it to the renewal instead."
    )


# ---- D. the symmetric link rule -------------------------------------------------------------------------------------------


def test_an_earlier_term_may_link_the_renewal_its_successor_drives(a):
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", end_date="2027-03-31")
    second = renewal_action.renew(org, editor, first, {"decision": "renew"}).successor
    assert second is not None
    second = contracts.update_contract(org, editor, second, {"create_renewal_obligation": True})
    obligation = second.renewal_obligation
    assert obligation is not None
    contracts.update_contract(org, editor, first, {"renewal_obligation": str(obligation.id)})  # accepted
    first.refresh_from_db()
    assert first.renewal_obligation_id == obligation.id
    assert contracts.driver(obligation).id == second.id  # type: ignore[union-attr]
    cycle = RenewalCycle.objects.get(obligation=obligation)
    assert cycle.renewal_date == second.end_date == D(2028, 3, 31)
    unrelated = make(a, "Unrelated", end_date="2027-09-30")
    with pytest.raises(FieldErrors) as refused:
        contracts.update_contract(org, editor, unrelated, {"renewal_obligation": str(obligation.id)})
    assert refused.value.errors == {"renewal_obligation": ["This renewal already takes its dates from the contract A."]}


# ---- a Renewed term doesn't drive (review decision) -----------------------------------------------------------------------


def chain(a):
    """A (2026-04-01..2027-03-31) renewed by B (..2028-03-31, not auto-renewing), A already Renewed; B drives."""
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", create_renewal_obligation=True, start_date="2026-04-01", end_date="2027-03-31",
                 auto_renew="yes")  # fmt: skip
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "auto_renew": "no", "name": "B"}).successor
    assert second is not None
    terms.advance_terms(org, D(2027, 4, 1))
    first.refresh_from_db()
    assert first.status == "renewed" and not contracts.can_drive(first)
    obligation = first.renewal_obligation
    assert obligation is not None and contracts.driver(obligation).id == second.id  # type: ignore[union-attr]
    return first, second, obligation


def keeps_b_dates(obligation):
    assert contracts.driver(obligation) is None  # A doesn't take over
    cycle = RenewalCycle.objects.get(obligation=obligation, completed_on__isnull=True)
    assert (cycle.sequence, cycle.renewal_date, cycle.notice_value) == (2, D(2028, 3, 31), None)
    obligation.refresh_from_db()
    assert obligation.auto_renew == "no"
    return cycle


def test_when_the_next_term_expires_the_renewal_keeps_its_dates_and_is_editable(a):
    org, editor = a["org"], a["users"]["editor"]
    _first, second, obligation = chain(a)
    assert terms.advance_terms(org, D(2028, 4, 1))["expired"] == 1
    second.refresh_from_db()
    assert second.status == "expired"
    cycle = keeps_b_dates(obligation)
    from money import renewals

    edited = renewals.update_cycle(org, editor, obligation, cycle.id, {"renewal_date": "2028-06-30"})  # editable
    assert edited.renewal_date == D(2028, 6, 30)
    renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "cancel"})
    renewals.record_completion(
        org, editor, obligation, cycle.id, {"completed_on": "2026-11-05", "completion_evidence": "Notice sent"}
    )
    following = renewals.establish_next_cycle(org, editor, obligation, {})  # no driver: the renewal's own defaults
    assert following.sequence == 3 and following.renewal_date is None  # no term on the cycle to add


@pytest.mark.parametrize("how", ["cancel", "archive"])
def test_when_the_next_term_is_cancelled_or_archived_the_renewal_keeps_its_dates(a, how):
    org, editor = a["org"], a["users"]["editor"]
    first, second, obligation = chain(a)
    if how == "cancel":
        renewal_action.renew(org, editor, second, {"decision": "cancel", "decision_notes": "Dropped."})
    else:
        contracts.archive_contract(org, editor, second)
    for day in (D(2028, 4, 1), D(2028, 4, 2)):  # A (auto-renewing, Renewed) expires; it never drives again
        terms.advance_terms(org, day)
        first.refresh_from_db()
        assert first.status == "expired"
        assert contracts.driver(obligation) is None
    if how == "cancel":  # cycle 2 was completed by the cancellation
        assert not RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).exists()
        last = RenewalCycle.objects.get(obligation=obligation, sequence=2)
        assert last.renewal_date == D(2028, 3, 31)  # B's dates, not A's
    else:
        cycle = keeps_b_dates(obligation)
        renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "cancel"})
        renewals.record_completion(
            org, editor, obligation, cycle.id, {"completed_on": "2026-11-05", "completion_evidence": "Notice sent"}
        )
    following = renewals.establish_next_cycle(org, editor, obligation, {})  # not stuck: no driver to wait for
    assert following.sequence == 3


def test_a_renewed_term_coming_back_is_a_revive(a):
    """Setting a Renewed term back to a status that counts is refused while its next term counts (an expired one
    still renewed it), and, once it doesn't, while an unrelated contract drives its renewal, as for a cancelled or
    expired term; a renewed term can't be renewed again."""
    org, editor = a["org"], a["users"]["editor"]
    first, second, obligation = chain(a)
    terms.advance_terms(org, D(2028, 4, 1))  # B expires: no driver
    other = make(a, "C", end_date="2029-03-31", renewal_obligation=str(obligation.id))
    assert contracts.driver(obligation).id == other.id  # type: ignore[union-attr]
    with pytest.raises(FieldErrors) as refused:
        contracts.update_contract(org, editor, first, {"status": "active"})
    assert refused.value.errors["status"] == ["This term was renewed by B. Edit B instead."]
    contracts.archive_contract(org, editor, second)  # B no longer counts
    with pytest.raises(FieldErrors) as refused:
        contracts.update_contract(org, editor, first, {"status": "active"})
    assert refused.value.errors["status"] == [
        "This renewal now takes its dates from the contract C. Unlink one of them first."
    ]
    with pytest.raises(Conflict) as again:
        renewal_action.renew(org, editor, first, {"decision": "renew"})
    assert "already has a next term" in str(again.value.detail)


# ---- review follow-ups ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("auto_renew", "expected"), [("no", "expired"), ("yes", "active")])
def test_a_cancelled_next_term_did_not_renew_it(a, auto_renew, expected):
    """A renewed early (A Active, B Signed), B cancelled: at A's end nothing renewed A."""
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", create_renewal_obligation=True, start_date="2026-04-01", end_date="2027-03-31",
                 auto_renew=auto_renew)  # fmt: skip
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    renewal_action.renew(org, editor, second, {"decision": "cancel", "decision_notes": "Dropped."})
    obligation = first.renewal_obligation
    assert contracts.driver(obligation).id == first.id  # type: ignore[union-attr]  # A drives again
    assert first not in terms.due(org, D(2027, 3, 31))
    counts = terms.advance_terms(org, D(2027, 4, 1))
    first.refresh_from_db()
    assert first.status == expected
    assert counts["renewed"] == 0 and counts["expired"] == (1 if expected == "expired" else 0)
    assert list(terms.due(org, D(2027, 4, 1))) == []  # an ended auto-renewing term isn't selected again


def test_an_archived_next_term_did_not_renew_it_and_an_expired_one_did(a):
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", start_date="2026-04-01", end_date="2027-03-31", auto_renew="no")
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    contracts.update_contract(org, editor, second, {"status": "expired"})
    terms.advance_terms(org, D(2027, 4, 1))
    first.refresh_from_db()
    assert first.status == "renewed"  # an expired next term still renewed it
    contracts.archive_contract(org, editor, second)
    assert terms.advance_terms(org, D(2027, 4, 2))["expired"] == 1  # the Renewed term is looked at again
    first.refresh_from_db()
    assert first.status == "expired"


def test_a_renewed_term_whose_next_term_is_deleted_expires_even_when_it_auto_renews(a):
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", create_renewal_obligation=True, start_date="2025-04-01", end_date="2026-03-31",
                 auto_renew="yes")  # fmt: skip
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    first.refresh_from_db()
    assert first.status == "renewed"  # ended before the decision
    contracts.delete_contract(org, a["users"]["admin"], second)
    assert contracts.driver(first.renewal_obligation) is None
    assert terms.advance_terms(org, D(2026, 11, 5))["expired"] == 1
    first.refresh_from_db()
    assert first.status == "expired"
    assert contracts.driver(first.renewal_obligation) is None  # never Active again: it doesn't take the renewal back
    assert terms.advance_terms(org, D(2026, 11, 5)) == {"started": 0, "renewed": 0, "expired": 0, "failed": 0}


def test_a_renewed_term_whose_next_term_is_archived_expires(a):
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", start_date="2025-04-01", end_date="2026-03-31", auto_renew="no")
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    contracts.archive_contract(org, editor, second)
    terms.advance_terms(org, D(2026, 11, 5))
    first.refresh_from_db()
    assert first.status == "expired"


def test_a_renewed_term_that_has_not_ended_is_left_alone(a):
    """Only a manual edit makes one: the job doesn't select it, hour after hour."""
    org = a["org"]
    term = make(a, "A", end_date="2027-03-31")
    Contract.objects.filter(id=term.id).update(status="renewed")
    assert list(terms.due(org, D(2026, 11, 5))) == []
    assert terms.advance_terms(org, D(2027, 4, 1))["expired"] == 1  # once it has ended


def test_a_renewed_term_cannot_be_set_back_while_its_next_term_counts(a):
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", start_date="2025-04-01", end_date="2026-03-31")
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    first.refresh_from_db()
    assert first.status == "renewed"
    for status in ("active", "pending_renewal", "signed"):
        with pytest.raises(FieldErrors) as refused:
            contracts.update_contract(org, editor, first, {"status": status})
        assert refused.value.errors == {"status": ["This term was renewed by B. Edit B instead."]}
    contracts.update_contract(org, editor, first, {"status": "expired"})  # to a status that doesn't count: fine


@pytest.mark.parametrize("via", ["expired", "cancelled"])
def test_the_renewed_by_guard_holds_through_an_intermediate_status(a, via):
    """Renewed → Expired (or Cancelled) → Active in two edits doesn't get round "renewed by B" (review nit): once the
    term has ended the guard applies whatever status it comes back from, not only from Renewed."""
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", start_date="2025-04-01", end_date="2026-03-31")
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    first.refresh_from_db()
    assert first.status == "renewed"
    contracts.update_contract(org, editor, first, {"status": via})
    for status in ("active", "pending_renewal", "signed"):
        with pytest.raises(FieldErrors) as refused:
            contracts.update_contract(org, editor, first, {"status": status})
        assert refused.value.errors == {"status": ["This term was renewed by B. Edit B instead."]}
    contracts.archive_contract(org, editor, second)  # B no longer counts: A may come back
    first.refresh_from_db()
    assert contracts.update_contract(org, editor, first, {"status": "active"}).status == "active"


def test_counting_successor_counts_a_next_term_until_it_stops_counting(a):
    """(The composite key keeps a next term in its predecessor's organization, so there is no cross-organization case
    to build; the query is scoped anyway.)"""
    first = make(a, "A", start_date="2025-04-01", end_date="2026-03-31")
    assert not terms.counting_successor(first)
    second = renewal_action.renew(a["org"], a["users"]["editor"], first, {"decision": "renew", "name": "B"}).successor
    assert second is not None and terms.counting_successor(first)
    contracts.archive_contract(a["org"], a["users"]["editor"], second)
    assert not terms.counting_successor(first)


def renewed_early(a):
    """A, still running (ends in 2027), renewed early by B: A stays Active until its end date."""
    org, editor = a["org"], a["users"]["editor"]
    first = make(a, "A", start_date="2026-04-01", end_date="2027-03-31")
    second = renewal_action.renew(org, editor, first, {"decision": "renew", "name": "B"}).successor
    assert second is not None
    first.refresh_from_db()
    assert first.status == "active"
    return first


@pytest.mark.parametrize("via", ["cancelled", "expired"])
def test_a_term_that_has_not_ended_may_come_back_under_its_next_term(a, via):
    """Renewed early, then cancelled (or expired) by mistake: it can be set back while its term runs (review decision),
    so it counts as spend again; the next term still drives."""
    org, editor = a["org"], a["users"]["editor"]
    first = renewed_early(a)
    contracts.update_contract(org, editor, first, {"status": via})
    assert contracts.update_contract(org, editor, first, {"status": "active"}).status == "active"


def test_a_term_that_has_not_ended_is_refused_from_renewed_or_when_the_edit_ends_it(a):
    org, editor = a["org"], a["users"]["editor"]
    first = renewed_early(a)
    contracts.update_contract(org, editor, first, {"status": "cancelled"})
    with pytest.raises(FieldErrors) as refused:  # its new end date has passed
        contracts.update_contract(org, editor, first, {"status": "active", "end_date": "2026-06-30"})
    assert refused.value.errors == {"status": ["This term was renewed by B. Edit B instead."]}
    Contract.objects.filter(id=first.id).update(status="renewed")  # a manual Renewed before its end
    first.refresh_from_db()
    with pytest.raises(FieldErrors) as refused:
        contracts.update_contract(org, editor, first, {"status": "active"})
    assert refused.value.errors == {"status": ["This term was renewed by B. Edit B instead."]}


def test_a_signed_term_without_a_start_date_moves_once_it_has_ended(a):
    term = make(a, status="signed", end_date="2026-06-30", auto_renew="no")
    assert terms.advance_terms(a["org"], D(2026, 6, 30))["started"] == 0
    assert terms.advance_terms(a["org"], D(2026, 7, 1)) == {"started": 1, "renewed": 0, "expired": 1, "failed": 0}
    term.refresh_from_db()
    assert term.status == "expired"


def test_one_organization_failing_does_not_stop_the_others(a, b, monkeypatch):
    make(a, status="signed", start_date="2026-01-01", end_date="2028-01-01")
    contracts.create_contract(
        b["org"], b["users"]["editor"], {"name": "Theirs", "status": "signed", "start_date": "2026-01-01",
                                         "end_date": "2028-01-01"}
    )  # fmt: skip
    real = terms.advance_terms

    def flaky(org, today):
        if org.id == a["org"].id:
            raise RuntimeError("boom")
        return real(org, today)

    monkeypatch.setattr(terms, "advance_terms", flaky)
    result = terms.scheduled_advance(datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    assert result == {"started": 1, "renewed": 0, "expired": 0, "failed": 1, "deadlines": 0}


def test_one_contract_failing_does_not_stop_its_organization(a, monkeypatch):
    bad = make(a, "Bad", status="signed", start_date="2026-01-01", end_date="2028-01-01")
    good = make(a, "Good", status="signed", start_date="2026-01-01", end_date="2028-01-01")
    real = terms.advance_contract

    def flaky(org, contract, today):
        if contract.id == bad.id:
            raise RuntimeError("boom")
        return real(org, contract, today)

    monkeypatch.setattr(terms, "advance_contract", flaky)
    assert terms.advance_terms(a["org"], D(2026, 11, 5)) == {"started": 1, "renewed": 0, "expired": 0, "failed": 1}
    good.refresh_from_db()
    assert good.status == "active"


def test_a_contract_changed_or_deleted_after_it_was_selected_is_judged_as_it_is_now(a):
    org, editor = a["org"], a["users"]["editor"]
    archived = make(a, "Archived", status="signed", start_date="2026-01-01", end_date="2028-01-01")
    edited = make(a, "Edited", end_date="2026-06-30", auto_renew="no")
    gone = make(a, "Gone", end_date="2026-06-30", auto_renew="no")
    today = D(2026, 11, 5)
    selected = {c.name: c for c in terms.due(org, today)}
    assert set(selected) == {"Archived", "Edited", "Gone"}
    contracts.archive_contract(org, editor, archived)
    contracts.update_contract(org, editor, edited, {"end_date": "2027-06-30"})
    contracts.delete_contract(org, a["users"]["admin"], gone)
    for contract in selected.values():
        assert terms.advance_contract(org, contract, today) == []
    archived.refresh_from_db()
    edited.refresh_from_db()
    assert (archived.status, edited.status) == ("signed", "active")


def test_the_history_says_the_change_came_from_the_term_dates(a, api):
    term = make(a, status="signed", start_date="2026-01-01", end_date="2028-01-01")
    terms.advance_terms(a["org"], D(2026, 11, 5))
    history = api(a["users"]["viewer"]).get(f"contracts/{term.id}/audit").json_body["results"]
    assert history[0]["summary"] == "Status: signed → active (from the term dates)"


def test_stale_custom_field_values_are_not_carried(a):
    """A field no longer applying to contracts or pools can leave values behind; the next term and copied pools drop
    them instead of refusing the renewal."""
    org, editor = a["org"], a["users"]["editor"]
    field(a, "budget_code", ["contract"])
    old = make(a, end_date="2027-03-31", custom_field_data={"budget_code": "B-1"})
    pool = contracts.create_pool(
        org, editor, {"application": str(a["app"].id), "contract": str(old.id), "sku_name": "E5"}
    )
    Contract.objects.filter(id=old.id).update(custom_field_data={"budget_code": "B-1", "retired_field": "x"})
    LicensePool.objects.filter(id=pool.id).update(custom_field_data={"retired_field": "y"})
    old.refresh_from_db()
    new = renewal_action.renew(org, editor, old, {"decision": "renew", "pool_action": "copy"}).successor
    assert new is not None and new.custom_field_data == {"budget_code": "B-1"}
    assert LicensePool.objects.get(contract=new).custom_field_data == {}


def test_a_pool_copy_problem_names_the_field(a):
    org, editor = a["org"], a["users"]["editor"]
    field(a, "tier", ["license_pool"], type="select", choices=["a", "b"])
    old = make(a, end_date="2027-03-31")
    pool = contracts.create_pool(
        org, editor, {"application": str(a["app"].id), "contract": str(old.id), "sku_name": "E5",
                      "custom_field_data": {"tier": "a"}}
    )  # fmt: skip
    LicensePool.objects.filter(id=pool.id).update(custom_field_data={"tier": "z"})  # no longer a choice
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(org, editor, old, {"decision": "renew", "pool_action": "copy"})
    assert refused.value.errors == {
        "pools": [
            "License pool E5 can't be copied: Tier: Choose one of: a, b. Fill it on the pool, or move the pool instead."
        ]
    }


def test_a_term_that_has_not_ended_is_refused_when_its_next_term_cannot_drive(a):
    """Review decision: a running old term doesn't come back while a counting next term renews it but can't drive
    (here set to Expired), so a revived old term never drives under a counting successor."""
    org, editor = a["org"], a["users"]["editor"]
    first = renewed_early(a)
    contracts.update_contract(org, editor, first, {"status": "cancelled"})
    second = Contract.objects.get(predecessor=first)
    contracts.update_contract(org, editor, second, {"status": "expired"})
    first.refresh_from_db()
    with pytest.raises(FieldErrors) as refused:
        contracts.update_contract(org, editor, first, {"status": "active"})
    assert refused.value.errors == {"status": ["This term was renewed by B. Edit B instead."]}
    contracts.update_contract(org, editor, second, {"status": "active"})  # B drives again: A may come back
    first.refresh_from_db()
    assert contracts.update_contract(org, editor, first, {"status": "active"}).status == "active"
