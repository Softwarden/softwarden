"""A cloud organization's invitation emails through the installation's mail server have a budget, and its name stays
out of their Subject (core/limits.py `invitation_emails_hour` / `_day`,
catalog/invitations.reserve_email, core.models.InstallationEmail).

The budget counts the messages an organization had sent through the installation's transport in the last hour and day
from the database (InstallationEmail rows), so every app process sees the same count. Lower until a second person
joins; platform staff raise it with a limit override (audited)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.utils import timezone

from catalog import invitations, platform_admin
from core import limits
from core.capabilities import set_edition
from core.models import AuditEvent, InstallationEmail, Invitation, Membership, PlatformAuditEvent, Role


@pytest.fixture(autouse=True)
def fresh_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    return settings


@pytest.fixture
def cloud(db):
    """A brand-new cloud organization: its Admin alone (signup's shape)."""
    from django.contrib.auth import get_user_model

    from core import capabilities

    admin = get_user_model().objects.create_user(username="owner@spam.test", email="owner@spam.test", password="x" * 20)
    org = capabilities.create_signup_organization(name="Spam Co", timezone="UTC", coordinator=admin)
    Membership.objects.create(user=admin, organization=org, role=Role.ADMIN)
    return {"org": org, "admin": admin}


def invite_many(api, admin, count: int, prefix="target"):
    client = api(admin)
    return [client.post("invitations", {"email": f"{prefix}{i}@victims.test", "role": "viewer"}) for i in range(count)]


# ---- the acceptance -----------------------------------------------------------------------------------------------


def test_thirty_invitations_from_a_new_cloud_organization_stop_at_the_budget(cloud, api, email_on):
    """30 invitations from a cloud organization on the installation transport are refused with
    /problems/limit-reached after the budget, and the outbox holds no more than the budget."""
    budget = limits.NEW_ORGANIZATION_BUDGET[limits.INVITATION_EMAILS_HOUR]
    responses = invite_many(api, cloud["admin"], 30)
    assert [r.status_code for r in responses] == [201] * budget + [409] * (30 - budget)
    refused = responses[budget].json_body
    assert refused["type"] == "/problems/limit-reached" and refused["limit"] == limits.INVITATION_EMAILS_HOUR
    assert refused["maximum"] == budget and refused["usage"] == budget
    assert len(mail.outbox) == budget <= 5
    assert Invitation.objects.count() == budget  # nothing is made past the budget
    assert InstallationEmail.objects.filter(organization=cloud["org"]).count() == budget
    # A resend is a message too.
    assert api(cloud["admin"]).post(f"invitations/{responses[0].json_body['id']}/resend").status_code == 409


def test_once_someone_joined_the_budget_is_twenty_an_hour_and_a_hundred_a_day(cloud, api, email_on):
    from django.contrib.auth import get_user_model

    colleague = get_user_model().objects.create_user(username="col@spam.test", email="col@spam.test")
    Membership.objects.create(user=colleague, organization=cloud["org"], role=Role.VIEWER)
    assert limits.effective(cloud["org"], limits.INVITATION_EMAILS_HOUR) == 20
    assert limits.effective(cloud["org"], limits.INVITATION_EMAILS_DAY) == 100
    responses = invite_many(api, cloud["admin"], 30)
    assert [r.status_code for r in responses].count(201) == 20 and len(mail.outbox) == 20
    # An hour later the hourly budget is back, and the day's counts on to 100.
    InstallationEmail.objects.update(created_at=timezone.now() - timedelta(hours=1, seconds=1))
    sent = invite_many(api, cloud["admin"], 20, prefix="later")
    assert [r.status_code for r in sent] == [201] * 20
    InstallationEmail.objects.filter(created_at__gt=timezone.now() - timedelta(minutes=5)).update(
        created_at=timezone.now() - timedelta(hours=2)
    )
    InstallationEmail.objects.bulk_create(
        [InstallationEmail(organization=cloud["org"], kind="invitation") for _ in range(60)]
    )
    InstallationEmail.objects.update(created_at=timezone.now() - timedelta(hours=3))
    refused = api(cloud["admin"]).post("invitations", {"email": "day@victims.test", "role": "viewer"})
    assert refused.status_code == 409 and refused.json_body["limit"] == limits.INVITATION_EMAILS_DAY


def test_an_organization_name_with_a_url_never_reaches_an_installation_subject(cloud, api, email_on):
    """An organization name containing https:// never appears in an installation-transport Subject."""
    org = cloud["org"]
    org.name = "ACTION REQUIRED: your password expires today - reset at https://evil.example/reset"
    org.save(update_fields=["name"])
    assert (
        api(cloud["admin"]).post("invitations", {"email": "victim@victims.test", "role": "viewer"}).status_code == 201
    )
    [message] = mail.outbox
    assert "https://" not in message.subject and "evil" not in message.subject
    assert message.subject == invitations.INSTALLATION_SUBJECT
    # Review follow-up: nor in the body, where a name that reads as a web address becomes a neutral phrase.
    assert "evil" not in message.body and "join an organization on Softwarden" in message.body


def test_an_ordinary_organization_name_stays_in_the_body(cloud, api, email_on):
    assert api(cloud["admin"]).post("invitations", {"email": "v@victims.test", "role": "viewer"}).status_code == 201
    assert "join Spam Co on Softwarden" in mail.outbox[-1].body


def test_an_inviter_name_with_a_url_never_reaches_an_installation_email(cloud, api, email_on):
    """Review follow-up: the inviter's display name is the customer's text too; one that reads as a web address is
    replaced by the inviter's sign-in address. An ordinary name stays."""
    admin = cloud["admin"]
    admin.first_name, admin.last_name = "Reset", "your password at https://evil.example/reset"
    admin.save(update_fields=["first_name", "last_name"])
    assert api(admin).post("invitations", {"email": "v@victims.test", "role": "viewer"}).status_code == 201
    body = mail.outbox[-1].body
    assert "evil" not in body and body.startswith(f"{admin.email} invited you")
    admin.first_name, admin.last_name = "Olive", "Owner"
    admin.save(update_fields=["first_name", "last_name"])
    assert api(admin).post("invitations", {"email": "w@victims.test", "role": "viewer"}).status_code == 201
    assert mail.outbox[-1].body.startswith("Olive Owner invited you")


def test_the_organizations_own_settings_cant_name_the_installations_mail_server(cloud, settings):
    """Review follow-up: the installation's SMTP host typed in as the organization's own would send through the
    service's server without counting against the budget."""
    from core.problems import FieldErrors
    from notify import channel_services

    settings.EMAIL_HOST = "smtp.service.test"
    for host in ("smtp.service.test", "SMTP.Service.Test"):
        with pytest.raises(FieldErrors) as refused:
            channel_services.update_email_settings(
                cloud["org"],
                cloud["admin"],
                {"transport": "smtp", "smtp_host": host, "from_address": "it@spam.test"},
            )
        assert "service's own mail server" in refused.value.errors["smtp_host"][0]


def test_control_characters_leave_the_subject_on_the_organizations_own_server(a, api, email_on, monkeypatch):
    """Review follow-up (restored from test_signup): on the organization's own server the Subject keeps its name, on
    one line."""
    from core.models import Organization
    from tests.test_invitation_mailbox import own_mail_server

    sent = own_mail_server(a, monkeypatch)
    Organization.objects.filter(pk=a["org"].pk).update(name="Orga\r\nBcc: evil@example.test")
    assert api(a["users"]["admin"]).post("invitations", {"email": "c@orga.test", "role": "viewer"}).status_code == 201
    [delivery] = sent
    assert "\n" not in delivery.subject and "\r" not in delivery.subject
    assert delivery.subject == "You're invited to Orga Bcc: evil@example.test on Softwarden"


def test_the_organizations_own_mail_server_has_no_budget_and_keeps_the_name(cloud, api, email_on, monkeypatch):
    from notify import channels, transactional

    sent: list[str] = []
    monkeypatch.setattr(channels, "effective_transport", lambda row: "smtp")
    monkeypatch.setattr(channels, "email_configured", lambda org, row=None: True)

    def send_for_org(org, kind, to, subject, text) -> bool:
        sent.append(subject)
        return True

    monkeypatch.setattr(transactional, "send_for_org", send_for_org)
    responses = invite_many(api, cloud["admin"], 8)
    assert {r.status_code for r in responses} == {201}
    assert not InstallationEmail.objects.exists()
    assert sent and all("Spam Co" in s for s in sent)


def test_community_and_enterprise_organizations_have_no_budget(a, api, email_on):
    responses = invite_many(api, a["users"]["admin"], 25)
    assert {r.status_code for r in responses} == {201} and len(mail.outbox) == 25
    set_edition(a["org"], "enterprise")
    assert limits.effective(a["org"], limits.INVITATION_EMAILS_HOUR) is None


def test_without_email_nothing_is_counted(cloud, api, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    responses = invite_many(api, cloud["admin"], 8)
    assert {r.status_code for r in responses} == {201} and not InstallationEmail.objects.exists()


def test_platform_staff_raise_the_budget_audited(cloud, api, email_on):
    from django.contrib.auth import get_user_model

    from core.models import PlatformStaff

    staff = get_user_model().objects.create_user(username="ops@platform.test", email="ops@platform.test")
    PlatformStaff.objects.create(user=staff)
    platform_admin.set_limit(
        staff, cloud["org"], {"limit": limits.INVITATION_EMAILS_HOUR, "value": 12, "reason": "Onboarding a team"}
    )
    cloud["org"].refresh_from_db()
    assert limits.effective(cloud["org"], limits.INVITATION_EMAILS_HOUR) == 12  # the override, not the new-org cap
    responses = invite_many(api, cloud["admin"], 14)
    assert [r.status_code for r in responses].count(201) == 12
    assert AuditEvent.objects.filter(organization=cloud["org"], action="organization.limit_set").exists()
    assert PlatformAuditEvent.objects.filter(action="limit.set").exists()


def test_the_limits_report_shows_the_budget_and_its_usage(cloud, api, email_on):
    invite_many(api, cloud["admin"], 2)
    rows = {r["name"]: r for r in api(cloud["admin"]).get("organization/limits").json_body["limits"]}
    hour = rows[limits.INVITATION_EMAILS_HOUR]
    assert hour["limit"] == limits.NEW_ORGANIZATION_BUDGET[limits.INVITATION_EMAILS_HOUR] and hour["usage"] == 2
    assert hour["default"] == 20 and rows[limits.INVITATION_EMAILS_DAY]["default"] == 100
