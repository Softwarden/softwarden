"""Contracts and licence pools as import kinds. imports/importer.py runs the run's life (upload,
mapping, decisions, preview, commit, purge) and hands these two kinds to `plan_run` and `write` here.

Columns: csvio.TEMPLATE["contract"] and ["license_pool"] plus one cf_<name> column per custom field that applies. A
blank cell leaves the stored value. The derived columns (annualized_cost, cancellation_deadline, last_synced)
are read back from an export and ignored: the services derive them on every write.

Matching. A row matches by Softwarden ID, then by the import source's external key, then (contracts) by contract
number among unarchived contracts, case-insensitive, or (pools) by application and SKU name. A contract row that
matches none of those but names an unarchived contract with the same name and no conflicting number asks the
uploader to create a new contract or match that one (terms of one agreement share a name, so a name never matches on
its own). Vendors are matched by name and otherwise created or left out by the uploader's choice, as on records;
the renewal owner is a Person by email (or name), the same way; renewals, records on lines, applications and contracts
must exist.

Lines. The `items` cell is "Name=40; Other": `=n` a fixed percentage, none a share of what the fixed ones
leave. A record is named by its name, by kind:name (application:, component:, agent:) when names repeat across
kinds, or by its Softwarden ID; the export writes the shortest of those that is unambiguous. Fixed shares over 100 %
are a row error; the replace goes through contracts.set_items, which never passes through an over-allocated state
(an 80/20 to 20/80 reshuffle applies).

Preview and commit. Planning reads only. The preview then tries every row that would change something through the
same services the commit uses (contracts.create_contract, update_contract, set_items, create_pool, update_pool), in
row order, inside a transaction it rolls back, so the service's own refusals (the one source of truth with a linked
renewal: takeover_error, an end date a linked renewal needs, linking a renewal another contract drives; the one
syncing pool per SKU; a SKU ID for seat sync) are row errors before anything is saved, and the row shows what the
write would compute: the annualised cost, the cancellation deadline with its label, and the allocation of the cost
over the lines. The commit plans again, refuses on any error, and writes for real; the services audit each change
(their events carry the import run) and the run's import.committed event counts them. `last_synced` is never written:
only discovery's sync sets it."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

from catalog import customfields
from catalog.models import Kind, Person, Resource, Vendor
from core import dates
from core.problems import Conflict, FieldErrors
from money import contracts
from money.models import (
    AutoRenew,
    BillingFrequency,
    Contract,
    ContractItem,
    ContractStatus,
    LicensePool,
    PeriodUnit,
    RenewalObligation,
)

from . import csvio, importer
from .models import CONTRACT, LICENSE_POOL, ExternalIdentifier, ImportRow, RowOutcome

LINE_SEPARATOR = csvio.TAG_SEPARATOR
# A preview rehearses each row's writes only for files this size or smaller (it runs in the request).
# Larger files get the planning checks and the services' checks at commit.
REHEARSE_MAX = 500
LARGE_FILE_NOTICE = (
    f"This file has more than {REHEARSE_MAX} rows, so the contract rules (allocations already on a contract, renewal "
    "links, seat sync per SKU) are checked when you commit, not in this preview, and the annualized cost and deadline "
    "of new rows aren't shown. If a row breaks one, the commit names it and nothing is imported."
)
# Kind prefixes in a line's record name; an earlier version's software:/agent:/solution: are read too.
PREFIXES = {
    "application": Kind.APPLICATION,
    "software": Kind.APPLICATION,
    "component": Kind.COMPONENT,
    "solution": Kind.COMPONENT,
    "agent": Kind.AI_AGENT,
    "ai_agent": Kind.AI_AGENT,
}
WRITE_PREFIX = {Kind.APPLICATION: "application", Kind.COMPONENT: "component", Kind.AI_AGENT: "agent"}
KIND_WORDS = {Kind.APPLICATION: "application", Kind.COMPONENT: "component", Kind.AI_AGENT: "AI agent"}
CHOICES: dict[str, Any] = {
    "billing_frequency": BillingFrequency,
    "status": ContractStatus,
    "auto_renew": AutoRenew,
    "notice_unit": PeriodUnit,
}
# Other words a cell may use for a choice.
ALIASES = {
    "billing_frequency": {
        "semi-annual": "semiannual",
        "semi annual": "semiannual",
        "semi_annual": "semiannual",
        "six-monthly": "semiannual",
        "yearly": "annual",
        "multi-year": "multi_year",
        "multiyear": "multi_year",
        "one-time": "one_time",
        "onetime": "one_time",
        "usage-based": "usage_based",
    },
    "auto_renew": {"true": "yes", "false": "no", "y": "yes", "n": "no"},
    "notice_unit": {"day": "days", "month": "months", "year": "years"},
    "status": {"pending": "pending_renewal"},
}
YES_NO = {"true": True, "yes": True, "y": True, "1": True, "false": False, "no": False, "n": False, "0": False}
# The column a service's field error is shown under.
ERROR_COLUMNS = {
    "renewal_owner": "renewal_owner_email",
    "renewal_obligation": "renewal",
    contracts.CREATE: "create_renewal_obligation",
}
CENT = Decimal("0.01")


def _uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text.strip())
    except ValueError:
        return None


def _money(value: Decimal | None) -> str:
    return "" if value is None else str(value.quantize(CENT))


def _iso(day: date | None) -> str:
    return day.isoformat() if day else ""


def _unsafe(text: str) -> bool:
    """A name that can't be written as itself in a cell of references: it holds a separator, looks like a
    Softwarden ID, or has spaces at its ends (cells are stripped)."""
    return not text or LINE_SEPARATOR in text or "=" in text or text != text.strip() or _uuid(text) is not None


def _prefixed(text: str) -> bool:
    prefix, sep, _rest = text.partition(":")
    return bool(sep) and prefix.strip().lower() in PREFIXES


# ---- references: written by the exports, read by the import ---------------------------------------------------------


class Records:
    """An organization's applications, components and AI agents, to name them on contract lines and pools."""

    def __init__(self, org, kinds: Iterable[str] = contracts.ITEM_KINDS):
        rows = Resource.objects.filter(organization=org, kind__in=list(kinds)).only(
            "id", "name", "kind", "organization_id", "archived_at", "updated_at"
        )
        self.by_id: dict[Any, Resource] = {}
        self.by_name: dict[str, list[Resource]] = defaultdict(list)
        self.kind_names: Counter[tuple[str, str]] = Counter()
        for r in rows:
            self.by_id[r.id] = r
            self.by_name[r.name.lower()].append(r)
            self.kind_names[(r.kind, r.name.lower())] += 1

    def ref(self, r: Resource) -> str:
        """The shortest unambiguous way to name `r`: its name, kind:name, or its Softwarden ID."""
        if _unsafe(r.name):
            return str(r.id)
        if len(self.by_name[r.name.lower()]) == 1 and not _prefixed(r.name):
            return r.name
        if self.kind_names[(r.kind, r.name.lower())] == 1:
            return f"{WRITE_PREFIX[Kind(r.kind)]}:{r.name}"
        return str(r.id)

    def find(self, text: str, kind: str | None = None) -> tuple[Resource | None, str | None]:
        """(record, None) or (None, why not). `kind` limits the search (a pool's application)."""
        what = KIND_WORDS[Kind(kind)] if kind else "application, component or AI agent"
        text = text.strip()
        rid = _uuid(text)
        if rid is not None:
            one = self.by_id.get(rid)
            if one is None or (kind and one.kind != kind):
                return None, f"No {what} has the Softwarden ID {text}."
            return one, None
        kinds = {kind} if kind else None
        prefix, sep, rest = text.partition(":")
        named = PREFIXES.get(prefix.strip().lower()) if sep else None
        if named is not None and not kind:
            kinds, text = {named}, rest.strip()
            what = KIND_WORDS[Kind(named)]
        elif named is not None and named == kind:
            text = rest.strip()  # application:X where only applications are looked for: what ref() writes
        found = [r for r in self.by_name.get(text.lower(), []) if kinds is None or r.kind in kinds]
        if len(found) == 1:
            return found[0], None
        if not found:
            return None, f'No {what} is named "{text}".'
        if kinds is None:
            return None, (
                f'{len(found)} records are named "{text}". Write it as application:{text}, component:{text} or '
                f"agent:{text}, or use its Softwarden ID."
            )
        return None, f'{len(found)} {what}s are named "{text}". Use its Softwarden ID.'


class Renewals:
    def __init__(self, org):
        rows = RenewalObligation.objects.filter(organization=org).only("id", "name", "archived_at")
        self.by_id = {o.id: o for o in rows}
        self.by_name: dict[str, list[RenewalObligation]] = defaultdict(list)
        for o in self.by_id.values():
            self.by_name[o.name.lower()].append(o)

    def ref(self, o: RenewalObligation | None) -> str:
        if o is None:
            return ""
        if o.name != o.name.strip() or _uuid(o.name) or len(self.by_name[o.name.lower()]) != 1:
            return str(o.id)
        return o.name

    def find(self, text: str) -> tuple[RenewalObligation | None, str | None]:
        rid = _uuid(text)
        if rid is not None:
            found = self.by_id.get(rid)
            return (found, None) if found else (None, f"No renewal has the Softwarden ID {text}.")
        named = self.by_name.get(text.lower(), [])
        if len(named) == 1:
            return named[0], None
        if not named:
            return None, f'No renewal is named "{text}". Add it under Renewals first, or leave it empty.'
        return None, f'{len(named)} renewals are named "{text}". Use its Softwarden ID.'


class Contracts:
    """Contracts by ID, number and name, for a pool's `contract` cell (a number is tried before a name)."""

    def __init__(self, org):
        rows = Contract.objects.filter(organization=org).only("id", "name", "contract_number", "archived_at")
        self.by_id = {c.id: c for c in rows}
        self.by_number: dict[str, list[Contract]] = defaultdict(list)
        self.by_name: dict[str, list[Contract]] = defaultdict(list)
        for c in self.by_id.values():
            if c.contract_number:
                self.by_number[c.contract_number.lower()].append(c)
            self.by_name[c.name.lower()].append(c)

    def ref(self, c: Contract | None) -> str:
        if c is None:
            return ""
        number = c.contract_number
        if number and number == number.strip() and not _uuid(number) and len(self.by_number[number.lower()]) == 1:
            return number
        name = c.name
        if (
            name == name.strip()
            and not _uuid(name)
            and len(self.by_name[name.lower()]) == 1
            and not self.by_number.get(name.lower())
        ):
            return name
        return str(c.id)

    def find(self, text: str) -> tuple[Contract | None, str | None]:
        cid = _uuid(text)
        if cid is not None:
            found = self.by_id.get(cid)
            return (found, None) if found else (None, f"No contract has the Softwarden ID {text}.")
        for index, what in ((self.by_number, "number"), (self.by_name, "name")):
            named = index.get(text.lower(), [])
            if len(named) > 1:
                live = [c for c in named if c.archived_at is None]
                named = live if len(live) == 1 else named
            if len(named) == 1:
                return named[0], None
            if named:
                return None, f'{len(named)} contracts have the {what} "{text}". Use its Softwarden ID.'
        return None, f'No contract has the number or name "{text}".'


def items_text(lines: Iterable[ContractItem], records: Records) -> str:
    """A contract's lines as "Name=40; Name", each record named unambiguously."""
    parts = []
    for line in lines:
        record = records.by_id.get(line.resource_id) or line.resource
        pct = "" if line.allocation_percent is None else f"={line.allocation_percent.normalize():f}"
        parts.append(f"{records.ref(record)}{pct}")
    return f"{LINE_SEPARATOR} ".join(parts)


def contract_values(
    c: Contract, lines: list[ContractItem], custom, records: Records, renewals: Renewals
) -> dict[str, str]:
    """A contract in the template's columns (the export's cells, and what an import row is compared with)."""
    owner = c.renewal_owner
    stored = c.custom_field_data or {}
    obligation = renewals.by_id.get(c.renewal_obligation_id) if c.renewal_obligation_id else None
    return {
        "softwarden_id": str(c.id),
        "external_key": "",
        "name": c.name,
        "vendor": c.vendor.name if c.vendor else "",
        "contract_number": c.contract_number,
        "po_number": c.po_number,
        "amount": _money(c.amount),
        "currency": c.currency,
        "billing_frequency": c.billing_frequency,
        "annualized_cost": _money(c.annualized_cost),
        "start_date": _iso(c.start_date),
        "end_date": _iso(c.end_date),
        "auto_renew": c.auto_renew,
        "notice_value": "" if c.notice_value is None else str(c.notice_value),
        "notice_unit": c.notice_unit,
        "cancellation_deadline": _iso(c.cancellation_deadline),
        "renewal_owner_email": owner.email if owner else "",
        "renewal_owner_name": owner.name if owner else "",
        "status": c.status,
        "cost_center": c.cost_center,
        "renewal": renewals.ref(obligation),
        "create_renewal_obligation": "",
        "items": items_text(lines, records),
        "notes": c.notes,
        **{customfields.column(cf): customfields.to_text(cf, stored.get(cf.name)) for cf in custom},
    }


def pool_values(pool: LicensePool, custom, applications: Records, contract_refs: Contracts) -> dict[str, str]:
    stored = pool.custom_field_data or {}
    app = applications.by_id.get(pool.application_id) or pool.application
    return {
        "softwarden_id": str(pool.id),
        "external_key": "",
        "application": applications.ref(app),
        "contract": contract_refs.ref(contract_refs.by_id.get(pool.contract_id) if pool.contract_id else None),
        "sku_name": pool.sku_name,
        "sku_id": pool.sku_id,
        "seats_purchased": str(pool.seats_purchased),
        "seats_assigned": str(pool.seats_assigned),
        "seat_sync": "true" if pool.seat_sync else "false",
        "last_synced": pool.last_synced.isoformat() if pool.last_synced else "",
        "notes": pool.notes,
        **{customfields.column(cf): customfields.to_text(cf, stored.get(cf.name)) for cf in custom},
    }


def export_contract_rows(org, rows: list[Contract], lines: dict[Any, list[ContractItem]], custom):
    records, renewals = Records(org), Renewals(org)
    return [contract_values(c, lines.get(c.id, []), custom, records, renewals) for c in rows]


def export_pool_rows(org, rows: list[LicensePool], custom):
    applications, contract_refs = Records(org, [Kind.APPLICATION]), Contracts(org)
    return [pool_values(p, custom, applications, contract_refs) for p in rows]


EXPORT_COLUMNS = {
    CONTRACT: [c for c in csvio.TEMPLATE[CONTRACT] if c not in ("external_key", "create_renewal_obligation")],
    LICENSE_POOL: [c for c in csvio.TEMPLATE[LICENSE_POOL] if c != "external_key"],
}


# ---- planning ---------------------------------------------------------------------------------------------------------


class _Context:
    def __init__(self, run):
        self.run = run
        self.org = org = run.organization
        self.kind = run.kind
        self.custom = customfields.for_kind(org, self.kind)
        self.columns = csvio.columns(self.kind, False, self.custom)
        self.people = {p.email.lower(): p for p in Person.objects.filter(organization=org).exclude(email="")}
        self.person_ids: set = set()
        self.vendors = {v.name.lower(): v for v in Vendor.objects.filter(organization=org)}
        self.lines: dict[Any, list[ContractItem]] = defaultdict(list)
        self.targets: dict[Any, Any]
        if self.kind == CONTRACT:
            self.records = Records(org)
            self.renewals = Renewals(org)
            self.targets = {
                c.id: c for c in Contract.objects.filter(organization=org).select_related("vendor", "renewal_owner")
            }
            items = ContractItem.objects.filter(organization=org).order_by("created_at", "id")
            for item in items:
                self.lines[item.contract_id].append(item)
            self.by_number: dict[str, list[Contract]] = defaultdict(list)
            self.by_name: dict[str, list[Contract]] = defaultdict(list)
            for c in sorted(self.targets.values(), key=lambda c: (c.created_at, str(c.id))):
                if c.contract_number:
                    self.by_number[c.contract_number.lower()].append(c)
                if c.archived_at is None:
                    self.by_name[c.name.lower()].append(c)
            field = "contract"
        else:
            self.records = Records(org, [Kind.APPLICATION])
            self.contract_refs = Contracts(org)
            self.targets = {p.id: p for p in LicensePool.objects.filter(organization=org).select_related("application")}
            self.natural: dict[tuple[Any, str], list[LicensePool]] = defaultdict(list)
            for pool in self.targets.values():
                self.natural[(pool.application_id, pool.sku_name.lower())].append(pool)
            field = "license_pool"
        self.key_to_target: dict[str, Any] = {}
        self.key_of_target: dict[Any, str] = {}
        if run.source_id:
            for ident in ExternalIdentifier.objects.filter(source_id=run.source_id, **{f"{field}__isnull": False}):
                target_id = getattr(ident, f"{field}_id")
                self.key_to_target[ident.external_key] = target_id
                self.key_of_target[target_id] = ident.external_key
        others = ExternalIdentifier.objects.filter(organization=org, **{f"{field}__isnull": False})
        if run.source_id:
            others = others.exclude(source_id=run.source_id)
        self.keyed_elsewhere = set(others.values_list(f"{field}_id", flat=True))

    @property
    def noun(self) -> str:
        return "contract" if self.kind == CONTRACT else "license pool"

    def current(self, target) -> dict[str, str] | None:
        if target is None:
            return None
        if self.kind == CONTRACT:
            return contract_values(target, self.lines.get(target.id, []), self.custom, self.records, self.renewals)
        return pool_values(target, self.custom, self.records, self.contract_refs)


def _choice(field: str, cell: str) -> str | None:
    lookup: dict[str, str] = {}
    for value, label in CHOICES[field].choices:
        lookup[value] = value
        lookup[str(label).lower()] = value
        lookup[value.replace("_", " ")] = value
    lookup.update(ALIASES.get(field, {}))
    return lookup.get(cell.strip().lower())


def _allowed(field: str) -> str:
    return ", ".join(CHOICES[field].values)


class _Row:
    """One row's planning: cells, the current values of what it matched, and helpers to record a change."""

    def __init__(self, ctx: _Context, p: importer.RowPlan, cells: dict[str, str]):
        self.ctx, self.p, self.cells = ctx, p, cells
        self.current = ctx.current(p.target)

    def put(self, column: str, key: str, value: Any, text: str) -> None:
        before = self.current[column] if self.current else ""
        if self.current is None or text != before:
            self.p.data[key] = value
            self.p.changes[column] = [before, text]


def _identify(ctx: _Context, p, cells, decisions, id_rows, key_rows) -> None:
    """Softwarden ID, then the source's external key, then the kind's natural key (contract number; a pool's
    application and SKU name), then a contract's possible twin by name (the uploader decides)."""
    run, row = ctx.run, p.row
    id_cell, key_cell = cells["softwarden_id"], cells["external_key"]
    if id_cell and len(id_rows[id_cell.lower()]) > 1:
        others = ", ".join(str(n) for n in id_rows[id_cell.lower()] if n != row.row_number)
        p.error("softwarden_id", f"The same Softwarden ID is on row {others}.")
    if key_cell and len(key_rows[key_cell]) > 1:
        others = ", ".join(str(n) for n in key_rows[key_cell] if n != row.row_number)
        p.error("external_key", f"The same external key is on row {others}.")
    if key_cell and run.source_id is None:
        p.error("external_key", "Choose an import source to use external keys.")
    target = None
    if id_cell:
        tid = _uuid(id_cell)
        if tid is None:
            p.error("softwarden_id", "That isn't a Softwarden ID.")
        elif tid in ctx.targets:
            target, p.match = ctx.targets[tid], "id"
        else:
            p.error("softwarden_id", f"No {ctx.noun} in this organization has that Softwarden ID.")
    elif key_cell and run.source_id:
        tid = ctx.key_to_target.get(key_cell)
        if tid is not None:
            target, p.match = ctx.targets.get(tid), "key"
    if target is not None and getattr(target, "archived_at", None) is not None:
        p.error("softwarden_id" if p.match == "id" else "external_key", "That contract is archived. Restore it first.")
        target = None
    if target is None and not id_cell and not p.errors:
        target = _natural_match(ctx, p, cells, decisions)
    if target is not None and key_cell and run.source_id and not any(e["column"] == "external_key" for e in p.errors):
        owner = ctx.key_to_target.get(key_cell)
        known = ctx.key_of_target.get(target.id)
        if owner is not None and owner != target.id:
            p.error("external_key", f"That external key belongs to another {ctx.noun}.")
        elif known is not None and known != key_cell:
            p.error("external_key", f"This {ctx.noun} already has the key {known} for this source.")
        elif known is None:
            p.new_key = key_cell
    elif target is None and key_cell and run.source_id:
        p.new_key = key_cell
    p.target = target


def _natural_match(ctx: _Context, p, cells, decisions):
    if ctx.kind == CONTRACT:
        number = cells.get("contract_number", "")
        if number:
            found = ctx.by_number.get(number.lower(), [])
            live = [c for c in found if c.archived_at is None]
            if len(live) == 1:
                p.match = "number"
                return live[0]
            if len(live) > 1:
                p.error("contract_number", f"{len(live)} contracts have this number. Add the softwarden_id column.")
                return None
            if found:
                p.error("contract_number", "The contract with this number is archived. Restore it first.")
                return None
        name = cells.get("name", "")
        twins = [c for c in ctx.by_name.get(name.lower(), []) if not number or not c.contract_number] if name else []
        if twins:
            twin = twins[-1]  # the newest term of that name
            p.possible = twin
            choice = decisions.get("matches", {}).get(str(p.row.row_number))
            if choice == "create":
                p.match_decision = "create"
            elif choice and str(twin.id) == choice:
                p.match_decision, p.match = "match", "approved"
                return twin
            else:
                p.pending.append(
                    {
                        "level": "decision",
                        "column": "name",
                        "message": f'A contract named "{twin.name}" exists. Create a new contract or match it.',
                    }
                )
        return None
    app_cell, sku = cells.get("application", ""), cells.get("sku_name", "")
    if not app_cell or not sku:
        return None
    app, _why = ctx.records.find(app_cell, Kind.APPLICATION)
    if app is None:
        return None
    pools = ctx.natural.get((app.id, sku.lower()), [])
    if len(pools) == 1:
        p.match = "natural"
        return pools[0]
    if len(pools) > 1:
        p.error("sku_name", f"{app.name} has {len(pools)} pools of this SKU. Add the softwarden_id column.")
    return None


def _plan_custom(ctx: _Context, p, target, cells) -> None:
    """The cf_<name> columns, as on records (importer._plan_custom): typed, checked as the API checks them; a blank
    cell leaves the value; a new contract or pool gets defaults and needs required fields."""
    given: dict[str, Any] = {}
    by_name = {cf.name: cf for cf in ctx.custom}
    for cf in ctx.custom:
        col = customfields.column(cf)
        try:
            value = customfields.parse_text(cf, cells.get(col, ""))
        except customfields.Invalid as e:
            p.error(col, f"{cf.label}: {e.message}")
            continue
        if value is not None:
            given[cf.name] = value
    current = (target.custom_field_data or {}) if target is not None else {}
    problems: dict[str, list[str]] = {}
    after = customfields.clean(
        ctx.org, ctx.kind, current, {customfields.KEY: given}, problems, creating=target is None, lock=False
    )
    for key, messages in problems.items():
        cf = by_name[key.removeprefix(customfields.PREFIX)]
        message = messages[0]
        p.error(customfields.column(cf), message if message.startswith(cf.label) else f"{cf.label}: {message}")
    if problems or after is None:
        return
    changed = {}
    for name in given:
        if after.get(name) != current.get(name):
            changed[name] = given[name]
            cf = by_name[name]
            p.changes[customfields.column(cf)] = [
                customfields.to_text(cf, current.get(name)),
                customfields.to_text(cf, after[name]),
            ]
    if changed:
        p.data[customfields.KEY] = changed


def _text(r: _Row, column: str, key: str | None = None) -> None:
    cell = r.cells.get(column, "")
    if cell:
        r.put(column, key or column, cell, cell)


def _choice_cell(r: _Row, column: str) -> None:
    cell = r.cells.get(column, "")
    if not cell:
        return
    value = _choice(column, cell)
    if value is None:
        r.p.error(column, f'{importer._column_label(column)} "{cell}" isn\'t valid. Use {_allowed(column)}.')
        return
    r.put(column, column, value, value)


def _date_cell(r: _Row, column: str) -> None:
    cell = r.cells.get(column, "")
    if not cell:
        return
    try:
        day = dates.parse_stored(cell)
    except ValueError as exc:
        r.p.error(column, str(exc))
        return
    r.put(column, column, day.isoformat(), day.isoformat())


def _plan_vendor(ctx: _Context, r: _Row, target) -> None:
    cell = r.cells.get("vendor", "")
    if not cell:
        return
    current = target.vendor if target is not None and target.vendor_id else None
    if current is not None and current.name.lower() == cell.lower():
        return
    found = ctx.vendors.get(cell.lower())
    shown = current.name if current else ""
    if found is not None:
        r.p.data["vendor"] = str(found.id)
        r.p.changes["vendor"] = [shown, found.name]
    elif len(cell) > 200:
        r.p.error("vendor", "Use 200 characters or fewer.")
    else:
        r.p.refs.append(
            {"table": "vendors", "key": cell.lower(), "email": "", "name": cell, "field": "vendor", "shown": shown}
        )


def _plan_notice(r: _Row) -> None:
    value, unit = r.cells.get("notice_value", ""), r.cells.get("notice_unit", "")
    if not value and not unit:
        return
    if not value:
        r.p.error("notice_value", "Enter the notice period as a whole number, or leave both notice cells empty.")
        return
    if not (value.isascii() and value.isdigit()):
        r.p.error("notice_value", "Enter the notice period as a whole number.")
        return
    chosen = _choice("notice_unit", unit) if unit else PeriodUnit.DAYS  # earlier notice periods were in days
    if chosen is None:
        r.p.error("notice_unit", f'Notice unit "{unit}" isn\'t valid. Use {_allowed("notice_unit")}.')
        return
    number = str(int(value))
    before = (r.current["notice_value"], r.current["notice_unit"]) if r.current else ("", "")
    if r.current is None or before != (number, chosen):
        r.p.data["notice_value"], r.p.data["notice_unit"] = number, str(chosen)
        if before[0] != number:
            r.p.changes["notice_value"] = [before[0], number]
        if before[1] != chosen:
            r.p.changes["notice_unit"] = [before[1], str(chosen)]


def parse_items(records: Records, cell: str) -> tuple[list[tuple[Resource, Decimal | None]], list[str]]:
    """An `items` cell as [(record, percent or None)] and the problems with it."""
    lines: list[tuple[Resource, Decimal | None]] = []
    problems: list[str] = []
    seen: set = set()
    for part in (x.strip() for x in cell.split(LINE_SEPARATOR)):
        if not part:
            continue
        name, sep, pct = part.rpartition("=")
        if not sep:
            name, pct = part, ""
        name, pct = name.strip(), pct.strip().removesuffix("%").strip()
        percent = None
        if sep:
            errors: dict[str, list[str]] = {}
            percent = contracts.decimal_value(
                pct or None, "allocation", errors, maximum=contracts.HUNDRED, label="allocation"
            )
            if errors or percent is None:
                problems.append(f'"{part}": ' + (errors["allocation"][0] if errors else "enter a percentage."))
                continue
        record, why = records.find(name)
        if record is None:
            problems.append(why or f'Nothing is named "{name}".')
            continue
        if record.id in seen:
            problems.append(f"{record.name} is listed twice.")
            continue
        seen.add(record.id)
        lines.append((record, percent))
    total = sum((pct for _r, pct in lines if pct is not None), Decimal(0))
    if total > contracts.HUNDRED:
        problems.append(f"Fixed allocations would total {total.normalize():f}% (max 100%).")
    return lines, problems


def _plan_items(ctx: _Context, r: _Row, target) -> None:
    cell = r.cells.get("items", "")
    if not cell:
        return
    lines, problems = parse_items(ctx.records, cell)
    for message in problems:
        r.p.error("items", message)
    if problems:
        return
    current = {i.resource_id: i.allocation_percent for i in ctx.lines.get(target.id, [])} if target else {}
    wanted = {rec.id: pct for rec, pct in lines}
    if target is not None and current == wanted:
        return
    before = r.current["items"] if r.current else ""
    after = f"{LINE_SEPARATOR} ".join(
        ctx.records.ref(rec) + ("" if pct is None else f"={pct.normalize():f}") for rec, pct in lines
    )
    r.p.data["items"] = lines
    r.p.changes["items"] = [before, after]


def _plan_contract(ctx: _Context, row, decisions, id_rows, key_rows) -> importer.RowPlan:
    p = importer.RowPlan(row)
    cells = {c: str(row.raw.get(c) or "").strip() for c in ctx.columns}
    _identify(ctx, p, cells, decisions, id_rows, key_rows)
    target = p.target
    r = _Row(ctx, p, cells)
    if not cells["name"] and target is None:
        p.error("name", "Enter a name.")
    if cells["contract_number"]:
        p.natural_key = cells["contract_number"].lower()
    _text(r, "name")
    _plan_vendor(ctx, r, target)
    for column in ("contract_number", "po_number", "cost_center", "notes"):
        _text(r, column)
    if cells["amount"]:
        errors: dict[str, list[str]] = {}
        amount = contracts.decimal_value(
            cells["amount"], "amount", errors, maximum=contracts.AMOUNT_MAX, label="amount"
        )
        if errors:
            p.error("amount", errors["amount"][0])
        elif amount is not None:
            r.put("amount", "amount", str(amount), _money(amount))
    if cells["currency"]:
        currency = cells["currency"].upper()
        if not contracts.CURRENCY.match(currency):
            p.error("currency", "Enter a three-letter currency code, such as USD or EUR.")
        else:
            r.put("currency", "currency", currency, currency)
    for column in ("billing_frequency", "status", "auto_renew"):
        _choice_cell(r, column)
    for column in ("start_date", "end_date"):
        _date_cell(r, column)
    _plan_notice(r)
    importer._plan_person(ctx, p, target, "renewal_owner", cells, decisions)
    renewal_cell, create_cell = cells["renewal"], cells["create_renewal_obligation"]
    create = None
    if create_cell:
        create = YES_NO.get(create_cell.lower())
        if create is None:
            p.error("create_renewal_obligation", "Use true or false, or leave it empty.")
    if renewal_cell and create:
        p.error("create_renewal_obligation", "Either link an existing renewal or create one, not both.")
    elif renewal_cell:
        obligation, why = ctx.renewals.find(renewal_cell)
        if obligation is None:
            p.error("renewal", why or "Choose an existing renewal.")
        elif target is None or target.renewal_obligation_id != obligation.id:
            r.put("renewal", "renewal_obligation", str(obligation.id), ctx.renewals.ref(obligation))
    elif create:
        if target is not None and target.renewal_obligation_id is not None:
            p.warn("create_renewal_obligation", "This contract already has a renewal, so none is created.")
        else:
            p.data[contracts.CREATE] = True
            p.changes["create_renewal_obligation"] = ["", "true"]
    _plan_items(ctx, r, target)
    if ctx.custom:
        _plan_custom(ctx, p, target, cells)
    return p


def _plan_pool(ctx: _Context, row, decisions, id_rows, key_rows) -> importer.RowPlan:
    p = importer.RowPlan(row)
    cells = {c: str(row.raw.get(c) or "").strip() for c in ctx.columns}
    _identify(ctx, p, cells, decisions, id_rows, key_rows)
    target = p.target
    r = _Row(ctx, p, cells)
    if cells["application"]:
        app, why = ctx.records.find(cells["application"], Kind.APPLICATION)
        if app is None:
            p.error("application", why or "Choose an application.")
        else:
            if cells["sku_name"]:
                p.natural_key = (app.id, cells["sku_name"].lower())
            if target is None or target.application_id != app.id:
                r.put("application", "application", str(app.id), ctx.records.ref(app))
    elif target is None:
        p.error("application", "Enter the application the seats are for.")
    if cells["contract"]:
        contract, why = ctx.contract_refs.find(cells["contract"])
        if contract is None:
            p.error("contract", why or "Choose a contract.")
        elif target is None or target.contract_id != contract.id:
            r.put("contract", "contract", str(contract.id), ctx.contract_refs.ref(contract))
    if not cells["sku_name"] and target is None:
        p.error("sku_name", "Enter the SKU or plan name, such as Microsoft 365 E5.")
    _text(r, "sku_name")
    _text(r, "sku_id")
    _text(r, "notes")
    for column in ("seats_purchased", "seats_assigned"):
        cell = cells[column]
        if not cell:
            continue
        if not (cell.isascii() and cell.isdigit()):
            p.error(column, "Enter a whole number of seats.")
            continue
        r.put(column, column, str(int(cell)), str(int(cell)))
    if cells["seat_sync"]:
        flag = YES_NO.get(cells["seat_sync"].lower())
        if flag is None:
            p.error("seat_sync", "Use true or false, or leave it empty.")
        else:
            r.put("seat_sync", "seat_sync", flag, "true" if flag else "false")
    if ctx.custom:
        _plan_custom(ctx, p, target, cells)
    return p


def plan_run(run, simulate: bool = False, actor=None) -> importer.Plan:
    """What importing this contracts or licence pools run would do now. With `simulate` (the preview) and at most
    REHEARSE_MAX rows, the rows that change something are also written through the services as `actor` and rolled back,
    so their refusals become row errors and their computed values are shown; a larger file is planned without that
    rehearsal (it is done in the request, and would take minutes), its service-rule checks run at commit, and the
    preview says so (panels.notices, counts.rehearsed 0). Saves nothing."""
    ctx = _Context(run)
    decisions = run.decisions or {}
    rows = list(run.rows.order_by("row_number"))
    id_rows: dict[str, list[int]] = defaultdict(list)
    key_rows: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        i, k = str(r.raw.get("softwarden_id") or "").strip(), str(r.raw.get("external_key") or "").strip()
        if i:
            id_rows[i.lower()].append(r.row_number)
        if k:
            key_rows[k].append(r.row_number)
    planner = _plan_contract if ctx.kind == CONTRACT else _plan_pool
    plans = [planner(ctx, r, decisions, id_rows, key_rows) for r in rows]
    _same_natural_key_errors(ctx, plans)
    plan = importer.assemble(
        plans, decisions, person_ids=ctx.person_ids, keyed_elsewhere=ctx.keyed_elsewhere, noun=ctx.noun
    )
    rehearse = len(rows) <= REHEARSE_MAX
    plan.panels["notices"] = [] if rehearse else [LARGE_FILE_NOTICE]
    plan.counts["rehearsed"] = 1 if rehearse else 0
    if simulate:
        if rehearse:
            _simulate(ctx, plan, actor if actor is not None else run.uploaded_by)
            plan.counts["rehearsed"] = 1
        for p in plan.rows:
            if not p.computed and p.target is not None:
                p.computed = _computed(ctx, p.target, ctx.lines.get(p.target.id, []))
    return plan


def _same_natural_key_errors(ctx: _Context, plans) -> None:
    """Rows that would create two contracts with one contract number, or two pools of one application and SKU name
    (case-insensitive), are errors, as two rows with one Softwarden ID or external key are: a later re-import couldn't
    tell them apart."""
    groups: dict[Any, list] = defaultdict(list)
    for p in plans:
        if p.target is None and p.natural_key is not None and not p.errors:
            groups[p.natural_key].append(p)
    column = "contract_number" if ctx.kind == CONTRACT else "sku_name"
    what = "contract number" if ctx.kind == CONTRACT else "application and SKU name"
    for group in groups.values():
        if len(group) > 1:
            for p in group:
                others = ", ".join(str(g.row.row_number) for g in group if g is not p)
                p.error(column, f"The same {what} is on row {others}.")


# ---- writing: the preview's rehearsal and the commit ------------------------------------------------------------------


class _Rollback(Exception):
    pass


def _column_of(key: str) -> str:
    if key.startswith(customfields.PREFIX):
        return customfields.COLUMN + key.removeprefix(customfields.PREFIX)
    return ERROR_COLUMNS.get(key, key)


def _apply(org, actor, run, p, made) -> Any:
    """Write one row through the services: the contract (or pool), then its lines, then a renewal created for it (so
    the renewal covers the applications on its lines)."""
    data = importer._resolve({k: v for k, v in p.data.items() if k not in ("items", contracts.CREATE)}, made)
    note = {"import_run": str(run.id)}
    if run.kind == LICENSE_POOL:
        if p.target is None:
            return contracts.create_pool(org, actor, data, note=note)
        return contracts.update_pool(org, actor, p.target, data, note=note) if data else p.target
    if p.target is None:
        contract = contracts.create_contract(org, actor, data, note=note)
    elif data:
        contract = contracts.update_contract(org, actor, p.target, data, note=note)
    else:
        contract = p.target
    if "items" in p.data:
        contracts.set_items(org, actor, contract, p.data["items"], note=note)
    if p.data.get(contracts.CREATE):
        contract = contracts.update_contract(org, actor, contract, {contracts.CREATE: True}, note=note)
    return contract


def _computed(ctx: _Context, target, lines: list[ContractItem] | None = None) -> dict[str, Any]:
    """What the row's contract or pool works out to: shown in the preview."""
    if ctx.kind == LICENSE_POOL:
        return {
            "seats_purchased": target.seats_purchased,
            "seats_assigned": target.seats_assigned,
            "utilization": contracts.utilization(target),
        }
    c = target
    deadline = contracts.contract_deadline(c, ctx.org.renewal_lead_days)
    items = {c.id: lines} if lines is not None else None
    allocation = []
    for a in contracts.allocations([c], items):
        record = None
        if a.item is not None:
            found = ctx.records.by_id.get(a.item.resource_id) or a.item.resource
            record = {"id": str(found.id), "name": found.name, "kind": found.kind}
        allocation.append({"record": record, "share": str(a.share.quantize(CENT)), "amount": _money(a.amount)})
    return {
        "annualized_cost": _money(c.annualized_cost) or None,
        "currency": c.currency,
        "annualization_note": contracts.annualization_note(c.amount, c.billing_frequency, c.start_date, c.end_date),
        "cancellation_deadline": _iso(c.cancellation_deadline) or None,
        "deadline_label": deadline.label,
        "deadline_explanation": deadline.explanation,
        "renewal": str(c.renewal_obligation_id) if c.renewal_obligation_id else None,
        "allocation": allocation,
    }


def _simulate(ctx: _Context, plan: importer.Plan, actor) -> None:
    """Try the rows that change something, in order, through the services as `actor` (the person asking for the
    preview), then roll everything back. A refusal becomes the row's error; a row that saves shows what it computed,
    and its derived values join its changes."""
    org, run = ctx.org, ctx.run
    rows = [p for p in plan.rows if p.eligible and p.outcome in (RowOutcome.CREATE, RowOutcome.UPDATE)]
    if not rows:
        return
    try:
        with transaction.atomic():
            made, _counts = importer.make_refs(org, actor, plan, run)
            for p in rows:
                try:
                    with transaction.atomic():
                        saved = _apply(org, actor, run, p, made)
                        # Read afresh: the row's matched contract or pool keeps the values the preview compares with.
                        p.computed = _computed(ctx, type(saved).objects.get(id=saved.id))
                except FieldErrors as e:
                    for key, messages in e.errors.items():
                        p.error(_column_of(key), messages[0])
                    continue
                except Conflict as e:
                    p.error("", str(e.detail))
                    continue
                if ctx.kind == CONTRACT:
                    before = ctx.current(p.target) or {}
                    for column in ("annualized_cost", "cancellation_deadline"):
                        after = p.computed.get(column) or ""
                        if before.get(column, "") != after:
                            p.changes[column] = [before.get(column, ""), after]
            raise _Rollback
    except _Rollback:
        pass
    importer.settle(plan)


def write(org, actor, run, plan: importer.Plan):
    """The commit's writes (imports/importer.commit_run has checked the plan): people and vendors chosen for
    creation, then each row through the services in order, with new external keys. A refusal now (the data changed
    between the checks and the write) undoes the whole commit."""
    made, created_counts = importer.make_refs(org, actor, plan, run)
    field = "contract" if run.kind == CONTRACT else "license_pool"
    totals = {"created": 0, "updated": 0, "unchanged": 0, "excluded": 0, "keys_stored": 0}
    results: list[ImportRow] = []
    for p in plan.rows:
        n = p.row.row_number
        if p.row.excluded:
            totals["excluded"] += 1
            continue
        try:
            if p.target is None:
                saved = _apply(org, actor, run, p, made)
                totals["created"] += 1
            elif p.data:
                saved = _apply(org, actor, run, p, made)
                totals["updated"] += 1
            else:
                saved = p.target
                totals["unchanged"] += 1
            if p.new_key:
                ExternalIdentifier.objects.create(
                    organization=org, source_id=run.source_id, external_key=p.new_key, **{field: saved}
                )
                totals["keys_stored"] += 1
        except FieldErrors as e:
            detail = "; ".join(m for msgs in e.errors.values() for m in msgs)
            raise Conflict(f"Row {n} couldn't be saved: {detail} Nothing was imported.") from None
        except Conflict as e:
            raise Conflict(f"Row {n} couldn't be saved: {e.detail} Nothing was imported.") from None
        setattr(p.row, f"result_{field}", saved)
        results.append(p.row)
    ImportRow.objects.bulk_update(results, [f"result_{field}"], batch_size=500)
    return importer.finish_commit(org, actor, run, plan, totals, created_counts)
