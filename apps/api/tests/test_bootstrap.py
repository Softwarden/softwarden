"""CLI bootstrap and break-glass reset."""

import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from core.models import AuditEvent, Membership, Organization, Role
from tests.conftest import Api

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("fresh")]  # whatever ran before

PASSWORD = "tangerine-orbit-ladder"
OPTIONS = {
    "org_name": "Fabrikam (synthetic)",
    "timezone": "America/Chicago",
    "email": "Ana@Fabrikam.test",
    "name": "Ana Silva",
}


def bootstrap(monkeypatch, password=PASSWORD, **overrides):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", password)
    out = io.StringIO()
    call_command("bootstrap_admin", interactive=False, stdout=out, **{**OPTIONS, **overrides})
    return out.getvalue()


def test_bootstrap_creates_org_admin_and_coordinator(monkeypatch):
    bootstrap(monkeypatch)
    org = Organization.objects.get()
    membership = Membership.objects.get()
    user = membership.user
    assert (org.name, org.timezone) == ("Fabrikam (synthetic)", "America/Chicago")
    assert (user.username, user.first_name, user.last_name) == ("ana@fabrikam.test", "Ana", "Silva")
    assert membership.organization == org and membership.role == Role.ADMIN
    assert org.coordinator == user
    assert membership.must_change_password is False  # they just chose it
    assert user.check_password(PASSWORD)


def test_bootstrap_audits_without_the_password(monkeypatch):
    bootstrap(monkeypatch)
    event = AuditEvent.objects.get(action="user.created")
    assert event.actor is None and event.changes["role"] == "admin"
    assert PASSWORD not in json.dumps(event.changes, default=str)


@pytest.mark.parametrize("password", ["short-pass1", "password1234"])
def test_bootstrap_refuses_weak_passwords_and_explains(monkeypatch, password):
    with pytest.raises(CommandError, match="password"):
        bootstrap(monkeypatch, password=password)
    assert not Organization.objects.exists() and not get_user_model().objects.exists()


def test_bootstrap_refuses_bad_timezone(monkeypatch):
    with pytest.raises(CommandError, match="timezone"):
        bootstrap(monkeypatch, timezone="Mars/Olympus")
    assert not Organization.objects.exists()


def test_bootstrap_refuses_once_an_organization_exists(monkeypatch):
    bootstrap(monkeypatch)
    with pytest.raises(CommandError, match="reset_admin"):
        bootstrap(monkeypatch, email="second@fabrikam.test")
    assert get_user_model().objects.count() == 1


def test_unattended_bootstrap_reads_email_and_name_from_the_environment(monkeypatch):
    """A Helm Job sets BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_NAME instead of passing --email and --name."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "Env@Fabrikam.test")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Eve Nvar")
    bootstrap(monkeypatch, email=None, name=None)
    user = Membership.objects.get().user
    assert (user.email, user.first_name, user.last_name) == ("env@fabrikam.test", "Eve", "Nvar")


def test_an_option_wins_over_its_environment_variable(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "env@fabrikam.test")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Eve Nvar")
    bootstrap(monkeypatch)
    user = Membership.objects.get().user
    assert (user.email, user.first_name) == ("ana@fabrikam.test", "Ana")


def test_unattended_bootstrap_names_the_options_and_variables_it_is_missing(monkeypatch):
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_NAME", raising=False)
    with pytest.raises(CommandError) as raised:
        bootstrap(monkeypatch, password="", org_name=None, timezone=None, email=None, name=None)
    message = str(raised.value)
    assert "Nothing was created" in message
    for source in ("--org-name", "--timezone", "--email or BOOTSTRAP_ADMIN_EMAIL", "--name or BOOTSTRAP_ADMIN_NAME"):
        assert source in message, source
    assert "password:" in message and "BOOTSTRAP_ADMIN_PASSWORD" in message
    assert not Organization.objects.exists() and not get_user_model().objects.exists()


def test_the_help_names_the_environment_variables():
    from core.management.commands.bootstrap_admin import Command

    help_text = Command().create_parser("manage.py", "bootstrap_admin").format_help()
    for name in ("BOOTSTRAP_ADMIN_PASSWORD", "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_NAME"):
        assert name in help_text, name


def test_bootstrap_prompts_interactively(monkeypatch):
    answers = iter(["Fabrikam (synthetic)", "UTC", "ana@fabrikam.test", "Ana Silva"])
    passwords = iter(["too-short", PASSWORD, "typo-" + PASSWORD, PASSWORD, PASSWORD])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: next(passwords))
    call_command("bootstrap_admin", create=True, stdout=io.StringIO(), stderr=io.StringIO())
    assert Membership.objects.get().user.check_password(PASSWORD)


def test_bootstrapped_admin_can_sign_in(monkeypatch):
    bootstrap(monkeypatch)
    response = Api().post("auth/login", {"email": "ana@fabrikam.test", "password": PASSWORD})
    assert response.status_code == 200


def reset(email):
    out = io.StringIO()
    call_command("reset_admin", email, stdout=out)
    return out.getvalue().split(": ", 1)[1].splitlines()[0]


def test_reset_admin_sets_temporary_password_and_forces_change(monkeypatch):
    bootstrap(monkeypatch)
    temp = reset("ANA@fabrikam.test")
    membership = Membership.objects.get()
    assert membership.must_change_password is True
    assert membership.user.check_password(temp) and not membership.user.check_password(PASSWORD)
    event = AuditEvent.objects.get(action="user.password_reset")
    assert event.actor is None and temp not in json.dumps(event.changes, default=str)


def test_reset_admin_reactivates_a_deactivated_admin(monkeypatch):
    bootstrap(monkeypatch)
    get_user_model().objects.update(is_active=False)
    reset("ana@fabrikam.test")
    assert Membership.objects.get().user.is_active


def test_reset_admin_refuses_non_admins_and_unknown_emails(a):
    with pytest.raises(CommandError, match="No Admin"):
        reset(a["users"]["editor"].email)
    with pytest.raises(CommandError, match="No Admin"):
        reset("nobody@example.test")
    assert not Membership.objects.filter(must_change_password=True).exists()


def test_reactivate_account_turns_on_a_deactivated_account_with_no_memberships_and_audits_it(a):
    """The operator's command, refusing accounts with memberships."""
    from io import StringIO

    from django.contrib.auth import get_user_model
    from django.core.management import CommandError, call_command

    from catalog import services as catalog_services
    from core.models import AuditEvent, Membership

    user = get_user_model().objects.create_user(username="left@orga.test", email="left@orga.test", is_active=False)
    catalog_services.audit(a["org"], None, "user.removed", None, {"user": user.id, "role": "viewer"})
    out = StringIO()
    call_command("reactivate_account", "LEFT@orga.test", stdout=out)
    user.refresh_from_db()
    assert user.is_active and "Reactivated" in out.getvalue() and a["org"].slug in out.getvalue()
    event = AuditEvent.objects.get(action="user.updated", organization=a["org"])
    assert event.actor is None and event.changes == {"user": user.id, "active": [False, True], "via": "operator"}
    with pytest.raises(CommandError, match="already active"):
        call_command("reactivate_account", "left@orga.test")
    member = a["users"]["viewer"]
    member.is_active = False
    member.save()
    assert Membership.objects.filter(user=member).exists()
    with pytest.raises(CommandError, match="belongs to an organization"):
        call_command("reactivate_account", member.username)
    with pytest.raises(CommandError, match="No account"):
        call_command("reactivate_account", "nobody@orga.test")
