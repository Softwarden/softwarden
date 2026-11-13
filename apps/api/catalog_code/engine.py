"""Catalog-as-code: export an organization's catalog as a tree of YAML files, and plan or apply a tree back
(docs/CATALOG_AS_CODE.md has the format and the git workflow).

Export (`render_tree`, `export`) writes one file per record, `<folder>/<stem>.yaml`, plus `catalog.yaml`, the manifest
naming the kinds the tree holds. The same records always give the same bytes.

Plan and apply (`execute`) share one pass, the way an import's preview and commit do (imports/importer.py): inside one
transaction, under a per-organization lock, every file is read, matched to a record by its natural key and compared
field by field; each create or update then runs through the record's own service function (validation, organization
scope and an audit event marked `via: catalog_code`) in a savepoint, so a file that fails is that file's error and
the others still run. A plan always rolls everything back: it writes nothing, yet its errors are exactly the ones
apply would meet. Apply commits only when no file has an error.

Like an import's commit, apply refuses to run against a catalog that changed since it was planned: the plan returns
`plan_hash`, a SHA-256 over the organization's catalog as it is (its full export, people included), the tree and the
prune flag, and apply must send the same hash (409 /problems/catalog-code-stale otherwise). As in an import, records
are matched by key, never by name, and a whole batch is checked against the records limit before anything is
written. Rows missing from the tree are deleted only with prune, and only for the kinds the manifest lists (or, with
no manifest, the kinds the tree has files for). Discovery observations are never written."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from django.db import DataError, IntegrityError, connection, transaction
from rest_framework.exceptions import APIException

from catalog.models import Person, Resource, Vendor
from catalog.services import audit
from core import limits
from core.capabilities import CUSTOM_FIELDS
from core.problems import Conflict, FieldErrors, LimitReached
from core.request_context import acting_via, forget_organization

from . import fields as F
from . import fmt
from .bundle import TOO_LARGE
from .fmt import API_VERSION, Path, path_text
from .kinds import KINDS, ORDER, PRUNE_ORDER, KindSpec, PathErrors, PersonKind, ResourceKind, VendorKind
from .state import State

MANIFEST = "catalog.yaml"
CATALOG_KIND = "Catalog"
TOP_KEYS = ("apiVersion", "kind", "metadata", "spec", "status")
STALE = "/problems/catalog-code-stale"
INVALID = "/problems/catalog-code-invalid"
VIA = "catalog_code"
API_MAX_CREATES = 2000  # creates per API plan or apply (about 60 s at an import's pace; the request timeout is 120 s)
MAX_TREE_NODES = 400_000  # YAML values in the whole tree (each file has its own cap too, fmt.MAX_NODES)
DATA_ERROR = "A value doesn't fit what the database stores (too long, or out of range)."
# The prune cap: a prune that deletes more than MASS_DELETE_MAX records in all, more than MASS_DELETE_SHARE of a
# kind's records when that is at least MASS_DELETE_SHARE_MIN of them, or every record of a kind that has some, is an
# error unless the caller allows a mass delete. A bad checkout, a sparse clone or a truncated zip otherwise plans to
# delete the catalog.
MASS_DELETE_MAX = 10
MASS_DELETE_SHARE = 0.2
MASS_DELETE_SHARE_MIN = 3


# ---- export -------------------------------------------------------------------------------------------------------


def exported_kinds(state: State, include_people: bool) -> list[KindSpec]:
    return [k for k in ORDER if k.available(state) and (include_people or not isinstance(k, PersonKind))]


def render_doc(kind: KindSpec, state: State, values: dict[str, Any], obj: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    spec: dict[str, Any] = {}
    for f in kind.fields(state):
        value = F.render(f, values[f.name], state)
        if value is not None:
            (meta if f.section == "metadata" else spec)[f.key] = value
    doc: dict[str, Any] = {"apiVersion": API_VERSION, "kind": kind.name}
    if meta:
        doc["metadata"] = meta
    if spec:
        doc["spec"] = spec
    status = kind.status(state, obj)
    if status:
        doc["status"] = status
    return doc


def _render(state: State, include_people: bool) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    kinds = exported_kinds(state, include_people)
    for kind in kinds:
        entries = []
        for obj in kind.objects(state):
            values = kind.values(state, obj)
            entries.append((kind.basename(state, values), str(kind.key(values)), render_doc(kind, state, values, obj)))
        entries.sort(key=lambda e: (e[0], e[1]))
        used: set[str] = set()
        for base, _, doc in entries:
            name, n = base, 1
            while name.lower() in used:
                n += 1
                name = f"{base}-{n}"
            used.add(name.lower())
            files[f"{kind.folder}/{name}.yaml"] = fmt.dump(doc)
    manifest = {
        "apiVersion": API_VERSION,
        "kind": CATALOG_KIND,
        "spec": {"kinds": [k.name for k in kinds], "includesPeople": include_people},
    }
    files[MANIFEST] = fmt.dump(manifest)
    return files


def render_tree(org, *, include_people: bool = False) -> dict[str, bytes]:
    """The organization's catalog as {path: bytes}. People (names, emails, titles) only with include_people; without
    it, owners and members are named by their Softwarden ID."""
    state = State(org, include_people=include_people)
    return _render(state, include_people)


@transaction.atomic
def export(org, actor, *, include_people: bool = False, interface: str = "api") -> dict[str, bytes]:
    """render_tree, audited: an export is the whole catalog leaving the application."""
    files = render_tree(org, include_people=include_people)
    with acting_via(via=VIA):
        audit(
            org,
            actor,
            "catalog_code.exported",
            None,
            {"files": len(files), "include_people": include_people, "interface": interface},
        )
    return files


def _digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).digest())
    return h.hexdigest()


def plan_hash(state: State, files: dict[str, bytes], prune: bool) -> str:
    current = _render(state, include_people=True)
    text = f"{API_VERSION}\norg={state.org.pk}\nprune={prune}\nstate={_digest(current)}\ntree={_digest(files)}"
    return hashlib.sha256(text.encode()).hexdigest()


# ---- reading a tree -----------------------------------------------------------------------------------------------


@dataclass
class Doc:
    path: str
    kind: KindSpec
    raw: dict[str, Any]
    lines: dict[Path, int]

    def line(self, path: Path | None) -> int:
        at: Path = path or ("kind",)
        while at:
            if at in self.lines:
                return self.lines[at]
            at = at[:-1]
        return self.lines.get((), 1)


@dataclass
class Result:
    plan_hash: str
    prune: bool
    applied: bool = False
    changes: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0
    files: int = 0

    def problem(self, bucket: list, file: str | None, line: int | None, path: Path | None, message: str, **extra):
        bucket.append(
            {"file": file, "line": line, "path": path_text(path) if path else None, "message": message, **extra}
        )

    def summary(self) -> dict[str, int]:
        counts = {"create": 0, "update": 0, "delete": 0}
        for change in self.changes:
            counts[change["action"]] += 1
        return {**counts, "unchanged": self.unchanged, "errors": len(self.errors), "files": self.files}

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "prune": self.prune,
            "applied": self.applied,
            "summary": self.summary(),
            "changes": self.changes,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class Manifest:
    kinds: list[str] | None = None
    include_people: bool | None = None


def _manifest(path: str, value: dict[str, Any], lines: dict[Path, int], result: Result) -> Manifest | None:
    spec = value.get("spec") or {}
    if not isinstance(spec, dict):
        result.problem(result.errors, path, lines.get(("spec",)), ("spec",), "Write spec as kinds and includesPeople.")
        return None
    out = Manifest()
    for key in spec:
        if key not in ("kinds", "includesPeople"):
            result.problem(
                result.errors,
                path,
                lines.get(("spec", key)),
                ("spec", key),
                f"The manifest has kinds and includesPeople; {key} isn't one.",
            )
    kinds = spec.get("kinds")
    if kinds is not None:
        if not isinstance(kinds, list) or not all(isinstance(k, str) and k in KINDS for k in kinds):
            result.problem(
                result.errors,
                path,
                lines.get(("spec", "kinds")),
                ("spec", "kinds"),
                f"List kinds from: {', '.join(KINDS)}.",
            )
            return None
        out.kinds = kinds
    people = spec.get("includesPeople")
    if people is not None and not isinstance(people, bool):
        result.problem(
            result.errors, path, lines.get(("spec", "includesPeople")), ("spec", "includesPeople"), "Use true or false."
        )
        return None
    out.include_people = people
    return out


def read_tree(files: dict[str, bytes], state: State, result: Result) -> tuple[list[Doc], Manifest | None]:
    """The tree's documents in path order, each with a known kind and only known keys; every problem is a file error
    with its line."""
    docs: list[Doc] = []
    manifest: Manifest | None = None
    nodes = 0
    for path in sorted(files):
        data = files[path]
        if data == TOO_LARGE:
            result.problem(result.errors, path, None, None, f"The file is larger than {fmt.MAX_FILE_BYTES // 1024} KB.")
            continue
        try:
            value, lines = fmt.load(data)
        except fmt.YamlError as e:
            result.problem(result.errors, path, e.line, None, e.message)
            continue
        nodes += len(lines)
        if nodes > MAX_TREE_NODES:
            message = f"The tree has more than {MAX_TREE_NODES:,} values in all; split it."
            result.problem(result.errors, path, None, None, message)
            break

        def err(where: Path | None, message: str, _path=path, _lines=lines) -> None:
            line = None
            at = where or ()
            while at and line is None:
                line = _lines.get(at)
                at = at[:-1]
            result.problem(result.errors, _path, line or _lines.get((), 1), where, message)

        if not isinstance(value, dict):
            err(None, "A file holds one mapping: apiVersion, kind, metadata and spec.")
            continue
        bad = [k for k in value if k not in TOP_KEYS]
        for key in bad:
            err((key,), f"Unknown key {key}. A file has {', '.join(TOP_KEYS)}.")
        if value.get("apiVersion") != API_VERSION:
            err(("apiVersion",), f"Set apiVersion: {API_VERSION}.")
            continue
        kind_name = value.get("kind")
        if kind_name == CATALOG_KIND:
            if path != MANIFEST:
                err(("kind",), f"The Catalog manifest belongs in {MANIFEST} at the top of the tree.")
                continue
            manifest = _manifest(path, value, lines, result)
            continue
        kind = KINDS.get(kind_name) if isinstance(kind_name, str) else None
        if kind is None:
            err(("kind",), f"Unknown kind {kind_name!r}. Use one of: {', '.join(KINDS)}.")
            continue
        if not kind.available(state):
            err(
                ("kind",),
                f"{kind.name} needs the {kind.capability} capability, which this organization's edition doesn't include.",
            )
            continue
        if bad:
            continue
        ok = True
        known = kind.fields(state)
        for section in ("metadata", "spec"):
            part = value.get(section)
            if part is None:
                continue
            if not isinstance(part, dict):
                err((section,), f"Write {section} as field: value.")
                ok = False
                continue
            names = {f.key for f in known if f.section == section}
            for key in part:
                if key in names:
                    continue
                ok = False
                where = (section, key)
                if (
                    section == "spec"
                    and key in {fmt.camel(n) for n in _COMPLIANCE_KEYS}
                    and isinstance(kind, ResourceKind)
                ):
                    err(
                        where,
                        "The compliance profile is off for this organization. An Admin can turn it on in Organization settings.",
                    )
                elif key == "customFields" and CUSTOM_FIELDS not in state.caps:
                    err(
                        where,
                        f"Custom fields need the {CUSTOM_FIELDS} capability, which this organization's edition doesn't include.",
                    )
                else:
                    allowed = ", ".join(sorted(names)) or "nothing"
                    err(where, f"Unknown field {key} for {kind.name}. {section} takes: {allowed}.")
        if ok:
            docs.append(Doc(path, kind, value, lines))
    return docs, manifest


_COMPLIANCE_KEYS = (
    "is_gxp",
    "validation_status",
    "validation_doc_url",
    "last_periodic_review",
    "next_periodic_review",
    "risk_tier",
)


# ---- planning and applying ----------------------------------------------------------------------------------------


class _Rollback(Exception):
    def __init__(self, result: Result):
        super().__init__("rolled back")
        self.result = result


class Stale(Conflict):
    def __init__(self):
        super().__init__(
            "The catalog or the files changed since this plan was made, so nothing was applied. Plan again and review "
            "the new plan.",
            extra={"type": STALE},
        )


def _display(f: F.Field, value: Any, state: State) -> Any:
    shown = F.render(f, value, state)
    if shown is None and f.type in (F.FLAG, F.INT, F.BOOL):
        return value
    return shown


class Run:
    """One pass over a tree. The hooks (instance_*) are called by kinds.ApplicationKind while it writes."""

    def __init__(self, org, actor, state: State, result: Result, prune: bool, max_creates: int | None = None):
        self.org, self.actor, self.state, self.result, self.prune = org, actor, state, result, prune
        self.index: dict[str, dict[Any, Any]] = {}
        self.seen: dict[str, dict[Any, str]] = {}
        self.kept: dict[str, set[Any]] = {}
        self.instance_deletes: list[tuple[Any, Any]] = []
        self.pruned: dict[str, tuple[int, int]] = {}  # kind -> (records prune deletes, records of the kind)
        self.block_record_creates = False
        self.max_creates = max_creates
        self.resolved: dict[str, dict[Any, str]] = {}  # kind -> {row pk: the file that resolved to it}
        self.reference_only: set[Any] = set()  # people matched by name only: their fields are never written
        self.unkeyed_names: dict[str, list[str]] = {}  # name -> Person files with no email and an unknown ID
        self.creates = 0
        self.doc: Doc | None = None
        self.entry: dict[str, Any] | None = None

    def error(self, doc: Doc | None, path: Path | None, message: str, **extra) -> None:
        if doc is None:
            self.result.problem(self.result.errors, None, None, path, message, **extra)
        else:
            self.result.problem(self.result.errors, doc.path, doc.line(path), path, message)

    def doc_path(self) -> str:
        return self.doc.path if self.doc is not None else ""

    def note_unkeyed_people(self, docs: list[Doc]) -> None:
        """The Person files a name would have to match (no email, an ID this organization doesn't know), by name."""
        for doc in docs:
            if not isinstance(doc.kind, PersonKind):
                continue
            meta = doc.raw.get("metadata") or {}
            email, pid, name = meta.get("email"), meta.get("id"), meta.get("name")
            if (isinstance(email, str) and email.strip()) or not isinstance(name, str):
                continue
            known = self.state.find_person(pid.strip()) if isinstance(pid, str) and pid.strip() else None
            if known is None:
                self.unkeyed_names.setdefault(name.strip(), []).append(doc.path)

    def warn(self, path: Path | None, message: str) -> None:
        """A warning on the file being processed."""
        if self.doc is not None:
            self.result.problem(self.result.warnings, self.doc.path, self.doc.line(path), path, message)

    def index_for(self, kind: KindSpec) -> dict[Any, Any]:
        if kind.name not in self.index:
            self.index[kind.name] = {kind.key(kind.values(self.state, o)): o for o in kind.objects(self.state)}
        return self.index[kind.name]

    def parse(self, doc: Doc) -> dict[str, Any] | None:
        problems = F.Problems()
        values: dict[str, Any] = {}
        for f in doc.kind.fields(self.state):
            raw = (doc.raw.get(f.section) or {}).get(f.key)
            if raw is None and f.required:
                problems.add(f.path, f"Add {f.section}.{f.key}.")
                values[f.name] = F.BAD
                continue
            values[f.name] = F.parse(f, raw, self.state, f.path, problems, values, doc.kind.custom_kind)
        if isinstance(doc.kind, PersonKind) and not problems and not values["email"] and not values["id"]:
            problems.add(("metadata",), "Add metadata.email, or metadata.id for a person without one.")
        for path, message in problems.items:
            self.error(doc, path, message)
        return None if problems else values

    def upsert(self, doc: Doc) -> None:
        kind, state = doc.kind, self.state
        values = self.parse(doc)
        if values is None:
            return
        key = kind.key(values)
        seen = self.seen.setdefault(kind.name, {})
        if key in seen:
            self.error(doc, ("kind",), f"This is the same {kind.name} as {seen[key]}.")
            return
        seen[key] = doc.path
        self.doc = doc
        try:
            obj = kind.find(self, self.index_for(kind), values)
        except PathErrors as e:
            for path, message in e.items:
                self.error(doc, path, message)
            return
        finally:
            self.doc = None
        label = kind.label(state, values)
        entry: dict[str, Any] = {"file": doc.path, "kind": kind.name, "key": label, "action": "create", "fields": []}
        current: dict[str, Any] = {}
        changed: list[F.Field] = []
        if obj is not None:
            # Two files that differ by key can still name one row (an email and the same person's ID): refused, as
            # two files with one key are, so no row is written twice in a run.
            resolved = self.resolved.setdefault(kind.name, {})
            if obj.pk in resolved:
                self.error(doc, ("kind",), f"This is the same {kind.name} as {resolved[obj.pk]}.")
                return
            resolved[obj.pk] = doc.path
            self.kept.setdefault(kind.name, set()).add(obj.pk)
            current = kind.values(state, obj)
            changed = [
                f for f in kind.fields(state) if not f.identity and not F.same(f, current[f.name], values[f.name])
            ]
            if changed and obj.pk in self.reference_only:
                # Matched by name only (PersonKind.find): the file links references to the person, never edits them.
                for f in changed:
                    before, after = _display(f, current[f.name], state), _display(f, values[f.name], state)
                    message = f"Not changed (matched by name only): {before!r} here, {after!r} in the file."
                    self.result.problem(self.result.warnings, doc.path, doc.line(f.path), f.path, message)
                changed = []
            if not changed:
                self.result.unchanged += 1
                return
            entry["action"] = "update"
        for f in kind.fields(state) if obj is None else changed:
            if f.type == F.INSTANCES:
                continue  # each instance is its own line (instance_change)
            if f.type == F.CUSTOM:
                before, after = current.get(f.name) or {}, values[f.name]
                for name in sorted(set(before) | set(after)):
                    if before.get(name) != after.get(name):
                        entry["fields"].append(
                            {"path": path_text((*f.path, name)), "before": before.get(name), "after": after.get(name)}
                        )
                continue
            if obj is None and F.render(f, values[f.name], state) is None:
                continue  # a create lists what the file sets, not the defaults
            after = _display(f, values[f.name], state)
            before = _display(f, current[f.name], state) if obj is not None else None
            entry["fields"].append({"path": path_text(f.path), "before": before, "after": after})
        if obj is None:
            self.creates += 1
            if self.max_creates is not None and self.creates > self.max_creates:
                raise TooManyCreates(self.max_creates)
        if obj is None and self.block_record_creates and isinstance(kind, ResourceKind):
            self.result.changes.append(entry)  # the limit error is already reported; nothing is rehearsed
            return
        self.doc, self.entry = doc, entry
        try:
            with transaction.atomic():
                written = kind.create(self, values) if obj is None else kind.update(self, obj, current, values, changed)
        except PathErrors as e:
            for path, message in e.items:
                self.error(doc, path, message)
            return
        except FieldErrors as e:
            for name, messages in e.errors.items():
                self.error(doc, kind.error_path(state, str(name)), messages[0] if messages else "Check this field.")
            return
        except APIException as e:
            self.error(doc, ("kind",), str(e.detail))
            return
        except IntegrityError:
            self.error(doc, ("kind",), "This conflicts with a row saved at the same time. Plan again.")
            return
        except DataError:
            self.error(doc, ("kind",), DATA_ERROR)
            return
        finally:
            self.doc, self.entry = None, None
        state.add(written)
        self.index_for(kind)[kind.key(values)] = written
        self.kept.setdefault(kind.name, set()).add(written.pk)
        if entry["action"] == "update" and not entry["fields"]:
            self.result.unchanged += 1  # only an instance the file leaves out, kept without prune
        else:
            self.result.changes.append(entry)

    # ---- hooks for an application's instances ----

    def instance_path(self, key: str) -> Path:
        items = (self.doc.raw.get("spec") or {}).get("instances") or [] if self.doc else []
        for i, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("name") or "").strip().lower() == key:
                return ("spec", "instances", i)
        return ("spec", "instances")

    def instance_change(self, key: str, before: dict | None, after: dict | None) -> None:
        if self.entry is not None:
            name = (after or before or {}).get("name", key)
            self.entry["fields"].append(
                {
                    "path": f"spec.instances[{name}]",
                    "before": F.render_instance(before) if before else None,
                    "after": F.render_instance(after) if after else None,
                }
            )

    def instance_missing(self, record, instance) -> None:
        if self.prune:
            self.instance_deletes.append((record, instance))
            self.instance_change(
                instance.name.lower(),
                {
                    "name": instance.name,
                    "environment": instance.environment,
                    "url": instance.url,
                    "status": instance.status,
                },
                None,
            )
        elif self.doc is not None:
            self.result.problem(
                self.result.warnings,
                self.doc.path,
                self.doc.line(("spec", "instances")),
                ("spec", "instances"),
                f"The instance {instance.name} isn't in the file. It is kept; apply with prune to delete it.",
            )

    # ---- prune ----

    def run_prune(self, scope: set[str]) -> None:
        for kind in PRUNE_ORDER:
            if kind.name == "Application":
                self._delete_instances()
            if kind.name not in scope or not kind.available(self.state):
                continue
            kept = self.kept.get(kind.name, set())
            keep = getattr(kind, "keep", None)
            rows = []
            total = 0
            for obj in kind.objects(self.state):
                total += 1
                if obj.pk in kept or (keep is not None and keep(obj)):
                    continue
                values = kind.values(self.state, obj)
                rows.append((kind.label(self.state, values), obj))
            if rows:
                self.pruned[kind.name] = (len(rows), total)
            for label, obj in sorted(rows, key=lambda r: r[0].lower()):
                fields = kind.delete_fields(self.state, obj)  # read before the delete: what it takes with it
                try:
                    with transaction.atomic():
                        kind.delete(self, obj)
                except APIException as e:
                    self.error(
                        None, None, f"{kind.name} {label} can't be deleted: {e.detail}", kind=kind.name, key=label
                    )
                    continue
                except (IntegrityError, DataError):
                    message = f"{kind.name} {label} can't be deleted: {DATA_ERROR}"
                    self.error(None, None, message, kind=kind.name, key=label)
                    continue
                self.result.changes.append(
                    {"file": None, "kind": kind.name, "key": label, "action": "delete", "fields": fields}
                )

    def check_mass_delete(self) -> None:
        """The prune cap: past it, an error that names the count, so nothing is applied."""
        deletes = sum(n for n, _ in self.pruned.values())
        heavy = {
            name: (n, total)
            for name, (n, total) in self.pruned.items()
            if (n >= MASS_DELETE_SHARE_MIN and n > total * MASS_DELETE_SHARE) or n == total
        }
        if deletes <= MASS_DELETE_MAX and not heavy:
            return
        kinds = ", ".join(f"{name} {n} of {total}" for name, (n, total) in self.pruned.items())
        self.result.problem(
            self.result.errors,
            None,
            None,
            None,
            f"This tree with prune deletes {deletes} records ({kinds}): more than a prune deletes without asking "
            f"(more than {MASS_DELETE_MAX} in all, more than {MASS_DELETE_SHARE:.0%} of a kind, or every record of a "
            "kind). Nothing was applied. Check that the tree is complete (a partial checkout or a truncated zip "
            "deletes what it is missing); if the deletes are meant, plan and apply again with allow_mass_delete "
            "(manage.py catalog_apply --allow-mass-delete).",
            mass_delete=deletes,
        )

    def _delete_instances(self) -> None:
        from catalog import services

        for record, instance in self.instance_deletes:
            label = f"{record.kind}:{record.slug}@{instance.name}"
            try:
                with transaction.atomic():
                    services.delete_instance(self.org, self.actor, instance)
            except APIException as e:
                self.error(
                    None, None, f"The instance {label} can't be deleted: {e.detail}", kind="Application", key=label
                )
            except (IntegrityError, DataError):
                message = f"The instance {label} can't be deleted: {DATA_ERROR}"
                self.error(None, None, message, kind="Application", key=label)
        self.instance_deletes = []


def _lock(org) -> None:
    """One plan or apply per organization at a time, until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"catalog_code:{org.pk}"])


def _meta_text(doc: Doc, key: str) -> str | None:
    value = (doc.raw.get("metadata") or {}).get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _lock_rows(org, state: State, docs: list[Doc], prune_scope: set[str] | None) -> bool:
    """Lock the existing rows this run may write before it writes any: the People its Person files
    name (by email or Softwarden ID; a match by name only is never written), the records its record files name and the
    vendors its Vendor files name, plus, when pruning, every record and vendor of a kind in the prune's scope (any of
    them may be deleted). Each table's rows in ID order, FOR NO KEY UPDATE, in the order offboarding's reassign takes
    them (people, records, vendors; a tree never writes contracts or renewals), and bulk edit and bulk delete take
    records in ID order too: files are applied in path order, so without this a tree that updates two records could
    deadlock against a bulk edit or a reassign holding them the other way round. Returns whether any row was locked."""
    people: set[Any] = set()
    records: set[Any] = set()
    vendors: set[Any] = set()
    for doc in docs:
        if isinstance(doc.kind, PersonKind):
            for key in ("email", "id"):
                text = _meta_text(doc, key)
                found = state.find_person(text) if text else None
                if found is not None:
                    people.add(found)
        elif isinstance(doc.kind, ResourceKind):
            slug = _meta_text(doc, "slug")
            found = state.find_record(doc.kind.kind, slug) if slug else None
            if found is not None:
                records.add(found)
        elif isinstance(doc.kind, VendorKind):
            name = _meta_text(doc, "name")
            found = state.find_named(Vendor, name) if name else None
            if found is not None:
                vendors.add(found)
    if prune_scope:
        pruned = {k.kind for k in ORDER if isinstance(k, ResourceKind) and k.name in prune_scope}
        records |= {pk for pk, r in state.resources.items() if r.kind in pruned}
        if VendorKind.name in prune_scope:
            vendors |= set(state.rows[Vendor])
    locked = False
    for model, ids in ((Person, people), (Resource, records), (Vendor, vendors)):
        if ids:
            rows = model.objects.select_for_update(no_key=True).filter(organization=org, id__in=ids).order_by("id")
            locked |= bool(list(rows.values_list("id", flat=True)))
    return locked


def _records_to_create(docs: list[Doc], state: State) -> int:
    count = 0
    for doc in docs:
        if isinstance(doc.kind, ResourceKind):
            slug = (doc.raw.get("metadata") or {}).get("slug")
            if not (isinstance(slug, str) and (doc.kind.kind, slug.strip()) in state.by_slug):
                count += 1
    return count


def execute(
    org,
    actor,
    files: dict[str, bytes],
    *,
    prune: bool = False,
    apply: bool = False,
    expected_hash: str | None = None,
    interface: str = "api",
    max_creates: int | None = None,
    allow_mass_delete: bool = False,
) -> dict[str, Any]:
    """Plan (apply=False) or apply a tree. Returns the plan: plan_hash, summary, changes, errors and warnings, and
    `applied`. Raises Stale when expected_hash is given and differs, and LimitReached when applying would pass the
    records limit. An apply with errors writes nothing and returns applied=False. `max_creates` (the API's): more
    creates than that is TooManyCreates (400), so a request never runs into the server's timeout. A prune past the cap
    (MASS_DELETE_MAX, MASS_DELETE_SHARE, or a kind left with no record) is an error naming the count unless
    `allow_mass_delete`."""
    try:
        with acting_via(via=VIA), transaction.atomic():
            _lock(org)
            state = State(org, include_people=True)
            result = Result("", prune, files=len(files))
            docs, manifest = read_tree(files, state, result)  # reads only the organization's capabilities from state
            scope = None
            if prune:
                scope = set(manifest.kinds) if manifest and manifest.kinds is not None else {d.kind.name for d in docs}
            if _lock_rows(org, state, docs, scope):
                state = State(org, include_people=True)  # as the rows are now that this run holds them
            digest = plan_hash(state, files, prune)
            if expected_hash is not None and expected_hash != digest:
                raise Stale()
            result.plan_hash = digest
            people = manifest.include_people if manifest and manifest.include_people is not None else None
            state.include_people = people if people is not None else any(isinstance(d.kind, PersonKind) for d in docs)
            run = Run(org, actor, state, result, prune, max_creates)
            run.note_unkeyed_people(docs)
            try:
                limits.check_room(org, limits.RECORDS, _records_to_create(docs, state))
            except LimitReached as e:
                if apply:
                    raise
                result.problem(result.errors, None, None, None, str(e.detail), limit=e.limit)
                run.block_record_creates = True
            for kind in ORDER:
                for doc in docs:
                    if doc.kind is kind:
                        run.upsert(doc)
            if scope is not None:
                run.run_prune(scope)
                if not allow_mass_delete:
                    run.check_mass_delete()
            if not apply or result.errors:
                raise _Rollback(result)
            summary = result.summary()
            changes = {**summary, "prune": prune, "interface": interface}
            if allow_mass_delete:
                changes["allow_mass_delete"] = True
            audit(org, actor, "catalog_code.applied", None, changes)
            result.applied = True
    except _Rollback as rolled_back:
        forget_organization(org.pk)  # caches filled from rows the rollback removed (custom field definitions)
        return rolled_back.result.as_dict()
    except Exception:
        forget_organization(org.pk)
        raise
    return result.as_dict()


class TooManyCreates(FieldErrors):
    """400: the tree would create more rows than one API request does (API_MAX_CREATES), which could outlast the
    server's 120 s request timeout. Nothing was written. The CLI has no such cap."""

    def __init__(self, maximum: int):
        super().__init__(
            {
                "file": [
                    f"This tree creates more than {maximum:,} rows, more than one request can do in time. Nothing was "
                    "written. Apply a first load this large with manage.py catalog_apply, or split the tree."
                ]
            }
        )


class InvalidTree(APIException):
    """400 /problems/catalog-code-invalid: an apply whose files have errors; nothing was written. The problem lists
    them as `file_errors` (file, line, path, message), as a plan does."""

    status_code = 400

    def __init__(self, result: dict[str, Any]):
        count = len(result["errors"])
        super().__init__(
            f"{count} problem{'s' if count != 1 else ''} in the files, so nothing was applied. Fix them and plan again."
        )
        self.extra = {"type": INVALID, "file_errors": result["errors"]}
