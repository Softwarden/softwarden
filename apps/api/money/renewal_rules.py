"""Derived renewal state: the action deadline with its label and explanation, the timing, and the gaps.

Pure functions over loaded values, like state.py. The action deadline is never stored.

Deadline precedence:
- auto-renew Yes or Unknown (a cancellation deadline): Entered, else renewal date minus notice period (Calculated),
  else renewal date minus the organization lead time (Assumed);
- auto-renew No (renew before expiry): Entered, else renewal date minus the lead time (Renewal action).
An entered deadline drives reminders even without a renewal date; with neither there is no deadline and no reminder.
"""

from dataclasses import dataclass
from datetime import date

from core import dates

ENTERED, CALCULATED, ASSUMED, RENEWAL_ACTION = "entered", "calculated", "assumed", "renewal_action"
LABELS = {ENTERED: "Entered", CALCULATED: "Calculated", ASSUMED: "Assumed", RENEWAL_ACTION: "Renewal action"}

# Gap texts, in the order they are listed.
NO_DEADLINE = "No deadline"
NO_RENEWAL_DATE = "No renewal date"
ASSUMED_DEADLINE = "Assumed deadline"
UNKNOWN_AUTO_RENEWAL = "Auto-renewal unknown"
NO_APPLICATIONS = "No applications"
ALL_RETIRED = "All covered applications retired: confirm cancellation"
NO_RESPONSIBLE = "No responsible person"
INACTIVE_RESPONSIBLE = "Inactive responsible person"
NO_FOLLOWUP = "No IT follow-up"
DECISION_WITHOUT_COMPLETION = "Decision without completion"
NEXT_CYCLE_MISSING = "Next cycle not established"


@dataclass
class Deadline:
    date: date | None
    label: str | None  # entered | calculated | assumed | renewal_action, or None when there is no deadline
    explanation: str


def action_deadline(auto_renew, renewal_date, notice_value, notice_unit, explicit_deadline, lead_days) -> Deadline:
    cancelling = auto_renew != "no"
    if explicit_deadline is not None:
        what = "Cancellation deadline" if cancelling else "Action deadline"
        return Deadline(explicit_deadline, ENTERED, f"{what} entered: {explicit_deadline.isoformat()}.")
    if renewal_date is None:
        return Deadline(
            None,
            None,
            "No action deadline can be worked out. Add a renewal date or the "
            + ("cancellation deadline." if cancelling else "action deadline."),
        )
    if cancelling and notice_value is not None:
        day = dates.shift(renewal_date, -notice_value, notice_unit)
        if day is None:
            return _out_of_range(renewal_date, f"notice period {dates.describe(notice_value, notice_unit)}")
        return Deadline(
            day,
            CALCULATED,
            f"Renewal date {renewal_date.isoformat()} minus notice period {dates.describe(notice_value, notice_unit)}"
            f" = {day.isoformat()}.",
        )
    day = dates.shift(renewal_date, -lead_days, "days")
    if day is None:
        return _out_of_range(renewal_date, f"the {lead_days}-day lead time")
    if cancelling:
        return Deadline(
            day,
            ASSUMED,
            f"Renewal date {renewal_date.isoformat()} minus the {lead_days}-day default lead time = {day.isoformat()}."
            " Add the notice period or cancellation deadline to replace it.",
        )
    return Deadline(
        day,
        RENEWAL_ACTION,
        f"Renewal date {renewal_date.isoformat()} minus the {lead_days}-day lead time = {day.isoformat()}."
        " Renew before it expires.",
    )


def _out_of_range(renewal_date: date, minus: str) -> Deadline:
    """No deadline: the renewal date minus the period is before the first representable date. Such a date is
    refused on entry (core/dates.parse_stored), so only a row stored before that, or by hand, gets here."""
    return Deadline(
        None,
        None,
        f"No action deadline can be worked out: renewal date {renewal_date.isoformat()} minus {minus} is out of range."
        f" Correct the renewal date (between {dates.EARLIEST.isoformat()} and {dates.LATEST.isoformat()}).",
    )


def timing(deadline: date | None, today: date) -> dict | None:
    """Days until the deadline (negative when overdue) and the words for it."""
    if deadline is None:
        return None
    days = (deadline - today).days
    if days < 0:
        text = f"Overdue by {-days} day{'' if days == -1 else 's'}"
    elif days == 0:
        text = "Due today"
    else:
        text = f"in {days} day{'' if days == 1 else 's'}"
    return {"days": days, "overdue": dates.is_overdue(deadline, today), "text": text}


def cycle_gaps(*, auto_renew, cycle, deadline: Deadline, applications, responsible, followup_id, today) -> list[str]:
    """The gaps of an obligation, judged on its latest cycle. `applications` are the covered Resources (lifecycle
    loaded), `responsible` the Person or None. A completed latest cycle means the next one hasn't been established."""
    gaps = []
    if cycle.completed_on is not None:
        gaps.append(NEXT_CYCLE_MISSING)
    else:
        if deadline.date is None:
            gaps.append(NO_DEADLINE)
        elif cycle.renewal_date is None:
            gaps.append(NO_RENEWAL_DATE)
        if deadline.label == ASSUMED:
            gaps.append(ASSUMED_DEADLINE)
        if cycle.decision and deadline.date is not None and dates.is_overdue(deadline.date, today):
            gaps.append(DECISION_WITHOUT_COMPLETION)
    if auto_renew == "unknown":
        gaps.append(UNKNOWN_AUTO_RENEWAL)
    if not applications:
        gaps.append(NO_APPLICATIONS)
    elif all(app.lifecycle == "retired" for app in applications):
        gaps.append(ALL_RETIRED)
    if responsible is None:
        gaps.append(NO_RESPONSIBLE)
    elif not responsible.active:
        gaps.append(INACTIVE_RESPONSIBLE)
    if followup_id is None:
        gaps.append(NO_FOLLOWUP)
    return gaps


def next_renewal_date(cycle) -> date | None:
    """The default renewal date of the next cycle: the previous one plus the term, clamped. None when it would
    fall outside the dates the API stores (core/dates.LATEST), so a default is never a date an Editor couldn't enter."""
    if cycle.renewal_date is None or cycle.term_value is None:
        return None
    day = dates.shift(cycle.renewal_date, cycle.term_value, cycle.term_unit)
    return day if day is not None and dates.in_window(day) else None


# ---- Reminders -------------------------------------------------------------------------------

OVERDUE = -1  # the reminder bucket of an overdue action


def bucket(deadline: date | None, today: date, thresholds) -> int | None:
    """The reminder threshold an action is in today: the smallest threshold not below the days left, OVERDUE after
    the deadline, or None while it is further away than every threshold (or has no deadline)."""
    if deadline is None:
        return None
    days = (deadline - today).days
    if days < 0:
        return OVERDUE
    reached = [t for t in thresholds if days <= t]
    return min(reached) if reached else None


def due_threshold(deadline: date | None, today: date, thresholds, set_on: date | None, dispatched) -> int | None:
    """The threshold that fires today, or None. Only the current one ever fires, so a passed threshold is never
    sent late in a burst; one that had already passed on the day the deadline was set never fires; and a
    threshold fires once per cycle (`dispatched` holds the ones already sent). Overdue actions aren't a threshold:
    the digest lists them every day."""
    t = bucket(deadline, today, thresholds)
    if t is None or t == OVERDUE or t in dispatched:
        return None
    assert deadline is not None
    fires_on = dates.shift(deadline, -t, "days")
    if set_on is not None and fires_on is not None and fires_on < set_on:
        return None
    return t
