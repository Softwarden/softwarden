"""Scope rules the sources share (discovery/entra.py since reconciliation, azure.py, powerplatform.py,
halo.py): the `name_exclusions` list (name prefixes a run skips, counted under "name excluded")."""

from typing import Any

NAME_MAX = 200
NAMES_MAX = 100


def validate_names(scope: dict[str, Any], errors: dict[str, list[str]]) -> None:
    """`name_exclusions`: one name or prefix per line, at most NAMES_MAX of NAME_MAX characters each."""
    names = scope.get("name_exclusions", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and 0 < len(n.strip()) <= NAME_MAX for n in names):
        errors["scope.name_exclusions"] = [f"Enter one name or prefix per line, {NAME_MAX} characters at most."]
    elif len(names) > NAMES_MAX:
        errors["scope.name_exclusions"] = [f"Use {NAMES_MAX} exclusions or fewer."]


def excluded_by_name(name: str, scope: dict[str, Any] | None) -> bool:
    """The name starts with one of the scope's exclusions (case-insensitive, each exclusion stripped; one that is only
    spaces excludes nothing)."""
    prefixes = [
        n.strip().lower() for n in (scope or {}).get("name_exclusions") or [] if isinstance(n, str) and n.strip()
    ]
    return any(name.lower().startswith(p) for p in prefixes)
