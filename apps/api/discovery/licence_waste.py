"""Licence waste: paid licences assigned to people who haven't signed in for N
days, or to disabled accounts, and the money tied up in them against the licence pool's contract.

Where the numbers come from:

- Who holds which licence and when they last signed in: the organization's `entra_licence_usage` connectors
  (discovery/entra_licence_usage.py, LicensedUser rows), enabled connectors only.
- Seats purchased and assigned in the tenant (subscribedSkus): the live licence observations of enabled
  `entra_licenses` connectors, when there are any (null otherwise).
- The application, the licence pool and the contract: the pools bound to the SKU (their `sku_id`, the
  `entra_licenses` linkage, case-insensitive).

Each licensed member falls in exactly one of:

- `disabled`: the account is disabled but still holds the licence.
- `unknown`: the run couldn't read the last sign-in (no Entra ID P1, no AuditLog.Read.All, or turned off).
- `active`: a successful sign-in on or after the cutoff (today, organization time, minus N days).
- `new`: never signed in, created on or after the cutoff: a new starter, not waste.
- `inactive`: the rest: last successful sign-in before the cutoff, or none and created before it (or creation not
  known). `never_signed_in` counts those of them with no sign-in recorded at all (not even a failed one).

`reclaimable` is inactive plus disabled. Guests are counted apart (`guests`, `guests_inactive`) and service accounts
(the connector's scope names them) only as a count; neither is in `reclaimable` or the money.

The money (annual, in the contract's currency, never converted): the SKU's price comes from the live contracts (active
or pending renewal, not archived, with an amount) of the pools bound to it, when each such contract covers only pools
of this SKU: the contracts' annualised cost over the seats purchased in their pools. `annual_waste` is that cost times
the reclaimable seats over those seats, to the cent (half up); `annual_unassigned` the same for seats purchased in the
tenant and assigned to nobody. When no price can be told, the row carries a named gap instead of a zero
(`no_pool`, `no_contract`, `contract_not_live`, `no_cost`, `contract_shared`, `no_seats`, `mixed_currencies`,
`contracts_off`). Totals are per currency. The summary's seat counts (`reclaimable` among them) cover every row,
priced or not, free SKUs included; only `totals` is money, and `unpriced` counts what has no price.

`assignedLicenses` includes licences inherited from groups; such a seat is reclaimed by changing the user's group
membership, not by removing the licence. The drill-down flags them (`group_assigned`), and `group_reclaimable` counts
the reclaimable seats held only that way.

On a contract's page the rows are the SKUs' whole rows, plus the contract's share of the money, weighted by its own
annualised cost: its cost × the reclaimable seats ÷ the seats of every contract pricing the SKU (`contract_share`,
`contract_totals`).

N is the organization setting `inactive_seat_days` (default 90, 7 to 730; Admin, PATCH /organization), or
the report's `days` (the same range). Nothing here writes; the counts are one aggregate query over the stored rows, so the report's
queries don't grow with users or SKUs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from django.db import connection
from django.db.models import BooleanField, Case, CharField, ExpressionWrapper, F, Max, Q, Value, When
from django.db.models.functions import Lower
from rest_framework.exceptions import NotFound

from core.capabilities import CONTRACTS, has_capability
from core.dates import org_today
from money.models import LIVE_STATUSES, LicensePool

from .entra_licence_usage import UNKNOWN
from .models import LicensedUser, Observation, ObservationKind

SETTING = "inactive_seat_days"  # Organization.settings key (audited: snapshots.ORG_SETTINGS_AUDITED)
DEFAULT_DAYS = 90
DAYS_MIN, DAYS_MAX = 7, 730
DRILL_MAX = 5000  # users listed by a drill-down; `truncated` says when more matched
CENT = Decimal("0.01")
COUNT_FIELDS = (
    "assigned",
    "active",
    "new",
    "inactive",
    "never_signed_in",
    "disabled",
    "unknown",
    "reclaimable",
    "guests",
    "guests_inactive",
    "service_accounts",
    "group_reclaimable",
)
GAPS: dict[str, str] = {
    "contracts_off": "Needs the contracts capability to show a cost.",
    "no_pool": "No license pool is bound to this SKU: bind one, on a contract, to see its cost.",
    "no_contract": "Its license pool isn't on a contract, so its price isn't known.",
    "contract_not_live": "Its contract isn't active (archived, not started, ended or canceled).",
    "no_cost": "Its contract has no amount, so its price isn't known.",
    "contract_shared": "Its contract also covers other licenses, so the price of one seat isn't known.",
    "no_seats": "Its license pools record no purchased seats, so the price of one seat isn't known.",
    "mixed_currencies": "Its contracts are in different currencies, and Softwarden never converts.",
}
NO_SKU_ID = {"code": "no_sku_id", "label": "This pool has no SKU ID: bind it to an Entra SKU to see its waste."}
NO_CONNECTOR = (
    "No license usage connector has run yet: add a Microsoft Entra license usage connector (User.Read.All, and "
    "AuditLog.Read.All with Entra ID P1 for the last sign-in) to count active and inactive licenses."
)
STATUSES = ("active", "new", "inactive", "disabled", "unknown", "service_account")
USER_TYPES = ("member", "guest", "all")


# ---- the organization's setting --------------------------------------------------------------------------------------


def inactive_days(org) -> int:
    value = (org.settings or {}).get(SETTING)
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_DAYS


def clean_days(data: dict[str, Any], errors: dict[str, list[str]]) -> int | None:
    """The setting from an Organization PATCH, or None when not sent (catalog/admin_services.update_organization)."""
    if SETTING not in data:
        return None
    value = data[SETTING]
    if isinstance(value, bool) or not isinstance(value, int) or not DAYS_MIN <= value <= DAYS_MAX:
        errors[SETTING] = [f"Enter a number of days from {DAYS_MIN} to {DAYS_MAX}."]
        return None
    return value


# ---- the counts --------------------------------------------------------------------------------------------------------

# One row per SKU of the organization's enabled licence usage connectors. %(cutoff)s is the first day that counts as
# active. A member is neither a guest nor a service account.
_MEMBER = "NOT u.guest AND NOT u.service_account"
_KNOWN = "u.account_enabled AND u.sign_in_known"
_INACTIVE = (
    "(u.last_sign_in_on < %(cutoff)s OR (u.last_sign_in_on IS NULL AND (u.created_on IS NULL OR u.created_on < "
    "%(cutoff)s)))"
)
COUNTS_SQL = f"""
SELECT s.sku,
    count(*) FILTER (WHERE {_MEMBER}) AS assigned,
    count(*) FILTER (WHERE {_MEMBER} AND {_KNOWN} AND u.last_sign_in_on >= %(cutoff)s) AS active,
    count(*) FILTER (WHERE {_MEMBER} AND {_KNOWN} AND u.last_sign_in_on IS NULL AND u.created_on >= %(cutoff)s) AS new,
    count(*) FILTER (WHERE {_MEMBER} AND {_KNOWN} AND {_INACTIVE}) AS inactive,
    count(*) FILTER (
        WHERE {_MEMBER} AND {_KNOWN} AND {_INACTIVE} AND u.last_sign_in_on IS NULL AND NOT u.attempted
    ) AS never_signed_in,
    count(*) FILTER (WHERE {_MEMBER} AND NOT u.account_enabled) AS disabled,
    count(*) FILTER (WHERE {_MEMBER} AND u.account_enabled AND NOT u.sign_in_known) AS unknown,
    count(*) FILTER (WHERE u.guest AND NOT u.service_account) AS guests,
    count(*) FILTER (
        WHERE u.guest AND NOT u.service_account AND (NOT u.account_enabled OR ({_KNOWN} AND {_INACTIVE}))
    ) AS guests_inactive,
    count(*) FILTER (WHERE u.service_account) AS service_accounts,
    count(*) FILTER (
        WHERE {_MEMBER} AND (NOT u.account_enabled OR ({_KNOWN} AND {_INACTIVE})) AND s.sku = ANY(u.group_sku_ids)
    ) AS group_reclaimable
FROM discovery_licenseduser u
JOIN catalog_connector c ON c.id = u.connector_id AND c.organization_id = u.organization_id
CROSS JOIN LATERAL unnest(u.sku_ids) AS s(sku)
WHERE u.organization_id = %(org)s AND c.enabled
GROUP BY s.sku
"""  # noqa: S608 (constant fragments, no input: the values are parameters)


def usage_counts(org, cutoff: date) -> dict[str, dict[str, int]]:
    """{sku: counts} from the stored rows, in one query."""
    with connection.cursor() as cursor:
        cursor.execute(COUNTS_SQL, {"org": org.pk, "cutoff": cutoff})
        names = [c[0] for c in cursor.description]
        found = {}
        for row in cursor.fetchall():
            values = dict(zip(names, row, strict=True))
            sku = values.pop("sku")
            values["reclaimable"] = values["inactive"] + values["disabled"]
            found[sku] = values
    return found


def observed(org) -> dict[str, dict[str, Any]]:
    """{sku: {part, purchased, assigned}} from the live licence observations of enabled connectors (summed over
    connectors that report the same SKU)."""
    found: dict[str, dict[str, Any]] = {}
    rows = Observation.objects.filter(
        organization=org, kind=ObservationKind.LICENSE, gone_at__isnull=True, connector__enabled=True
    ).values_list("external_id", "display_name", "data")
    for external_id, name, data in rows:
        data = data if isinstance(data, dict) else {}
        sku = str(data.get("sku_id") or external_id).strip().lower()
        entry = found.setdefault(sku, {"part": str(data.get("sku_part_number") or name), "purchased": 0, "assigned": 0})
        for key, source in (("purchased", "seats_purchased"), ("assigned", "seats_assigned")):
            value = data.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                entry[key] += value
    return found


def connectors(org) -> list[dict[str, Any]]:
    """The enabled licence usage connectors with rows: users stored, when last seen, whether sign-ins were read."""
    rows = (
        LicensedUser.objects.filter(organization=org, connector__enabled=True)
        .values("connector_id", "connector__name", "sign_in_known")
        .annotate(seen=Max("seen_at"))
        .order_by("connector__name", "connector_id")
    )
    out: dict[Any, dict[str, Any]] = {}
    for r in rows:
        entry = out.setdefault(
            r["connector_id"],
            {"id": str(r["connector_id"]), "name": r["connector__name"], "seen_at": r["seen"], "sign_in_known": True},
        )
        entry["sign_in_known"] = entry["sign_in_known"] and r["sign_in_known"]
        entry["seen_at"] = max(entry["seen_at"], r["seen"])
    return list(out.values())


# ---- the money --------------------------------------------------------------------------------------------------------


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def bound_pools(org) -> dict[str, list[LicensePool]]:
    """{sku: the pools bound to it}, with their application, its vendor, and their contract and its vendor."""
    pools = (
        LicensePool.objects.filter(organization=org)
        .exclude(sku_id="")
        .select_related("application__application__vendor", "contract__vendor")
        .order_by("sku_name", "id")
    )
    found: dict[str, list[LicensePool]] = {}
    for pool in pools:
        found.setdefault(pool.sku_id.strip().lower(), []).append(pool)
    return found


@dataclass
class ContractSeats:
    skus: set[str] = field(default_factory=set)  # the SKUs of every pool on the contract ("" for one with none)
    seats: dict[str, int] = field(default_factory=dict)  # seats purchased per SKU


def contract_seats(org, contract_ids: Iterable[Any]) -> dict[Any, ContractSeats]:
    """What every pool of these contracts says, so a contract that also covers other licences is told apart."""
    found: dict[Any, ContractSeats] = {}
    rows = (
        LicensePool.objects.filter(organization=org, contract_id__in=set(contract_ids))
        .annotate(sku=Lower("sku_id"))
        .values_list("contract_id", "sku", "seats_purchased")
    )
    for contract_id, sku, seats in rows:
        entry = found.setdefault(contract_id, ContractSeats())
        sku = (sku or "").strip()
        entry.skus.add(sku)
        entry.seats[sku] = entry.seats.get(sku, 0) + int(seats or 0)
    return found


GAP_ORDER = ("no_contract", "contract_not_live", "no_cost", "contract_shared", "no_seats")


def gap(code: str) -> dict[str, str]:
    return {"code": code, "label": GAPS[code]}


def price(sku: str, pools: list[LicensePool], seats: dict[Any, ContractSeats], contracts_on: bool):
    """(price, gap): the SKU's price from its pools' contracts, or the named reason there is none."""
    if not contracts_on:
        return None, gap("contracts_off")
    if not pools:
        return None, gap("no_pool")
    reasons: set[str] = set()
    priced: dict[Any, tuple[Any, int]] = {}
    for pool in pools:
        c = pool.contract
        if c is None:
            reasons.add("no_contract")
        elif c.archived_at is not None or c.status not in LIVE_STATUSES:
            reasons.add("contract_not_live")
        elif c.annualized_cost is None:
            reasons.add("no_cost")
        elif seats[c.id].skus != {sku}:
            reasons.add("contract_shared")
        elif seats[c.id].seats.get(sku, 0) <= 0:
            reasons.add("no_seats")
        else:
            priced[c.id] = (c, seats[c.id].seats[sku])
    if not priced:
        return None, gap(next(code for code in GAP_ORDER if code in reasons))
    contracts = sorted((c for c, _s in priced.values()), key=lambda c: (c.name.lower(), str(c.id)))
    if len({c.currency for c in contracts}) > 1:
        return None, gap("mixed_currencies")
    cost = sum((Decimal(c.annualized_cost) for c in contracts), Decimal(0))
    total = sum(s for _c, s in priced.values())
    return {
        "currency": contracts[0].currency,
        "annual_cost": cost,
        "seats": total,
        "annual_unit_price": _q(cost / total),
        "contracts": [{"id": str(c.id), "name": c.name} for c in contracts],
        "some_pools_unpriced": bool(reasons),
    }, None


def money(found: dict[str, Any] | None, seats: int | None) -> Decimal | None:
    """The annual cost of `seats` at the SKU's price, to the cent (the contracts' cost over their seats, unrounded)."""
    if found is None or seats is None:
        return None
    return _q(found["annual_cost"] * seats / found["seats"])


# ---- the report -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    days: int
    skus: tuple[str, ...] = ()  # SKU IDs or part numbers, lower-cased
    application: UUID | None = None
    vendor: UUID | None = None
    min_inactive: int | None = None


def _brief(obj) -> dict[str, str]:
    return {"id": str(obj.pk), "name": obj.name}


def _row(sku, counts, seen, pools, seats, contracts_on) -> dict[str, Any]:
    part = (seen or {}).get("part") or next((p.sku_name for p in pools), "") or sku
    purchased: int | None = seen["purchased"] if seen else None
    assigned_seats: int | None = seen["assigned"] if seen else None
    unassigned = max(seen["purchased"] - seen["assigned"], 0) if seen else None
    found, missing = price(sku, pools, seats, contracts_on)
    applications = {p.application_id: p.application for p in pools}
    vendors: dict[Any, Any] = {}
    for p in pools:
        app_vendor = getattr(getattr(p.application, "application", None), "vendor", None)
        for v in (p.contract.vendor if p.contract else None, app_vendor):
            if v is not None:
                vendors[v.pk] = v
    return {
        "sku_id": sku,
        "sku_part_number": part,
        "seats_purchased": purchased,
        "seats_assigned": assigned_seats,
        "seats_unassigned": unassigned,
        **{name: counts.get(name, 0) for name in COUNT_FIELDS},
        "applications": [_brief(a) for a in sorted(applications.values(), key=lambda a: (a.name.lower(), str(a.pk)))],
        "vendors": [_brief(v) for v in sorted(vendors.values(), key=lambda v: (v.name.lower(), str(v.pk)))],
        "pools": [
            {
                "id": str(p.pk),
                "name": p.sku_name,
                "application": _brief(p.application),
                "contract": _brief(p.contract) if p.contract else None,
            }
            for p in pools
        ],
        "price": found,
        "gap": missing,
        "annual_waste": money(found, counts.get("reclaimable", 0)),
        "annual_unassigned": money(found, unassigned),
        "_application_ids": set(applications),
        "_vendor_ids": set(vendors),
        # Each contract's seats and annualised cost (contract_share weights a contract's part of the money by its cost).
        "_contract_seats": {p.contract_id: seats[p.contract_id].seats.get(sku, 0) for p in pools if p.contract_id},
        "_contract_costs": {p.contract_id: p.contract.annualized_cost for p in pools if p.contract_id},
    }


def _matches(row: dict[str, Any], filters: Filters) -> bool:
    if filters.skus and not ({row["sku_id"], row["sku_part_number"].lower()} & set(filters.skus)):
        return False
    if filters.application is not None and filters.application not in row["_application_ids"]:
        return False
    if filters.vendor is not None and filters.vendor not in row["_vendor_ids"]:
        return False
    return filters.min_inactive is None or row["inactive"] >= filters.min_inactive


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["price"] is None:
            continue
        t = totals.setdefault(
            r["price"]["currency"],
            {"skus": 0, "reclaimable": 0, "annual_waste": Decimal(0), "annual_unassigned": Decimal(0)},
        )
        t["skus"] += 1
        t["reclaimable"] += r["reclaimable"]
        t["annual_waste"] += r["annual_waste"]
        t["annual_unassigned"] += r["annual_unassigned"] or Decimal(0)
    unpriced = [r for r in rows if r["price"] is None]
    return {
        "skus": len(rows),
        **{name: sum(r[name] for r in rows) for name in COUNT_FIELDS},
        "totals": [{"currency": cur, **totals[cur]} for cur in sorted(totals)],
        "unpriced": {"skus": len(unpriced), "reclaimable": sum(r["reclaimable"] for r in unpriced)},
    }


def report(org, filters: Filters, today: date | None = None) -> dict[str, Any]:
    """The licence waste report: one row per SKU (assigned to a licensed user, or reported by a licence observation),
    most reclaimable first, with a summary of the filtered rows."""
    today = today or org_today(org)
    cutoff = today - timedelta(days=filters.days)
    counts = usage_counts(org, cutoff)
    seen = observed(org)
    pools = bound_pools(org)
    seats = contract_seats(org, {p.contract_id for group in pools.values() for p in group if p.contract_id})
    contracts_on = has_capability(org, CONTRACTS)
    rows = [
        _row(sku, counts.get(sku, {}), seen.get(sku), pools.get(sku, []), seats, contracts_on)
        for sku in sorted(set(counts) | set(seen))
    ]
    rows = [r for r in rows if _matches(r, filters)]
    rows.sort(key=lambda r: (-r["reclaimable"], -r["assigned"], r["sku_part_number"].lower(), r["sku_id"]))
    sources = connectors(org)
    total = summary(rows)
    notes = []
    if not sources:
        notes.append(NO_CONNECTOR)
    if total["unknown"]:
        notes.append(
            f"The last sign-in of {total['unknown']} licensed member(s) is {UNKNOWN}; they aren't counted as inactive."
        )
    known = {s["sign_in_known"] for s in sources}
    if not known:
        activity = "none"
    elif len(known) > 1:
        activity = "partial"
    else:
        activity = "known" if True in known else "unknown"
    return {
        "days": filters.days,
        "cutoff": cutoff,
        "as_of": max((s["seen_at"] for s in sources), default=None),
        "sign_in_activity": activity,
        "connectors": sources,
        "notes": notes,
        "summary": total,
        "rows": rows,
    }


def public(row: dict[str, Any]) -> dict[str, Any]:
    """A report row without its internal keys."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def pool_waste(org, pool: LicensePool, days: int) -> dict[str, Any]:
    """The waste of the SKU a pool is bound to (GET /license-pools/{id}/licence-waste)."""
    sku = pool.sku_id.strip().lower()
    out: dict[str, Any] = {"pool": {"id": str(pool.pk), "name": pool.sku_name}, "sku_id": sku or None, "days": days}
    if not sku:
        return {**out, "row": None, "gap": NO_SKU_ID}
    rows = report(org, Filters(days=days, skus=(sku,)))["rows"]
    return {**out, "row": public(rows[0]) if rows else None, "gap": None}


def contract_share(row: dict[str, Any], contract) -> dict[str, Any] | None:
    """This contract's share of a SKU row's money, or None when the contract doesn't price the SKU (a gap, or not
    live). Several live contracts of one currency can price a SKU together; the row's money is their cost together
    over their seats together. Each contract's part is weighted by its own annualised cost: its cost × the reclaimable
    (or unassigned) seats ÷ every pricing contract's seats, from unrounded values, rounded once. So a contract bought
    dearer carries more of the waste, and the unrounded parts add up to the row."""
    found = row["price"]
    if found is None or not any(c["id"] == str(contract.pk) for c in found["contracts"]):
        return None
    cost = Decimal(row["_contract_costs"][contract.pk])
    unassigned = row["seats_unassigned"]
    return {
        "seats": row["_contract_seats"].get(contract.pk, 0),
        "of_seats": found["seats"],
        "annual_cost": cost,
        "annual_waste": _q(cost * row["reclaimable"] / found["seats"]),
        "annual_unassigned": None if unassigned is None else _q(cost * unassigned / found["seats"]),
    }


def contract_waste(org, contract, days: int) -> dict[str, Any]:
    """The waste of the SKUs a contract's pools are bound to (GET /contracts/{id}/licence-waste): each SKU's whole row,
    and this contract's share of its money (contract_share: weighted by the contract's own cost), with the contract's
    totals from those shares."""
    skus = sorted(
        {
            s.strip().lower()
            for s in LicensePool.objects.filter(organization=org, contract=contract).values_list("sku_id", flat=True)
            if s.strip()
        }
    )
    rows = report(org, Filters(days=days, skus=tuple(skus)))["rows"] if skus else []
    shares = [contract_share(r, contract) for r in rows]
    priced = [s for s in shares if s is not None]
    return {
        "contract": {"id": str(contract.pk), "name": contract.name, "currency": contract.currency},
        "days": days,
        "contract_totals": {
            "currency": contract.currency,
            "skus": len(priced),
            "annual_waste": sum((s["annual_waste"] for s in priced), Decimal(0)),
            "annual_unassigned": sum((s["annual_unassigned"] or Decimal(0) for s in priced), Decimal(0)),
        },
        "summary": summary(rows),
        "rows": [{**public(r), "contract_share": share} for r, share in zip(rows, shares, strict=True)],
    }


# ---- the drill-down -----------------------------------------------------------------------------------------------


def status_expression(cutoff: date):
    """The user's category in SQL, as the aggregate counts them (service accounts first, then disabled, unknown, ...)."""
    return Case(
        When(service_account=True, then=Value("service_account")),
        When(account_enabled=False, then=Value("disabled")),
        When(sign_in_known=False, then=Value("unknown")),
        When(last_sign_in_on__gte=cutoff, then=Value("active")),
        When(last_sign_in_on__isnull=True, created_on__gte=cutoff, then=Value("new")),
        default=Value("inactive"),
        output_field=CharField(),
    )


def last_sign_in_text(u: LicensedUser) -> str:
    if not u.sign_in_known:
        return UNKNOWN
    if u.last_sign_in_on is not None:
        return u.last_sign_in_on.isoformat()
    return "no successful sign-in recorded" if u.attempted else "never signed in"


def ref(u: LicensedUser) -> str:
    """The short hash an unlinked user is shown by: the first eight characters of the row's keyed hash."""
    return u.user_key[:8]


def drill(org, sku: str, days: int, statuses: Iterable[str], user_type: str, today: date | None = None):
    """The licensed users of one SKU in the given categories, never-signed-in first, then the longest inactive. Only
    a linked Person's name is shown; other users are "Unlinked user" and a short hash. No object ID, name or email is
    stored, so none can be shown."""
    today = today or org_today(org)
    cutoff = today - timedelta(days=days)
    wanted = set(statuses)
    rows = (
        LicensedUser.objects.filter(organization=org, connector__enabled=True, sku_ids__contains=[sku])
        .annotate(
            status=status_expression(cutoff),
            never=ExpressionWrapper(
                Q(sign_in_known=True, last_sign_in_on__isnull=True, attempted=False), output_field=BooleanField()
            ),
        )
        .filter(status__in=sorted(wanted))
    )
    if user_type == "member":
        rows = rows.filter(guest=False)
    elif user_type == "guest":
        rows = rows.filter(guest=True)
    total = rows.count()
    page = rows.select_related("person", "connector").order_by(
        F("never").desc(),
        F("last_sign_in_on").asc(nulls_first=True),
        Lower("person__name").asc(nulls_last=True),
        "user_key",
    )[:DRILL_MAX]
    users = [
        {
            "name": u.person.name if u.person else "Unlinked user",
            "ref": ref(u),
            "person": _brief(u.person) if u.person else None,
            "status": u.status,
            "never_signed_in": u.never,
            "last_sign_in_on": u.last_sign_in_on,
            "last_sign_in": last_sign_in_text(u),
            "days_since": (today - u.last_sign_in_on).days if u.last_sign_in_on else None,
            "account_enabled": u.account_enabled,
            "guest": u.guest,
            "group_assigned": sku in (u.group_sku_ids or []),
            "connector": _brief(u.connector),
        }
        for u in page
    ]
    seen = observed(org).get(sku)
    pools = list(LicensePool.objects.filter(organization=org, sku_id__iexact=sku).order_by("sku_name"))
    if (
        seen is None
        and not pools
        and not LicensedUser.objects.filter(organization=org, sku_ids__contains=[sku]).exists()
    ):
        # A SKU no source reported, no pool is bound to and no licensed user holds: a 404, not an empty report, so a
        # mistyped ID is told apart from a SKU nobody wastes, and its CSV writes no export event.
        raise NotFound("No license source, pool or user knows this SKU.")
    part = (seen or {}).get("part") or next((p.sku_name for p in pools), sku)
    return {
        "sku_id": sku,
        "sku_part_number": part,
        "days": days,
        "cutoff": cutoff,
        "statuses": [s for s in STATUSES if s in wanted],
        "user_type": user_type,
        "total": total,
        "truncated": total > DRILL_MAX,
        "users": users,
    }
