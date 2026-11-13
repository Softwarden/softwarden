"""Deadline precedence, labels, explanations, timing and gaps."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from money import renewal_rules as r

D = date


@pytest.mark.parametrize(
    "auto,renewal,notice,explicit,expected,label",
    [
        # Auto-renewing or Unknown: Entered, then Calculated, then Assumed.
        ("yes", D(2027, 1, 31), (2, "months"), D(2026, 11, 15), D(2026, 11, 15), r.ENTERED),
        ("yes", D(2027, 3, 31), (2, "months"), None, D(2027, 1, 31), r.CALCULATED),
        ("yes", D(2027, 3, 31), (1, "months"), None, D(2027, 2, 28), r.CALCULATED),
        ("unknown", D(2028, 3, 31), (1, "months"), None, D(2028, 2, 29), r.CALCULATED),
        ("yes", D(2027, 1, 31), (30, "days"), None, D(2027, 1, 1), r.CALCULATED),
        ("yes", D(2028, 2, 29), (1, "years"), None, D(2027, 2, 28), r.CALCULATED),
        ("yes", D(2027, 3, 31), None, None, D(2026, 12, 31), r.ASSUMED),
        ("unknown", D(2027, 3, 31), None, None, D(2026, 12, 31), r.ASSUMED),
        # Auto-renew No: Entered, then renewal date minus the lead time; a notice period doesn't apply.
        ("no", D(2027, 3, 31), (2, "months"), D(2027, 2, 1), D(2027, 2, 1), r.ENTERED),
        ("no", D(2027, 3, 31), (2, "months"), None, D(2026, 12, 31), r.RENEWAL_ACTION),
        # An entered deadline works without a renewal date; with neither there is none.
        ("yes", None, None, D(2026, 11, 15), D(2026, 11, 15), r.ENTERED),
        ("yes", None, (2, "months"), None, None, None),
        ("no", None, None, None, None, None),
    ],
)
def test_precedence(auto, renewal, notice, explicit, expected, label):
    value, unit = notice or (None, "")
    deadline = r.action_deadline(auto, renewal, value, unit, explicit, 90)
    assert (deadline.date, deadline.label) == (expected, label)


def test_explanations_say_how():
    calc = r.action_deadline("yes", D(2027, 1, 31), 2, "months", None, 90)
    assert calc.explanation == "Renewal date 2027-01-31 minus notice period 2 months = 2026-11-30."
    assumed = r.action_deadline("yes", D(2027, 3, 31), None, "", None, 90)
    assert assumed.explanation.startswith("Renewal date 2027-03-31 minus the 90-day default lead time = 2026-12-31.")
    assert "Add the notice period" in assumed.explanation
    assert r.action_deadline("yes", None, None, "", D(2026, 11, 15), 90).explanation == (
        "Cancellation deadline entered: 2026-11-15."
    )
    assert r.action_deadline("no", None, None, "", D(2026, 11, 15), 90).explanation == (
        "Action deadline entered: 2026-11-15."
    )
    none = r.action_deadline("yes", None, None, "", None, 90)
    assert none.explanation == "No action deadline can be worked out. Add a renewal date or the cancellation deadline."


def test_lead_time_is_the_organizations():
    assert r.action_deadline("yes", D(2027, 3, 31), None, "", None, 60).date == D(2027, 1, 30)


@pytest.mark.parametrize(
    "today,days,overdue,text",
    [
        (D(2026, 10, 31), 30, False, "in 30 days"),
        (D(2026, 11, 29), 1, False, "in 1 day"),
        (D(2026, 11, 30), 0, False, "Due today"),
        (D(2026, 12, 1), -1, True, "Overdue by 1 day"),
        (D(2026, 12, 3), -3, True, "Overdue by 3 days"),
    ],
)
def test_timing(today, days, overdue, text):
    assert r.timing(D(2026, 11, 30), today) == {"days": days, "overdue": overdue, "text": text}


def cycle(**kw):
    base = {"renewal_date": D(2027, 1, 31), "completed_on": None, "decision": "", "term_value": None, "term_unit": ""}
    return SimpleNamespace(**{**base, **kw})


ACTIVE = SimpleNamespace(lifecycle="active")
RETIRED = SimpleNamespace(lifecycle="retired")
PERSON = SimpleNamespace(active=True)
TODAY = D(2026, 11, 3)


def gaps(auto="yes", c=None, deadline=None, apps=(ACTIVE,), responsible=PERSON, followup=1, today=TODAY):
    c = c or cycle()
    deadline = deadline or r.action_deadline(auto, c.renewal_date, 2, "months", None, 90)
    return r.cycle_gaps(
        auto_renew=auto,
        cycle=c,
        deadline=deadline,
        applications=list(apps),
        responsible=responsible,
        followup_id=followup,
        today=today,
    )


def test_no_gaps_when_complete():
    assert gaps() == []


def test_each_gap():
    entered_only = cycle(renewal_date=None)
    assert gaps(c=entered_only, deadline=r.action_deadline("yes", None, None, "", D(2026, 11, 15), 90)) == [
        r.NO_RENEWAL_DATE
    ]
    assert gaps(c=entered_only, deadline=r.action_deadline("yes", None, None, "", None, 90)) == [r.NO_DEADLINE]
    assumed = r.action_deadline("yes", D(2027, 1, 31), None, "", None, 90)
    assert gaps(deadline=assumed) == [r.ASSUMED_DEADLINE]
    assert gaps(auto="unknown") == [r.UNKNOWN_AUTO_RENEWAL]
    assert gaps(apps=()) == [r.NO_APPLICATIONS]
    assert gaps(apps=(RETIRED, RETIRED)) == [r.ALL_RETIRED]
    assert gaps(apps=(RETIRED, ACTIVE)) == []
    assert gaps(responsible=None) == [r.NO_RESPONSIBLE]
    assert gaps(responsible=SimpleNamespace(active=False)) == [r.INACTIVE_RESPONSIBLE]
    assert gaps(followup=None) == [r.NO_FOLLOWUP]


def test_decision_without_completion_only_after_the_deadline():
    decided = cycle(decision="renew")
    assert gaps(c=decided, today=D(2026, 11, 30)) == []
    assert gaps(c=decided, today=D(2026, 12, 1)) == [r.DECISION_WITHOUT_COMPLETION]
    assert gaps(c=cycle(), today=D(2026, 12, 1)) == []  # overdue, but no decision: an action, not this gap


def test_completed_latest_cycle_asks_for_the_next():
    assert gaps(c=cycle(completed_on=D(2026, 11, 1), decision="renew")) == [r.NEXT_CYCLE_MISSING]


def test_next_renewal_date_adds_the_term():
    assert r.next_renewal_date(cycle(term_value=1, term_unit="years")) == D(2028, 1, 31)
    assert r.next_renewal_date(cycle(renewal_date=D(2028, 2, 29), term_value=1, term_unit="years")) == D(2029, 2, 28)
    assert r.next_renewal_date(cycle(renewal_date=D(2027, 1, 31), term_value=1, term_unit="months")) == D(2027, 2, 28)
    assert r.next_renewal_date(cycle()) is None


THRESHOLDS = [90, 60, 30, 7]
DEADLINE = D(2027, 1, 31)


@pytest.mark.parametrize(
    "today,expected",
    [
        (D(2026, 10, 1), None),  # 122 days out
        (D(2026, 11, 2), 90),  # exactly 90
        (D(2026, 12, 2), 60),  # exactly 60
        (D(2026, 12, 15), 60),
        (D(2027, 1, 24), 7),
        (D(2027, 1, 31), 7),  # due today
        (D(2027, 2, 1), r.OVERDUE),
    ],
)
def test_bucket(today, expected):
    assert r.bucket(DEADLINE, today, THRESHOLDS) == expected


def test_each_threshold_fires_once():
    fired: set[int] = set()
    for offset in range(130, -5, -1):
        today = DEADLINE - timedelta(days=offset)
        t = r.due_threshold(DEADLINE, today, THRESHOLDS, D(2026, 9, 1), fired)
        if t is not None:
            assert today == DEADLINE - timedelta(days=t)
            fired.add(t)
    assert fired == {90, 60, 30, 7}


def test_late_created_cycle_fires_only_unpassed_thresholds():
    """Created 40 days before the deadline: 90 and 60 have passed, so 30 and then 7 fire, never 90 or 60."""
    set_on = DEADLINE - timedelta(days=40)
    assert r.due_threshold(DEADLINE, set_on, THRESHOLDS, set_on, set()) is None
    thirty = DEADLINE - timedelta(days=30)
    assert r.due_threshold(DEADLINE, thirty, THRESHOLDS, set_on, set()) == 30


def test_created_on_a_threshold_day_fires_it():
    day = DEADLINE - timedelta(days=60)
    assert r.due_threshold(DEADLINE, day, THRESHOLDS, day, set()) == 60


def test_a_missed_day_still_fires_the_current_threshold_late():
    day = DEADLINE - timedelta(days=28)
    assert r.due_threshold(DEADLINE, day, THRESHOLDS, D(2026, 9, 1), {90, 60}) == 30


def test_no_threshold_when_overdue_or_none():
    assert r.due_threshold(DEADLINE, D(2027, 2, 2), THRESHOLDS, None, set()) is None
    assert r.due_threshold(None, D(2027, 1, 1), THRESHOLDS, None, set()) is None
