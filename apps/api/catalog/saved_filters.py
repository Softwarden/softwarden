"""Saved filters, which are saved views (catalog 0039): a
name, the list's query parameters (never results) and optionally the columns to show. The four built-ins live in
code with fixed IDs. Every stored view has an owner (`user`); a private view is seen by its owner only, a shared one
(`shared`) by everyone in the organization.

Who may do what:

- anyone signed in saves private views and changes or deletes their own;
- sharing needs Editor or Admin: creating a shared view, or sharing a private one;
- a shared view is changed or deleted by its owner while they are an Editor or Admin, or by any Admin (an Editor
  can't remove a colleague's shared view);
- someone else's private view doesn't exist for you (404), Admins included.

The rows a view shows, its columns and the link that opens it are in catalog/saved_views.py."""

from typing import Any

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from rest_framework import exceptions

from core import limits
from core.models import Membership, Role
from core.problems import FieldErrors

from . import saved_views, services
from .filters import AIAgentFilterSet, ApplicationFilterSet, ComponentFilterSet, query_errors
from .models import RECORD_KINDS, SavedFilter

BUILTIN_PREFIX = "builtin:"
NAME_MAX = 100
# The query strings the Applications and Components lists use. "Missing owners" is the list's ownership gap filter
# (no business owner, technical owner or support team, or an inactive owner).
BUILTINS = [
    ("missing-owners", "Missing owners", {"gap": "true"}),
    ("overdue-reviews", "Overdue reviews", {"review_state": "overdue"}),
    ("unreviewed-imports", "Unreviewed imports", {"imported": "true", "review_state": "unreviewed"}),
    ("active", "Active", {"lifecycle": "active,deprecated", "archived": "false"}),
]
IGNORED_PARAMS = ("cursor", "page_size")  # position in a list, not part of the question
EDITABLE = ("name", "query", "columns", "shared")


def _role(org, user):
    return Membership.objects.get(user=user, organization=org).role


def can_see(view: SavedFilter, user) -> bool:
    return view.shared or view.user_id == user.id


def can_edit(view: SavedFilter, user, role: str) -> bool:
    """Change or delete: a private view by its owner; a shared one by its owner while an Editor or Admin, or by an
    Admin."""
    if not view.shared:
        return view.user_id == user.id
    return role == Role.ADMIN or (view.user_id == user.id and role in services.WRITE_ROLES)


def builtin_json(slug, name, query, kind):
    return {
        "id": BUILTIN_PREFIX + slug,
        "name": name,
        "kind": kind,
        "query": dict(query),
        "columns": [],
        "shared": True,
        "builtin": True,
        "mine": False,
        "owner": None,
        "can_edit": False,
        "can_delete": False,
        "problems": [],
        "subscription": None,
    }


def _owner(f) -> dict[str, Any] | None:
    owner = f.user
    if owner is None:
        return None
    return {"id": owner.id, "name": owner.get_full_name() or owner.email}


def subscription_json(sub) -> dict[str, Any] | None:
    if sub is None:
        return None
    return {"id": str(sub.id), "weekday": sub.weekday, "active": sub.active}


def filter_json(f, user, role, problems=(), subscription=None):
    """`problems`: what the list would refuse in the stored query now (a custom field changed since it was saved);
    the field's own changes prune their parameters, this is the backstop. `subscription`: the requester's own weekly
    email of this view, if any."""
    editable = can_edit(f, user, role)
    return {
        "id": str(f.id),
        "name": f.name,
        "kind": f.kind,
        "query": f.query,
        "columns": list(f.columns),
        "shared": f.shared,
        "builtin": False,
        "mine": f.user_id == user.id,
        "owner": _owner(f),
        "can_edit": editable,
        "can_delete": editable,
        "problems": list(problems),
        "subscription": subscription_json(subscription),
    }


def visible(org, user, kind=None):
    """Shared views and the user's own (for one kind, or all)."""
    rows = SavedFilter.objects.filter(organization=org).filter(Q(shared=True) | Q(user=user))
    return rows.filter(kind=kind) if kind is not None else rows


def problems_of(f, org) -> list[str]:
    query = f.query if isinstance(f.query, dict) else {}
    errors = query_errors(FILTERSETS[f.kind], query, IGNORED_PARAMS, organization=org)
    for key, messages in saved_views.query_key_errors(f.kind, query).items():
        errors.setdefault(key, messages)
    return [f"{name}: {' '.join(messages)}" for name, messages in errors.items()]


def subscriptions_of(org, user, views) -> dict:
    """{view id: the user's subscription} for these views."""
    from notify.models import SavedViewSubscription

    ids = [v.id for v in views]
    rows = SavedViewSubscription.objects.filter(
        organization=org, membership__user=user, saved_view_id__in=ids
    ).order_by("created_at")
    return {s.saved_view_id: s for s in rows}


def view_json(org, f, user, role):
    """One stored view as the API shows it to `user`."""
    return filter_json(f, user, role, problems_of(f, org), subscriptions_of(org, user, [f]).get(f.id))


def listing_for(org, user, kind):
    role = _role(org, user)
    rows = list(visible(org, user, kind).select_related("user").order_by("name", "id"))
    subs = subscriptions_of(org, user, rows)

    def json(f):
        return filter_json(f, user, role, problems_of(f, org), subs.get(f.id))

    shared = [json(f) for f in rows if f.shared]
    mine = [json(f) for f in rows if not f.shared]
    return [builtin_json(s, n, q, kind) for s, n, q in BUILTINS] + shared + mine


FILTERSETS = {"application": ApplicationFilterSet, "component": ComponentFilterSet, "ai_agent": AIAgentFilterSet}


def clean_query(query, kind, org=None):
    """The query as stored: strings only, valid for the list of this kind in this organization (custom field filters
    included). The list's own FilterSet validates it, so the messages name the same problems the list would."""
    if not isinstance(query, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in query.items()):
        raise FieldErrors({"query": ["Send the list's query parameters as an object of text values."]})
    kept = {k: v for k, v in query.items() if k not in IGNORED_PARAMS and v != ""}
    errors = query_errors(FILTERSETS[kind], kept, organization=org)
    for key, messages in saved_views.query_key_errors(kind, kept).items():
        errors.setdefault(key, messages)
    if errors:
        raise FieldErrors({"query": [f"{name}: {' '.join(msgs)}" for name, msgs in errors.items()]})
    return kept


def _clean_name(raw, errors) -> str:
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not name:
        errors["name"] = ["Enter a name."]
    elif len(name) > NAME_MAX:
        errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
    return name


def _check_name_free(org, actor, kind, name, exclude=None):
    taken = {n.lower() for _, n, _ in BUILTINS}
    others = visible(org, actor, kind)
    if exclude is not None:
        others = others.exclude(id=exclude.id)
    taken |= {f.name.lower() for f in others}
    if name.lower() in taken:
        raise FieldErrors({"name": ["A filter with this name already exists. Choose another name."]})


@transaction.atomic
def create(org, actor, data):
    errors: dict[str, list[str]] = {}
    kind = data.get("kind")
    if kind not in RECORD_KINDS:
        errors["kind"] = ["Choose application, component or ai_agent."]
    name = _clean_name(data.get("name"), errors)
    shared = data.get("shared", False)
    if not isinstance(shared, bool):
        errors["shared"] = ["Send true or false."]
        shared = False
    if shared and _role(org, actor) not in services.WRITE_ROLES:
        raise exceptions.PermissionDenied("Only Admins and Editors can share a filter.")
    if errors:
        raise FieldErrors(errors)
    query = clean_query(data.get("query", {}), kind, org)
    columns = saved_views.clean_columns(data.get("columns", []), kind, org)
    _check_name_free(org, actor, kind, name)
    limits.check_room(org, limits.OTHER_RECORDS)
    saved = SavedFilter.objects.create(
        organization=org,
        user=actor,
        shared=shared,
        kind=kind,
        name=name,
        query=query,
        columns=columns,
        created_by=actor,
    )
    services.audit(
        org,
        actor,
        "saved_filter.created",
        saved,
        {"name": name, "kind": kind, "shared": shared, "query": query, "columns": columns},
    )
    return saved


def find(org, actor, pk):
    """A stored view the actor can see, or 404 (built-ins are in code; someone else's private view doesn't exist)."""
    if str(pk).startswith(BUILTIN_PREFIX):
        raise FieldErrors({"id": ["Built-in filters can't be changed or deleted."]})
    if not services.is_uuid(pk):
        raise Http404
    saved = SavedFilter.objects.filter(organization=org, id=pk).first()
    if saved is None or not can_see(saved, actor):
        raise Http404
    return saved


def require_edit(org, actor, saved, role=None):
    role = role or _role(org, actor)
    if not can_edit(saved, actor, role):
        raise exceptions.PermissionDenied(
            "Only its owner (an Editor or Admin) or an Admin can change or remove a shared view."
        )
    return role


@transaction.atomic
def update(org, actor, saved, data):
    """Change a view's name, query, columns or sharing. Send only what changes."""
    saved = SavedFilter.objects.select_for_update().get(organization=org, id=saved.id)
    if not can_see(saved, actor):
        raise Http404
    role = require_edit(org, actor, saved)
    errors: dict[str, list[str]] = {name: ["This can't be changed."] for name in sorted(set(data) - set(EDITABLE))}
    values: dict[str, Any] = {}
    if "name" in data:
        values["name"] = _clean_name(data["name"], errors)
    if "shared" in data:
        if not isinstance(data["shared"], bool):
            errors["shared"] = ["Send true or false."]
        else:
            values["shared"] = data["shared"]
    if errors:
        raise FieldErrors(errors)
    if values.get("shared") is True and not saved.shared and role not in services.WRITE_ROLES:
        raise exceptions.PermissionDenied("Only Admins and Editors can share a filter.")
    if values.get("shared") is False and saved.shared:
        if saved.user_id is None:
            raise FieldErrors(
                {"shared": ["This shared view has no owner, so it can't be made private. Delete it instead."]}
            )
        if not Membership.objects.filter(organization=org, user_id=saved.user_id).exists():
            raise FieldErrors(
                {"shared": ["Its owner is no longer a member here, so it can't be made private. Delete it instead."]}
            )
    if "query" in data:
        values["query"] = clean_query(data["query"], saved.kind, org)
    if "columns" in data:
        values["columns"] = saved_views.clean_columns(data["columns"], saved.kind, org)
    changes = {k: [getattr(saved, k), v] for k, v in values.items() if getattr(saved, k) != v}
    if not changes:
        return saved
    if "name" in changes or "shared" in changes:
        owner_view = values.get("shared", saved.shared)
        # A view made private moves to its owner's list: its name must be free there too.
        _check_name_free(org, actor if owner_view else saved.user, saved.kind, values.get("name", saved.name), saved)
    for k, v in values.items():
        setattr(saved, k, v)
    saved.save()
    services.audit(org, actor, "saved_filter.updated", saved, {"name": saved.name, **changes})
    return saved


def delete_private_views_of(sender, instance, **kwargs):
    """pre_delete on User (connected in catalog/apps.py): a deleted user's private views go with them. Their shared
    views stay, ownerless (SavedFilter.user is SET_NULL), for Admins to manage. People are deactivated, not deleted,
    in the product; this covers an operator deleting an account."""
    SavedFilter.objects.filter(user=instance, shared=False).delete()


@transaction.atomic
def delete(org, actor, pk):
    """Private views: their owner. Shared: the owner (Editor or Admin) or an Admin. Someone else's private view doesn't
    exist. Subscriptions to it stay, and say on their next send day that the view was deleted."""
    if str(pk).startswith(BUILTIN_PREFIX):
        raise FieldErrors({"id": ["Built-in filters can't be deleted."]})
    saved = find(org, actor, pk)
    require_edit(org, actor, saved)
    services.audit(
        org,
        actor,
        "saved_filter.deleted",
        saved,
        {"name": saved.name, "kind": saved.kind, "shared": saved.shared},
    )
    saved.delete()
