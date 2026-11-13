"""Every audit action the code writes has a human label in the audit log (catalog/audit_log.py LABELS).

The actions are read from the source: every call to `audit(...)` or `audit_account(...)` in the local apps. A literal
action is checked directly. An action built at run time must be listed in DYNAMIC with every value it can take, so a
new dynamic call site fails here until someone lists (and labels) its values."""

import ast
from pathlib import Path

from catalog.audit_log import LABELS

ROOT = Path(__file__).resolve().parent.parent
APPS = (
    "core",
    "catalog",
    "money",
    "notify",
    "discovery",
    "imports",
    "catalog_code",
    "offboarding",
    "api",
    "eol",
    "tickets",
    "ee",
)
AUDIT_FUNCTIONS = {"audit", "audit_account"}  # catalog/services.py: action is the third positional argument of both

DYNAMIC = {
    # catalog/admin_services.py: reference data, model in (Team, Department, Vendor, SoftwareCategory), admin_services.NOUNS
    "f'{noun(model)}.created'": {"team.created", "department.created", "vendor.created", "category.created"},
    "f'{noun(model)}.updated'": {"team.updated", "department.updated", "vendor.updated", "category.updated"},
    "f'{noun(model)}.deleted'": {"team.deleted", "department.deleted", "vendor.deleted", "category.deleted"},
    # notify/services.py: a snooze or an acknowledgment
    "f\"notification.{('snoozed' if kind == SnoozeKind.SNOOZE else 'acknowledged')}\"": {
        "notification.snoozed",
        "notification.acknowledged",
    },
    # notify/channel_services.py: a destination's writes (noun from DESTINATIONS) and a channel test's outcome
    "f'{noun}.created'": {"teams_destination.created", "webhook_destination.created"},
    "f'{noun}.updated'": {"teams_destination.updated", "webhook_destination.updated"},
    "f'{noun}.deleted'": {"teams_destination.deleted", "webhook_destination.deleted"},
    # One expression at several call sites: a channel test's outcome (notify/channel_services.py), single sign-on
    # turned off or handed back to the installation's settings (core/sso.set_status) and an export's end
    # (catalog/org_lifecycle._finish)
    "action": {
        "email_settings.tested",
        "teams_destination.tested",
        "webhook_destination.tested",
        "sso_settings.disabled",
        "sso_settings.legacy_restored",
        "organization.export_ready",
        "organization.export_failed",
    },
    # core/connections.py: a connection's writes and test, noun from its Kind
    "f'{kind.noun}.created'": {
        "microsoft_connection.created",
        "halo_connection.created",
        "okta_connection.created",
        "google_workspace_connection.created",
    },
    "f'{kind.noun}.updated'": {
        "microsoft_connection.updated",
        "halo_connection.updated",
        "okta_connection.updated",
        "google_workspace_connection.updated",
    },
    "f'{kind.noun}.deleted'": {
        "microsoft_connection.deleted",
        "halo_connection.deleted",
        "okta_connection.deleted",
        "google_workspace_connection.deleted",
    },
    "f'{kind.noun}.tested'": {
        "microsoft_connection.tested",
        "halo_connection.tested",
        "okta_connection.tested",
        "google_workspace_connection.tested",
    },
}


def written_actions():
    literal, dynamic = set(), {}
    for app in APPS:
        for path in (ROOT / app).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name not in AUDIT_FUNCTIONS:
                    continue
                arg = (
                    node.args[2]
                    if len(node.args) > 2
                    else next((k.value for k in node.keywords if k.arg == "action"), None)
                )
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literal.add(arg.value)
                else:
                    dynamic[ast.unparse(arg) if arg is not None else "<missing>"] = f"{path.name}:{node.lineno}"
    return literal, dynamic


def test_dynamic_reference_data_actions_match_the_nouns():
    from catalog.admin_services import NOUNS

    nouns = set(NOUNS.values())
    for verb in ("created", "updated", "deleted"):
        assert DYNAMIC[f"f'{{noun(model)}}.{verb}'"] == {f"{n}.{verb}" for n in nouns}


def test_the_scan_finds_audit_calls():
    literal, _ = written_actions()
    assert {"resource.created", "user.organization_switched", "user.removed", "user.password_changed"} <= literal


def test_every_dynamic_action_is_listed():
    _, dynamic = written_actions()
    unknown = {expr: where for expr, where in dynamic.items() if expr not in DYNAMIC}
    assert unknown == {}, "List each value of these actions in DYNAMIC (and give each a label)"


def test_every_written_action_has_a_label():
    literal, dynamic = written_actions()
    actions = literal | {value for expr in dynamic for value in DYNAMIC.get(expr, ())}
    assert sorted(actions - set(LABELS)) == []


def test_a_tag_change_is_summarised_in_words(a):
    """A set such as tags reads as its entries, not a Python list ("['a'] → ['b']")."""
    from catalog.audit_log import Names, summary
    from core.models import AuditEvent

    event = AuditEvent(organization=a["org"], action="resource.updated", changes={"tags": [["a"], ["b"]]})
    assert summary(event, Names(a["org"], [event])) == "Tags: a → b"
    both = AuditEvent(organization=a["org"], action="resource.updated", changes={"tags": [["a", "c"], []]})
    assert summary(both, Names(a["org"], [both])) == "Tags: a, c → not set"
