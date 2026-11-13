"""Notification rules: turning live reminders into deliveries (on notify/deliveries.py).

**When.** For a live reminder a rule covers, on the organization's local `today`:

- before or on the due date (`days_left >= 0`), the rule's **current lead** is the smallest lead time at least
  `days_left`: with leads 90, 60, 30, 7 a reminder 25 days out is in the 30-day lead. Each lead sends once. So a lead
  day missed while the worker was down (or a reminder first seen inside a lead) sends that nearest lead once, late;
  the larger leads before it are not sent at all (the catch-up rule);
- after the due date (`overdue >= 1`), the **current escalation step** is the largest of `escalation_days` at most
  `overdue`, and each step sends once: steps 1, 7 mean the day after and a week after; a missed step catches up the
  same way. A rule with `repeat_every_days` N also has a step at every multiple of N (N, 2N, 3N, ...: notify 0013,
  the End of life built-in's 30), merged with its `escalation_days`, so a reminder still live keeps being announced
  every N days counted from its due date. A repeat is an escalation step in every respect (its own stage key `E60`,
  the escalation audience, the acknowledged rule below, the same catch-up: one missed mark sends once, the next
  follows on its date);
- snoozed (organization-local, through `snoozed_until`): nothing at all until the day after;
- acknowledged: no leads; escalation steps still go out unless the rule's `escalate_when_acknowledged` is off
  (acknowledging isn't doing the work);
- done or obsolete: nothing (they aren't live).

**To whom.** Email to the rule's audience, resolved now: the reminder's routed user (`reminders.routed_to`) when
`notify_routed`; when routing finds nobody who can be emailed (no one routed, not an active member, no address), the
rule's fallback person and team members, else the organization's IT coordinator; plus the rule's people, its team's
members and every active member with one of its roles. Escalations also reach the escalation people, team and roles.
A member who turned reminder emails off (NotificationPreference.reminders_enabled) isn't emailed (and isn't replaced
by the fallback). People are reached at their user account's address when they have one (so a person who is also
the routed user is emailed once), else at their own; inactive and anonymized people never. Teams and webhook
destinations of the rule get one message each; a destination that is off is left out.

**Once.** Every message is `deliveries.enqueue` under the dedupe key `{stage key}:{due date}`; the stage key names the
reminder and its notify cycle, the rule, the lead or step, the channel and the audience member (`u<user id>`,
`p<person id>`) or destination (`d<id>`): `r:<reminder>:<cycle>:<rule>:L30:email:u12:2026-12-06`. A stage already
had for a due date covers the current date while it is earlier, the same, or later by less than the stage's lead
(`Ledger.covers`): a deadline recomputed a few days either way, or every default-notice deadline moved by a changed
lead time, repeats nothing, while a contract extended by a year gets its leads again at the new date's marks. A rerun
sends nothing, a reopened reminder (new cycle) notifies again, and two rules covering a reminder each send their own.

**When the day's notices go.** Not before the organization's local send time: its digest time (the hour the digest
uses), at the latest 23:00 (`send_time`); the hourly run before it does nothing for that day. Every UTC offset is a
multiple of 15 minutes, so the hourly run at :25 lands once in every local hour (at :10 in a +5:45 zone such as
Kathmandu), and 23:00 is always reached on the day.

**No burst.** When a rule is created, turned on or changed, the messages the change adds for stages that began before
today are recorded as `skipped` deliveries with `baseline` true (`baseline`), so only stages that begin later send
to them; a stage beginning today, and one already owed before the change, still send. Built-ins seeded for a new
organization start baselined; those seeded by migration notify 0005 for existing organizations have `baselined_at`
null and are baselined by their first planner run instead of sending.

`decide()` is the one place that judges a reminder for a rule and a day; the planner (`run_for_org`, the hourly
`notify.rules` schedule) and the read-only `preview()` both call it, so the preview shows what the planner would do.
Query counts don't grow with the number of reminders (a fixed set of reads per organization) except for the
deliveries actually created."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, F, Q, Value
from django.db.models.functions import Cast, Concat, Replace

from . import deliveries, render
from .models import (
    LIVE_REMINDER_STATUSES,
    Channel,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    NotificationRule,
    NotificationRuleEscalationPerson,
    NotificationRulePerson,
    Reminder,
    ReminderSource,
    ReminderStatus,
    RuleKind,
)
from .reminders import path_of, routed_to, sync

log = logging.getLogger("softwarden.notify")

S = ReminderSource
RECORD_FILTER_TYPES = (S.PERIODIC_REVIEW, S.RECORD_REVIEW, S.CUSTOM_DATE, S.CREDENTIAL_EXPIRY, S.END_OF_LIFE)
PREVIEW_MAX_DAYS = 180
SCHEDULE_KEYS = ("deliveries", "failed")


# ---- when --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """A lead (`kind` reminder, `days` before the due date) or an escalation step (`kind` escalation, `days` after)."""

    kind: str
    days: int

    @property
    def tag(self) -> str:
        return f"{'L' if self.kind == DeliveryKind.REMINDER else 'E'}{self.days}"


@dataclass(frozen=True)
class Decision:
    stage: Stage | None
    reason: str = ""
    next_date: date | None = None


def current_lead(days_left: int, leads: Iterable[int]) -> int | None:
    found = [lead for lead in leads if lead >= days_left]
    return min(found) if found else None


def current_step(overdue: int, steps: Iterable[int], repeat: int | None = None) -> int | None:
    """The largest step at most `overdue`: one of `steps`, or a multiple of `repeat` (N, 2N, ...)."""
    found = [step for step in steps if step <= overdue]
    if repeat and overdue >= repeat:
        found.append(overdue // repeat * repeat)
    return max(found) if found else None


def next_step(overdue: int, steps: Iterable[int], repeat: int | None = None) -> int | None:
    """The smallest step after `overdue` (0 for the first), or None when there is none."""
    found = [step for step in steps if step > overdue]
    if repeat:
        found.append((max(overdue, 0) // repeat + 1) * repeat)
    return min(found) if found else None


def status_on(reminder: Reminder, day: date) -> str:
    """The reminder's status on `day`, assuming nobody acts before it: a snooze is quiet through its date."""
    if reminder.status == ReminderStatus.SNOOZED:
        if reminder.snoozed_until is not None and reminder.snoozed_until >= day:
            return ReminderStatus.SNOOZED
        return ReminderStatus.OPEN
    return reminder.status


def decide(rule: NotificationRule, reminder: Reminder, day: date) -> Decision:
    """Whether `rule` notifies about `reminder` on `day`, which lead or step, and if not why. `next_date` is the
    next day the answer can change if nothing else does (the next lead or step begins, a snooze ends), so the preview
    jumps from one to the next instead of walking every day. Filters are judged separately (`matches`)."""
    if rule.kind != RuleKind.REMINDERS or reminder.source_type not in rule.source_types:
        return Decision(None, "The rule doesn't cover this kind of reminder.")
    status = status_on(reminder, day)
    if status not in LIVE_REMINDER_STATUSES:
        return Decision(None, "Closed: no more notifications.")
    leads = sorted(set(rule.lead_times_days), reverse=True)
    steps = sorted(set(rule.escalation_days))
    repeat = rule.repeat_every_days or None
    has_steps = bool(steps or repeat)
    escalates = has_steps and (status != ReminderStatus.ACKNOWLEDGED or rule.escalate_when_acknowledged)
    first = next_step(0, steps, repeat) if escalates else None
    first_step = reminder.due_date + timedelta(days=first) if first is not None else None
    if status == ReminderStatus.SNOOZED:
        assert reminder.snoozed_until is not None
        return Decision(
            None, f"Snoozed through {reminder.snoozed_until.isoformat()}.", reminder.snoozed_until + timedelta(days=1)
        )
    days_left = (reminder.due_date - day).days
    if days_left >= 0:
        if status == ReminderStatus.ACKNOWLEDGED:
            return Decision(None, "Acknowledged: no advance notices.", first_step)
        lead = current_lead(days_left, leads)
        if lead is None:
            if not leads:
                return Decision(None, "The rule sends nothing before the due date.", first_step)
            return Decision(None, "Not in a lead time yet.", reminder.due_date - timedelta(days=leads[0]))
        smaller = [lead_ for lead_ in leads if lead_ < lead]
        following = reminder.due_date - timedelta(days=smaller[0]) if smaller else first_step
        return Decision(Stage(DeliveryKind.REMINDER, lead), next_date=following)
    overdue = -days_left
    if not escalates:
        if has_steps:
            return Decision(None, "Acknowledged: the rule doesn't escalate acknowledged reminders.")
        return Decision(None, "Overdue, and the rule sends nothing after the due date.")
    step = current_step(overdue, steps, repeat)
    if step is None:
        return Decision(None, "Overdue; the first escalation step hasn't come.", first_step)
    larger = next_step(overdue, steps, repeat)
    return Decision(
        Stage(DeliveryKind.ESCALATION, step),
        next_date=reminder.due_date + timedelta(days=larger) if larger is not None else None,
    )


# ---- which reminders a rule covers (filters) ---------------------------------------------------------------------


@dataclass
class Facts:
    """What the filters judge about one reminder: the records it concerns (their kinds and criticalities), the
    vendors, and an annual value (contracts and renewals)."""

    kinds: set[str] = field(default_factory=set)
    criticalities: set[str] = field(default_factory=set)
    vendors: set[uuid.UUID] = field(default_factory=set)
    annual_value: Decimal | None = None


def has_filters(rule: NotificationRule) -> bool:
    return bool(rule.criticalities or rule.record_kinds or rule.vendor_id or rule.min_annual_value is not None)


def matches(rule: NotificationRule, facts: Facts | None) -> bool:
    if not has_filters(rule):
        return True
    if facts is None:
        return False
    if rule.criticalities and not facts.criticalities & set(rule.criticalities):
        return False
    if rule.record_kinds and not facts.kinds & set(rule.record_kinds):
        return False
    if rule.vendor_id and rule.vendor_id not in facts.vendors:
        return False
    if rule.min_annual_value is not None and (facts.annual_value is None or facts.annual_value < rule.min_annual_value):
        return False
    return True


def filter_facts(org, rows: list[Reminder]) -> dict[uuid.UUID, Facts]:
    """{reminder id: Facts} in six queries whatever the number of reminders: a record's own kind, criticality and
    application vendor; a licence pool's application and its contract's vendor; a contract's vendor, annualised cost
    and the records on its lines; a renewal's vendor, annual cost (the new term's, else the old) and the
    applications it covers; a vendor's own custom dates and risk documents."""
    from catalog.models import Resource
    from money.models import Contract as ContractModel
    from money.models import ContractItem, LicensePool, RenewalCycle, RenewalCycleApplication

    facts = {r.id: Facts() for r in rows}
    records: dict[uuid.UUID, set[uuid.UUID]] = {r.id: set() for r in rows}  # reminder -> resource ids
    pools = {r.license_pool_id for r in rows if r.license_pool_id}
    contract_ids = {r.contract_id for r in rows if r.contract_id}
    cycles = {r.cycle_id for r in rows if r.cycle_id}
    pool_rows = {
        pid: (app, vendor)
        for pid, app, vendor in LicensePool.objects.filter(organization=org, id__in=pools).values_list(
            "id", "application_id", "contract__vendor_id"
        )
    }
    contract_rows = {
        cid: (vendor, cost)
        for cid, vendor, cost in ContractModel.objects.filter(organization=org, id__in=contract_ids).values_list(
            "id", "vendor_id", "annualized_cost"
        )
    }
    items: dict[uuid.UUID, set[uuid.UUID]] = {}
    for cid, rid in ContractItem.objects.filter(organization=org, contract_id__in=contract_ids).values_list(
        "contract_id", "resource_id"
    ):
        items.setdefault(cid, set()).add(rid)
    cycle_rows = {
        cid: (vendor, new or old)
        for cid, vendor, new, old in RenewalCycle.objects.filter(organization=org, id__in=cycles).values_list(
            "id", "obligation__vendor_id", "new_annualized_cost", "old_annualized_cost"
        )
    }
    coverage: dict[uuid.UUID, set[uuid.UUID]] = {}
    for cid, rid in RenewalCycleApplication.objects.filter(organization=org, cycle_id__in=cycles).values_list(
        "cycle_id", "application_id"
    ):
        coverage.setdefault(cid, set()).add(rid)
    for r in rows:
        f = facts[r.id]
        if r.resource_id:
            records[r.id].add(r.resource_id)
        if r.license_pool_id in pool_rows:
            app, vendor = pool_rows[r.license_pool_id]
            records[r.id].add(app)
            if vendor:
                f.vendors.add(vendor)
        if r.contract_id in contract_rows:
            vendor, cost = contract_rows[r.contract_id]
            records[r.id] |= items.get(r.contract_id, set())
            if vendor:
                f.vendors.add(vendor)
            f.annual_value = cost
        if r.cycle_id in cycle_rows:
            vendor, cost = cycle_rows[r.cycle_id]
            records[r.id] |= coverage.get(r.cycle_id, set())
            if vendor:
                f.vendors.add(vendor)
            f.annual_value = cost
        if r.source_type == S.CUSTOM_DATE and not (r.resource_id or r.contract_id or r.license_pool_id):
            f.vendors.add(r.source_id)  # a vendor's own custom date
        if r.source_type == S.VENDOR_RISK:
            f.vendors.add(r.source_id)  # a vendor's risk document
    wanted = set().union(*records.values()) if records else set()
    attrs = {
        rid: (kind, crit, vendor)
        for rid, kind, crit, vendor in Resource.objects.filter(organization=org, id__in=wanted).values_list(
            "id", "kind", "criticality", "application__vendor_id"
        )
    }
    for reminder_id, ids in records.items():
        f = facts[reminder_id]
        for rid in ids:
            if rid not in attrs:
                continue
            kind, crit, vendor = attrs[rid]
            f.kinds.add(kind)
            if crit:
                f.criticalities.add(crit)
            if vendor:
                f.vendors.add(vendor)
    return facts


# ---- to whom -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    key: str  # u<user id> or p<person hex>: the audience member in the dedupe key
    email: str
    name: str
    user_id: int | None = None
    person_id: uuid.UUID | None = None


@dataclass
class Member:
    user_id: int
    role: str
    email: str
    name: str
    reminders_enabled: bool


@dataclass
class PersonRow:
    id: uuid.UUID
    name: str
    email: str
    user_id: int | None
    reachable: bool


@dataclass
class Directory:
    """Everyone a rule could reach in one organization, read once per run."""

    coordinator_id: int | None
    members: dict[int, Member]
    persons: dict[uuid.UUID, PersonRow]
    team_members: dict[uuid.UUID, list[uuid.UUID]]
    people: dict[uuid.UUID, list[uuid.UUID]]  # rule -> person ids
    escalation_people: dict[uuid.UUID, list[uuid.UUID]]
    by_role: dict[str, list[int]] = field(default_factory=dict)  # role -> member user ids, scanned once
    opted_out_emails: set[str] = field(default_factory=set)  # lower-cased addresses of members who opted out


def directory(org, rules: list[NotificationRule]) -> Directory:
    from catalog.models import Person, TeamMember
    from core.models import Membership

    members = {}
    for uid, role, email, first, last, enabled in Membership.objects.filter(
        organization=org, user__is_active=True
    ).values_list(
        "user_id",
        "role",
        "user__email",
        "user__first_name",
        "user__last_name",
        "notification_preference__reminders_enabled",
    ):
        name = f"{first} {last}".strip() or email
        members[uid] = Member(uid, role, email or "", name, enabled is not False)
    rule_ids = [r.id for r in rules]
    people: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rid, pid in NotificationRulePerson.objects.filter(organization=org, rule_id__in=rule_ids).values_list(
        "rule_id", "person_id"
    ):
        people.setdefault(rid, []).append(pid)
    escalation: dict[uuid.UUID, list[uuid.UUID]] = {}
    for rid, pid in NotificationRuleEscalationPerson.objects.filter(organization=org, rule_id__in=rule_ids).values_list(
        "rule_id", "person_id"
    ):
        escalation.setdefault(rid, []).append(pid)
    teams = {t for r in rules for t in (r.team_id, r.escalation_team_id, r.fallback_team_id) if t}
    team_members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tid, pid in TeamMember.objects.filter(organization=org, team_id__in=teams).values_list("team_id", "person_id"):
        team_members.setdefault(tid, []).append(pid)
    wanted = {p for ids in (*people.values(), *escalation.values(), *team_members.values()) for p in ids}
    wanted |= {r.fallback_person_id for r in rules if r.fallback_person_id}
    persons = {
        pid: PersonRow(pid, name, email or "", uid, active and anonymized is None)
        for pid, name, email, uid, active, anonymized in Person.objects.filter(
            organization=org, id__in=wanted
        ).values_list("id", "name", "email", "user_id", "active", "anonymized_at")
    }
    by_role: dict[str, list[int]] = {}
    for member in members.values():
        by_role.setdefault(member.role, []).append(member.user_id)
    opted_out = {m.email.lower() for m in members.values() if m.email and not m.reminders_enabled}
    return Directory(org.coordinator_id, members, persons, team_members, people, escalation, by_role, opted_out)


@dataclass
class Audience:
    recipients: dict[str, Recipient] = field(default_factory=dict)
    opted_out: dict[str, str] = field(default_factory=dict)  # key -> name
    fallback: str = ""  # "", "rule" or "coordinator": who stood in for the routed owner
    addresses: set[str] = field(default_factory=set)  # lower-cased: one email per address, whoever it reaches

    def add(self, recipient: Recipient) -> None:
        address = recipient.email.lower()
        if recipient.key not in self.recipients and address not in self.addresses:
            self.recipients[recipient.key] = recipient
            self.addresses.add(address)


def _add_user(audience: Audience, d: Directory, user_id: int | None) -> bool:
    """True when the user is someone reminders reach (added, or opted out); False when they can't be emailed."""
    member = d.members.get(user_id) if user_id is not None else None
    if member is None or not member.email:
        return False
    key = f"u{member.user_id}"
    if not member.reminders_enabled:
        audience.opted_out[key] = member.name
        return True
    audience.add(Recipient(key, member.email, member.name, user_id=member.user_id))
    return True


def _add_person(audience: Audience, d: Directory, person_id: uuid.UUID | None) -> bool:
    person = d.persons.get(person_id) if person_id is not None else None
    if person is None or not person.reachable:
        return False
    if person.user_id is not None and _add_user(audience, d, person.user_id):
        return True
    if not person.email:
        return False
    key = f"p{person.id.hex}"
    if person.email.lower() in d.opted_out_emails:  # a member's address: their opt-out holds whoever names it
        audience.opted_out[key] = person.name
        return True
    audience.add(Recipient(key, person.email, person.name, person_id=person.id))
    return True


def _add_team(audience: Audience, d: Directory, team_id) -> bool:
    found = False
    for pid in d.team_members.get(team_id, []) if team_id else []:
        found = _add_person(audience, d, pid) or found
    return found


def _add_roles(audience: Audience, d: Directory, roles: Iterable[str]) -> None:
    for role in roles:
        for user_id in d.by_role.get(role, []):
            _add_user(audience, d, user_id)


def audience(rule: NotificationRule, d: Directory, routed: int | None, escalation: bool) -> Audience:
    found = Audience()
    if rule.notify_routed and not _add_user(found, d, routed):
        stood_in = _add_person(found, d, rule.fallback_person_id)
        stood_in = _add_team(found, d, rule.fallback_team_id) or stood_in
        if stood_in:
            found.fallback = "rule"
        elif _add_user(found, d, d.coordinator_id):
            found.fallback = "coordinator"
    for pid in d.people.get(rule.id, []):
        _add_person(found, d, pid)
    _add_team(found, d, rule.team_id)
    _add_roles(found, d, rule.roles)
    if escalation:
        for pid in d.escalation_people.get(rule.id, []):
            _add_person(found, d, pid)
        _add_team(found, d, rule.escalation_team_id)
        _add_roles(found, d, rule.escalation_roles)
    return found


# ---- reading what is in play -------------------------------------------------------------------------------------


def active_rules(org, rule_id=None) -> list[NotificationRule]:
    qs = NotificationRule.objects.filter(organization=org, kind=RuleKind.REMINDERS)
    if rule_id is not None:
        qs = qs.filter(id=rule_id)
    else:
        qs = qs.filter(enabled=True)
    return list(qs.prefetch_related("teams_destinations", "webhook_destinations").order_by("name", "id"))


def live_reminders(org, rules: list[NotificationRule], first: date, last: date) -> list[Reminder]:
    """Live reminders of the rules' types that any of them could notify about between `first` and `last`: due within
    the largest lead of `last`, and not overdue unless a rule covering its type escalates or repeats (an overdue
    renewal isn't read every hour because the End of life rule repeats)."""
    types = {t for r in rules for t in r.source_types}
    if not types:
        return []
    horizon = max((max(r.lead_times_days, default=0) for r in rules), default=0)
    qs = Reminder.objects.filter(
        organization=org,
        status__in=LIVE_REMINDER_STATUSES,
        source_type__in=types,
        due_date__lte=last + timedelta(days=horizon),
    )
    escalating = {t for r in rules if r.escalation_days or r.repeat_every_days for t in r.source_types}
    if escalating != types:
        qs = qs.filter(Q(due_date__gte=first) | Q(source_type__in=escalating))
    return list(
        qs.annotate(
            routed=routed_to(),
            resource_kind=F("resource__kind"),
            obligation_id=F("cycle__obligation_id"),
        ).order_by("due_date", "id")
    )


# ---- what is sent ------------------------------------------------------------------------------------------------


def _when(days: int) -> str:
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    if days > 0:
        return f"due in {days} days"
    return f"{-days} day{'' if days == -1 else 's'} overdue"


def link_of(reminder) -> str:
    path = path_of(
        reminder.source_type,
        reminder.source_id,
        resource_id=reminder.resource_id,
        resource_kind=getattr(reminder, "resource_kind", None),
        obligation_id=getattr(reminder, "obligation_id", None),
        contract_id=reminder.contract_id,
        license_pool_id=reminder.license_pool_id,
    )
    base = settings.SOFTWARDEN_BASE_URL.rstrip("/")
    return base + (path or "/")


@dataclass(frozen=True)
class Message:
    subject: str
    heading: str
    intro: str
    items: list[render.Item]
    link: str
    event: str
    data: dict[str, Any]

    def email(self, footer: str) -> dict[str, Any]:
        return render.email_content(self.heading, self.items, intro=self.intro, footer=footer, link=self.link)

    def card(self) -> dict[str, Any]:
        return render.card_content(self.heading, self.items, intro=self.intro, link=self.link)

    def webhook(self) -> dict[str, Any]:
        return render.webhook_content(self.event, self.data)


def message(rule: NotificationRule, reminder, stage: Stage, day: date) -> Message:
    days = (reminder.due_date - day).days
    when = _when(days)
    due = reminder.due_date.isoformat()
    from eol import reminders as eol_reminders

    overdue = stage.kind == DeliveryKind.ESCALATION
    undated = eol_reminders.is_undated(reminder.source_type, getattr(reminder, "source_field", "") or "")
    if undated:  # flag-only end of life: the date is when tracking began, not an end-of-life date (eol/reminders.py)
        since = eol_reminders.undated_text(reminder.due_date)
        subject = f"{reminder.label}: {reminder.title} (tracked since {due})"
        intro = (
            f"{reminder.title}: endoflife.date lists this release cycle as end of life without publishing a date. "
            f"{since}."
        )
        detail = since
    elif overdue:
        subject = f"Overdue: {reminder.label}: {reminder.title} ({when})"
        intro = f"{reminder.label} for {reminder.title} was due on {due} and is {when}."
        detail = f"{reminder.label} {due} ({when})"
    else:
        subject = f"{reminder.label}: {reminder.title} ({when}, {due})"
        intro = f"{reminder.label} for {reminder.title} is {when} ({due})."
        detail = f"{reminder.label} {due} ({when})"
    if reminder.status == ReminderStatus.ACKNOWLEDGED:
        intro += " It was acknowledged and is still open."
    link = link_of(reminder)
    item: render.Item = {"title": reminder.title, "detail": detail, "url": link}
    data = {
        "reminder": {
            "id": str(reminder.id),
            "source_type": reminder.source_type,
            "source_id": str(reminder.source_id),
            "title": reminder.title,
            "label": reminder.label,
            "due_date": due,
            "status": reminder.status,
            "url": link,
            **({"no_date_published": True} if undated else {}),  # due_date is when tracking began
        },
        "rule": {"id": str(rule.id), "name": rule.name},
        "lead_time_days": None if overdue else stage.days,
        "overdue_days": -days if overdue else None,
    }
    return Message(
        subject=subject[:300],
        heading=subject[:200],
        intro=intro,
        items=[item],
        link=link,
        event="reminder.overdue" if overdue else "reminder.due",
        data=data,
    )


def footer(rule: NotificationRule) -> str:
    return (
        f'Sent by the notification rule "{rule.name}". Snooze or acknowledge the reminder in Softwarden to quiet it; '
        "you can turn off reminder emails in your profile."
    )


@dataclass
class Planned:
    """One rule's notice about one reminder today: who gets it and where, with the stage key of each."""

    rule: NotificationRule
    reminder: Reminder
    stage: Stage
    audience: Audience
    teams: list[Any]
    webhooks: list[Any]

    def prefix(self) -> str:
        r = self.reminder
        return f"r:{r.id.hex}:{r.notify_cycle}:{self.rule.id.hex}:{self.stage.tag}"

    def keys(self) -> list[tuple[str, str, Any]]:
        """(stage key, channel, recipient or destination) for every message. A stage key names everything but the
        due date; the delivery's dedupe key is `{stage key}:{due date}` (`full`)."""
        found: list[tuple[str, str, Any]] = []
        if self.rule.send_email:
            found += [
                (f"{self.prefix()}:email:{rec.key}", Channel.EMAIL, rec) for rec in self.audience.recipients.values()
            ]
        found += [(f"{self.prefix()}:teams:d{d.id.hex}", Channel.TEAMS, d) for d in self.teams]
        found += [(f"{self.prefix()}:webhook:d{d.id.hex}", Channel.WEBHOOK, d) for d in self.webhooks]
        return found

    def full(self, base: str) -> str:
        return f"{base}:{self.reminder.due_date.isoformat()}"

    def began(self) -> date:
        """The day this stage began for the reminder: its due date less the lead, or plus the step."""
        if self.stage.kind == DeliveryKind.ESCALATION:
            return self.reminder.due_date + timedelta(days=self.stage.days)
        return self.reminder.due_date - timedelta(days=self.stage.days)


class Ledger:
    """What each stage key has already had (sent, queued, failed or baselined), with the due date it was for.

    A stage sent for a due date covers the reminder's current due date while that date is earlier, the same, or later
    by less than the stage's lead time (escalations: its step; at least one day): a deadline recomputed a few days
    either way, or every default-notice deadline moved by a changed lead time, repeats nothing, while a deadline moved
    later by at least a lead (an extended contract) makes that lead due again for the new date (`covers`)."""

    def __init__(self, rows: Iterable[tuple[str, date | None]] = ()):
        self.keys: set[str] = set()
        self.by_stage: dict[str, list[date]] = {}
        for key, due in rows:
            self.keys.add(key)
            if due is not None:
                self.by_stage.setdefault(key.rsplit(":", 1)[0], []).append(due)

    @classmethod
    def load(cls, org, rules: list[NotificationRule], reminders: Iterable[Reminder]) -> Ledger:
        """One query: the deliveries of these rules for these reminders, in each reminder's current notify cycle only
        (the key prefix `r:<reminder>:<cycle>:`), so a long-overdue reminder's earlier cycles aren't read on every run.
        An older cycle's keys never match a planned key anyway."""
        ids = {r.id for r in reminders}
        if not ids or not rules:
            return cls()
        prefix = Concat(
            Value("r:"),
            Replace(Cast("reminder_id", CharField()), Value("-"), Value("")),
            Value(":"),
            Cast("reminder__notify_cycle", CharField()),
            Value(":"),
            output_field=CharField(),
        )
        rows = (
            Delivery.objects.filter(organization=org, rule__in=[r.id for r in rules], reminder_id__in=ids)
            .alias(cycle_prefix=prefix)
            .filter(dedupe_key__startswith=F("cycle_prefix"))
            .values_list("dedupe_key", "due_date")
        )
        return cls(rows)

    def covers(self, base: str, due: date, stage: Stage) -> bool:
        if f"{base}:{due.isoformat()}" in self.keys:
            return True
        window = max(stage.days, 1)
        return any((due - old).days < window for old in self.by_stage.get(base, ()))

    def add(self, base: str, due: date) -> None:
        self.keys.add(f"{base}:{due.isoformat()}")
        self.by_stage.setdefault(base, []).append(due)

    def owed(self, planned: Planned) -> list[tuple[str, str, Any]]:
        due = planned.reminder.due_date
        return [(b, c, t) for b, c, t in planned.keys() if not self.covers(b, due, planned.stage)]


def _destinations(rule: NotificationRule) -> tuple[list[Any], list[Any]]:
    teams = [d for d in rule.teams_destinations.all() if d.enabled]
    hooks = [d for d in rule.webhook_destinations.all() if d.enabled]
    return sorted(teams, key=lambda d: d.name.lower()), sorted(hooks, key=lambda d: d.name.lower())


class Context:
    """What a run reads once for an organization: the rules, the reminders in play, their filter facts and the
    directory of people."""

    def __init__(self, org, rules: list[NotificationRule], first: date, last: date):
        self.org = org
        self.rules = rules
        self.reminders = live_reminders(org, rules, first, last)
        self.facts = filter_facts(org, self.reminders) if any(has_filters(r) for r in rules) else {}
        self.directory = directory(org, rules)
        self.destinations = {r.id: _destinations(r) for r in rules}
        self._audiences: dict[tuple[uuid.UUID, uuid.UUID, bool], Audience] = {}

    def covered(self, rule: NotificationRule) -> list[Reminder]:
        return [
            r
            for r in self.reminders
            if r.source_type in rule.source_types and matches(rule, self.facts.get(r.id) if self.facts else None)
        ]

    def plan(self, rule: NotificationRule, reminder: Reminder, stage: Stage) -> Planned:
        escalation = stage.kind == DeliveryKind.ESCALATION
        key = (rule.id, reminder.id, escalation)
        if key not in self._audiences:
            self._audiences[key] = audience(rule, self.directory, getattr(reminder, "routed", None), escalation)
        teams, hooks = self.destinations[rule.id]
        return Planned(rule, reminder, stage, self._audiences[key], teams, hooks)


def plan(org, today: date, rules: list[NotificationRule] | None = None) -> list[Planned]:
    """Every notice the organization's enabled rules owe today (sent or not; the ledger tells)."""
    rules = active_rules(org) if rules is None else rules
    ctx = Context(org, rules, today, today)
    found = []
    for rule in rules:
        for reminder in ctx.covered(rule):
            decision = decide(rule, reminder, today)
            if decision.stage is not None:
                found.append(ctx.plan(rule, reminder, decision.stage))
    return found


def planned_keys(org, rule: NotificationRule, today: date) -> set[str]:
    """The dedupe keys of every message the rule, as stored now, would owe today (sent or not)."""
    if rule.kind != RuleKind.REMINDERS:
        return set()
    return {p.full(base) for p in plan(org, today, [rule]) for base, _c, _t in p.keys()}


def _common(planned: Planned, subject: str) -> dict[str, Any]:
    escalation = planned.stage.kind == DeliveryKind.ESCALATION
    return {
        "subject": subject,
        "kind": planned.stage.kind,
        "reminder": planned.reminder,
        "rule": planned.rule,
        "rule_name": planned.rule.name,
        "lead_time_days": None if escalation else planned.stage.days,
        "overdue_step": planned.stage.days if escalation else None,
        "due_date": planned.reminder.due_date,
    }


def enqueue_planned(org, planned: Planned, today: date, ledger: Ledger) -> int:
    """Queue the messages of one notice that the ledger doesn't cover yet. Returns how many were created."""
    owed = ledger.owed(planned)
    if not owed:
        return 0
    msg = message(planned.rule, planned.reminder, planned.stage, today)
    rendered: dict[str, dict[str, Any]] = {}
    created = 0
    common = _common(planned, msg.subject)
    with transaction.atomic():
        for base, channel, target in owed:
            if channel not in rendered:
                rendered[channel] = (
                    msg.email(footer(planned.rule))
                    if channel == Channel.EMAIL
                    else msg.card()
                    if channel == Channel.TEAMS
                    else msg.webhook()
                )
            extra: dict[str, Any] = {}
            if channel == Channel.EMAIL:
                extra["recipient"] = target.email
            elif channel == Channel.TEAMS:
                extra["teams_destination"] = target
            else:
                extra["webhook_destination"] = target
            _delivery, made = deliveries.enqueue(
                org, channel=channel, dedupe_key=planned.full(base), content=rendered[channel], **common, **extra
            )
            created += int(made)
            ledger.add(base, planned.reminder.due_date)
    return created


BASELINE_NOTE = "Baseline: the rule started after this notice was due."


def baseline(
    org, rule: NotificationRule, today: date, now: datetime | None = None, before: set[str] | None = None
) -> int:
    """Record, without sending, the stages the rule's reminders were already inside before today: a `skipped`
    delivery (`baseline` true, BASELINE_NOTE) under the stage's key for each recipient and destination the ledger
    doesn't cover. A stage that begins today isn't baselined (it sends at the digest time). With `before` (the keys the
    rule owed today before a change), only messages the change adds are baselined: a stage already owed still sends.
    Called when a rule is created, turned on or changed, and by the planner for a rule never baselined (`baselined_at`
    null: the built-ins notify 0005 seeded for existing organizations). Returns how many messages were recorded."""
    from django.utils import timezone

    ctx = Context(org, [rule], today, today)
    covered = ctx.covered(rule)
    ledger = Ledger.load(org, [rule], covered)
    before = before or set()
    rows: list[Delivery] = []
    for reminder in covered:
        decision = decide(rule, reminder, today)
        if decision.stage is None:
            continue
        planned = ctx.plan(rule, reminder, decision.stage)
        if planned.began() >= today:
            continue
        owed = [(b, c, t) for b, c, t in ledger.owed(planned) if planned.full(b) not in before]
        if not owed:
            continue
        common = _common(planned, message(rule, reminder, decision.stage, today).subject)
        common["rule_name"] = rule.name[:200]
        for base, channel, target in owed:
            rows.append(
                Delivery(
                    organization=org,
                    channel=channel,
                    status=DeliveryStatus.SKIPPED,
                    dedupe_key=planned.full(base),
                    recipient=target.email if channel == Channel.EMAIL else "",
                    teams_destination=target if channel == Channel.TEAMS else None,
                    webhook_destination=target if channel == Channel.WEBHOOK else None,
                    destination_name=target.name if channel != Channel.EMAIL else "",
                    content={},
                    last_error=BASELINE_NOTE,
                    baseline=True,
                    **common,
                )
            )
    if rows:
        Delivery.objects.bulk_create(rows, ignore_conflicts=True)  # a key already there (sent or baselined) stays
    rule.baselined_at = now or timezone.now()
    NotificationRule.objects.filter(id=rule.id).update(baselined_at=rule.baselined_at)
    return len(rows)


# The latest send time every organization's hourly runs (rules at :25, saved view subscriptions at :26) reach on every
# local day: offsets are multiples of 15 minutes, so a run lands in the 23:00 hour everywhere, but as early as 23:10
# (+5:45, Kathmandu) or 23:11 for :26. A later digest time would skip a day.
LATEST_SEND_TIME = time(23, 0)


def send_time(org) -> time:
    """When the organization's notices go each local day: its digest time, but never after 23:00, so one-day stages
    (lead 0, a 1-day lead) aren't lost to a digest time the hourly run never reaches."""
    return min(org.digest_time, LATEST_SEND_TIME)


def run_for_org(org, now: datetime) -> int:
    """The organization's notices for its local today, once its local time has reached `send_time`; returns how many
    deliveries were queued (0 on a rerun, and before that time). A rule never baselined is baselined first and sends
    nothing on that run; its reminders are brought up to date (`reminders.sync` of its types) just before. If that
    fails, only that rule is skipped (still unbaselined, retried next run) and logged with its id."""
    local = now.astimezone(ZoneInfo(org.timezone))
    today = local.date()
    rules = active_rules(org)
    for rule in [r for r in rules if r.baselined_at is None]:
        rules.remove(rule)  # sends nothing on this run either way
        try:
            with transaction.atomic():
                # Its reminders first, so a source the hourly generate hasn't run for yet (a migration landing between
                # :20 and :25, a failed generate) is baselined too, not created after it and caught up as a burst.
                sync(org, today, rule.source_types)
                baseline(org, rule, today, now)
        except Exception:
            # A failing source skips only this rule (left unbaselined, so the next run tries again); the
            # organization's other rules still send. Ids only in the log.
            log.exception("notification rule baseline failed organization=%s rule=%s", org.id, rule.id)
    if local.time() < send_time(org):
        return 0
    planned = plan(org, today, rules)
    if not planned:
        return 0
    ledger = Ledger.load(org, rules, {p.reminder for p in planned})
    return sum(enqueue_planned(org, p, today, ledger) for p in planned)


def scheduled_run(now: datetime) -> dict[str, int]:
    """The `notify.rules` schedule, hourly after `reminders.generate`: every organization on its local date. One
    organization's failure doesn't stop the others (logged with its id only)."""
    from core.models import active_organizations

    total: Counter[str] = Counter()
    for org in active_organizations():  # a suspended organization is skipped
        try:
            total["deliveries"] += run_for_org(org, now)
        except Exception:
            total["failed"] += 1
            log.exception("notification rules failed organization=%s", org.id)
    return {f"rule_{k}": total[k] for k in SCHEDULE_KEYS}


# ---- preview -----------------------------------------------------------------------------------------------------


def preview(org, rule: NotificationRule, today: date, days: int) -> list[dict[str, Any]]:
    """What the rule would send from `today` for `days` days (today included) if nothing else changed: one entry per
    reminder and lead or step, on the first day it would go out, with only the recipients and destinations the ledger
    doesn't cover (none at all when it would reach nobody, e.g. everyone opted out). Reads only. A rule that is off or
    not yet baselined previews as if turned on today: stages that began before today are baselined."""
    if rule.kind != RuleKind.REMINDERS:
        return []
    last = today + timedelta(days=days - 1)
    ctx = Context(org, [rule], today, last)
    covered = ctx.covered(rule)
    ledger = Ledger.load(org, [rule], covered)
    unbaselined = not rule.enabled or rule.baselined_at is None
    shown: set[str] = set()  # "reaches nobody" entries already listed
    events: list[dict[str, Any]] = []
    for reminder in covered:
        day = today
        while day <= last:  # from one stage change to the next (decide's next_date), never day by day
            decision = decide(rule, reminder, day)
            if decision.stage is not None:
                planned = ctx.plan(rule, reminder, decision.stage)
                keys = planned.keys()
                if unbaselined and day == today and planned.began() < today:
                    for base, _c, _t in keys:
                        ledger.add(base, reminder.due_date)
                    shown.add(planned.prefix())
                new = ledger.owed(planned)
                if new:
                    for base, _c, _t in new:
                        ledger.add(base, reminder.due_date)
                    events.append(_event(planned, day, new))
                elif not keys and planned.prefix() not in shown:  # it would reach nobody: say so, once
                    shown.add(planned.prefix())
                    events.append(_event(planned, day, []))
            if decision.next_date is None or decision.next_date <= day:
                break
            day = decision.next_date
    events.sort(key=lambda e: (e["date"], e["reminder"]["due_date"], e["reminder"]["title"].lower()))
    return events


def _event(planned: Planned, day: date, new: list[tuple[str, str, Any]]) -> dict[str, Any]:
    r = planned.reminder
    escalation = planned.stage.kind == DeliveryKind.ESCALATION
    return {
        "date": day.isoformat(),
        "kind": planned.stage.kind,
        "lead_time_days": None if escalation else planned.stage.days,
        "overdue_step": planned.stage.days if escalation else None,
        "reminder": {
            "id": str(r.id),
            "source_type": r.source_type,
            "title": r.title,
            "label": r.label,
            "due_date": r.due_date.isoformat(),
            "status": r.status,
            "snoozed_until": r.snoozed_until.isoformat() if r.snoozed_until else None,
        },
        "recipients": [
            {"name": t.name, "email": t.email, "user": t.user_id, "person": str(t.person_id) if t.person_id else None}
            for _k, c, t in new
            if c == Channel.EMAIL
        ],
        "destinations": [{"channel": c, "id": str(t.id), "name": t.name} for _k, c, t in new if c != Channel.EMAIL],
        "opted_out": sorted(planned.audience.opted_out.values(), key=str.lower) if planned.rule.send_email else [],
        "fallback": planned.audience.fallback,
    }
