"""Serializers for accounts: the signed-in user (`auth/me`, sign-in, setup), members of the organization (`users`),
organization settings and password change (catalog/views.py, catalog/account_viewsets.py, core/setup_views.py).
Output serializers are read-only (core/drf.ReadOnlySerializer); input serializers only describe request bodies,
because catalog/admin_services.py validates every write and owns its messages.

Passwords are write-only: a temporary password appears once, in an Admin's reset response, and nothing returns a
password hash. An invitation's link appears once, in the create or resend response; its token is stored hashed."""

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db.models import Count
from drf_spectacular.utils import extend_schema_field
from rest_framework import exceptions, serializers

from core import access, simulation
from core.capabilities import COMPLIANCE_PROFILE, capabilities, ee_installed, has_capability
from core.drf import ReadOnlySerializer, UserBriefSerializer, timestamp
from core.limits import NAMES
from core.models import Edition, ExportStatus, Role
from core.platform_access import is_platform_staff
from notify.models import DigestRun

from . import compliance, quality

# ---- the signed-in user -------------------------------------------------------------------------------------------


class OrganizationBriefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    timezone = serializers.CharField(read_only=True, help_text="Dates default to the organization's today.")


class MembershipBriefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(source="organization_id", read_only=True, help_text="The organization's ID.")
    name = serializers.CharField(source="organization.name", read_only=True)
    role = serializers.ChoiceField(Role.choices, read_only=True, help_text="Your role in that organization.")


class FinalExportSerializer(serializers.Serializer):
    status = serializers.ChoiceField(ExportStatus.choices)
    expires_at = serializers.DateTimeField(allow_null=True)
    download_url = serializers.CharField(allow_null=True, help_text="While it is ready and not expired.")


class OrganizationDeletionSerializer(ReadOnlySerializer):
    """An organization scheduled for deletion (catalog/org_lifecycle.py): the banner every member sees."""

    requested_at = serializers.DateTimeField(read_only=True)
    due_at = serializers.DateTimeField(
        read_only=True,
        help_text="When every record, file and the audit log are removed; an Admin can cancel until then.",
    )
    requested_by = UserBriefSerializer(read_only=True, allow_null=True)
    final_export = FinalExportSerializer(
        read_only=True,
        allow_null=True,
        help_text="Admins only (null for others, and before one exists): the latest export made for the deletion, so "
        "the banner can say it is ready to download or that it failed.",
    )


def deletion_of(org, role: str | None = None) -> dict[str, Any] | None:
    """{requested_at, due_at, requested_by, final_export} while `org` is scheduled for deletion, else None. The final
    export is shown to Admins only."""
    if org.deletion_due_at is None:
        return None
    final = None
    if role == Role.ADMIN:
        from . import org_lifecycle

        export = org_lifecycle.deletion_export(org)
        if export is not None:
            final = {
                "status": export.status,
                "expires_at": export.expires_at,
                "download_url": org_lifecycle.download_url(export),
            }
    return {
        "requested_at": org.deletion_requested_at,
        "due_at": org.deletion_due_at,
        "requested_by": org.deletion_requested_by,
        "final_export": final,
    }


@dataclass
class Me:
    """What `auth/me` describes: the account, the current membership and every membership it can switch to."""

    user: Any
    role: str
    must_change_password: bool
    organization: Any
    organizations: list[Any]
    digest_problem: Any = None
    webhook_problem: Any = None
    capabilities: list[str] = field(default_factory=list)
    platform_staff: bool = False
    impersonation: dict[str, Any] | None = None
    simulation_allowed: bool = False

    @property
    def deployment(self) -> str:
        """The installation mode (SOFTWARDEN_DEPLOYMENT): "cloud" for the hosted service, else "selfhosted"."""
        return "cloud" if settings.SOFTWARDEN_CLOUD else "selfhosted"

    @property
    def organization_suspended(self) -> dict[str, Any] | None:
        if self.organization.suspended_at is None:
            return None
        return {"suspended_at": self.organization.suspended_at}

    @property
    def current_organization(self):
        return self.organization.id

    @property
    def edition(self) -> str:
        return self.organization.edition

    @property
    def organization_deletion(self) -> dict[str, Any] | None:
        return deletion_of(self.organization, self.role)


class OrganizationSuspendedSerializer(serializers.Serializer):
    suspended_at = serializers.DateTimeField()


class ImpersonationBannerSerializer(serializers.Serializer):
    id = serializers.UUIDField(
        help_text="This impersonation. Send it as X-Softwarden-Impersonation on writes, so a tab left open from "
        "another impersonation, or from before Stop, is refused (409 /problems/impersonation-ended)."
    )
    by = UserBriefSerializer(help_text="The platform staff account acting as you.")
    reason = serializers.CharField()
    started_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()


class DisabledWebhookSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    disabled_at = serializers.DateTimeField()
    reason = serializers.CharField()


class WebhookProblemSerializer(serializers.Serializer):
    disabled = serializers.IntegerField(help_text="How many endpoints are off.")
    endpoints = DisabledWebhookSerializer(many=True, help_text="The latest 10, newest first.")


class MeSerializer(ReadOnlySerializer):
    user = UserBriefSerializer(read_only=True)
    role = serializers.ChoiceField(Role.choices, read_only=True, help_text="Your role in the current organization.")
    must_change_password = serializers.BooleanField(read_only=True)
    organization = OrganizationBriefSerializer(read_only=True, help_text="The current organization.")
    current_organization = serializers.UUIDField(read_only=True, help_text="The current organization's ID.")
    organizations = MembershipBriefSerializer(
        many=True, read_only=True, help_text="Every organization you belong to (an API token sees only its own)."
    )
    digest_problem = serializers.JSONField(
        read_only=True, allow_null=True, help_text="Admins only: the latest digest run's failures."
    )
    webhook_problem = WebhookProblemSerializer(
        read_only=True,
        allow_null=True,
        help_text="Admins only: webhook endpoints Softwarden turned off (a 410 Gone or too many failed deliveries).",
    )
    edition = serializers.ChoiceField(
        Edition.choices, read_only=True, help_text="The current organization's edition; shown, not for gating."
    )
    capabilities = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="Capability names enabled for the current organization, sorted (docs/CAPABILITIES.md). Gate on "
        "these, never on the edition.",
    )
    organization_deletion = OrganizationDeletionSerializer(
        read_only=True,
        allow_null=True,
        help_text="Set while the current organization is scheduled for deletion (show every member a banner); null "
        "otherwise.",
    )
    organization_suspended = OrganizationSuspendedSerializer(
        read_only=True,
        allow_null=True,
        help_text="Set while the service operator has suspended the current organization: every other request is "
        "refused with /problems/organization-suspended. Null otherwise.",
    )
    platform_staff = serializers.BooleanField(
        read_only=True, help_text="Whether you are platform staff (the console at /api/v1/platform/)."
    )
    deployment = serializers.ChoiceField(
        ["selfhosted", "cloud"],
        read_only=True,
        help_text="The installation mode: cloud on the hosted service, else selfhosted. For wording that names who "
        "can act (an operator's setting or support), not for gating features.",
    )
    impersonation = ImpersonationBannerSerializer(
        read_only=True,
        allow_null=True,
        help_text="Set while platform staff act as you in this session: show a permanent banner with who, why and "
        "until when. Null otherwise.",
    )
    simulation_allowed = serializers.BooleanField(
        read_only=True,
        help_text="Whether this server accepts the simulated tenants and hosts (the tenant ID `simulated`, "
        "`*.simulated.invalid`; core/simulation.py). Show the hints that suggest them only when true.",
    )


def me_of(request) -> Me:
    """The signed-in user and the current organization (as before multi-organization membership, `role`,
    `must_change_password` and `organization` describe the current one). 403 without a membership."""
    current, rows = access.resolve(request)
    if current is None:
        raise exceptions.PermissionDenied("Your account isn't part of an organization.")
    org = current.organization
    # About the account: every membership counts, also in an SSO session confined to one (core/access.py).
    must_change = access.must_change_password(
        rows
        if not (access.sso_confinement(request) or access.impersonation_confinement(request))
        else access.memberships_of(request.user)
    )
    if access.pinned_organization_id(request) is not None:
        rows = [current]  # an API token belongs to one organization and sees only that one
    problem = webhook_problem = None
    if current.role == Role.ADMIN:  # Admins see a banner while the latest digest run has failures
        from notify import webhooks
        from notify.digest import digest_problem

        problem = digest_problem(org)
        webhook_problem = webhooks.problem(org)  # endpoints turned off after failures
    return Me(
        user=request.user,
        role=current.role,
        must_change_password=must_change,
        organization=org,
        organizations=sorted(rows, key=lambda m: (m.organization.name.lower(), str(m.organization_id))),
        digest_problem=problem,
        webhook_problem=webhook_problem,
        capabilities=sorted(capabilities(org)),  # the SPA gates on capability names, never on the edition
        platform_staff=is_platform_staff(request.user),
        impersonation=impersonation_banner(request),
        simulation_allowed=simulation.allowed(),
    )


def impersonation_banner(request) -> dict[str, Any] | None:
    """`auth/me` impersonation: {id, by, reason, started_at, expires_at} while platform staff act as this member."""
    django_request = getattr(request, "_request", request)
    state = getattr(django_request, "softwarden_impersonation", None)
    staff = getattr(django_request, "softwarden_impersonator", None)
    if state is None or staff is None:
        return None
    return {
        "id": state.get("id"),
        "by": staff,
        "reason": state.get("reason", ""),
        "started_at": state.get("started_at"),
        "expires_at": state.get("expires_at"),
    }


def me_body(request) -> dict[str, Any]:
    return MeSerializer(me_of(request)).data


class LoginInputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(help_text="Never returned or logged.")


class SwitchOrganizationInputSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField(help_text="One of your organizations.")


class PasswordInputSerializer(serializers.Serializer):
    current_password = serializers.CharField()
    new_password = serializers.CharField(help_text="12 characters or more, not a common password.")


# ---- members of the organization ------------------------------------------------------------------------------------


class UserIdentitySerializer(ReadOnlySerializer):
    """A pinned single sign-on identity (core.models.UserIdentity), without its subject."""

    id = serializers.IntegerField(read_only=True)
    provider = serializers.SerializerMethodField(help_text="SAML, Entra, Okta, Google, or the issuer's host.")
    issuer = serializers.SerializerMethodField(help_text="The issuer; for SAML, the identity provider's entity ID.")
    last_login_at = serializers.DateTimeField(read_only=True, allow_null=True)

    def get_provider(self, identity) -> str:
        from core.oidc_discovery import provider_label

        return provider_label(identity.issuer)

    def get_issuer(self, identity) -> str:
        from core.oidc_discovery import display_issuer

        return display_issuer(identity.issuer)


class MemberSerializer(ReadOnlySerializer):
    """One member. `must_change_password` is about the account (any membership). For an account that also belongs to
    another organization, `last_login` and `entra_linked` are null: they describe activity outside this organization.
    Context: `shared` and `must_change` (catalog/admin_services.account_facts) and `linked`, the user IDs with an Entra
    identity, and `identities`, {user ID: [UserIdentity]}, computed once per page."""

    id = serializers.IntegerField(source="user_id", read_only=True, help_text="The user's ID.")
    name = serializers.SerializerMethodField()
    email = serializers.CharField(source="user.email", read_only=True)
    role = serializers.ChoiceField(Role.choices, read_only=True)
    active = serializers.BooleanField(source="user.is_active", read_only=True)
    must_change_password = serializers.SerializerMethodField()
    last_login = serializers.SerializerMethodField(help_text="Null for an account shared with another organization.")
    entra_linked = serializers.SerializerMethodField(help_text="Null for an account shared with another organization.")
    has_password = serializers.SerializerMethodField(
        help_text="Whether the account has a password. False: it signs in only through single sign-on (made by "
        "pending-access approval or SCIM), so POST users/{id}/reset-password refuses it (409). Null for an account "
        "shared with another organization."
    )

    identities = serializers.SerializerMethodField(
        help_text="The account's single sign-on identities (unlink one with DELETE users/{id}/identities/"
        "{identity_id}). Null for an account shared with another organization. Never the subject: it identifies "
        "the person at the provider."
    )

    def get_name(self, m) -> str:
        return m.user.get_full_name() or m.user.email

    @extend_schema_field(UserIdentitySerializer(many=True, allow_null=True))
    def get_identities(self, m) -> list[dict[str, Any]] | None:
        if m.user_id in self.context["shared"]:
            return None
        return list(UserIdentitySerializer(self.context["identities"].get(m.user_id, []), many=True).data)

    def get_must_change_password(self, m) -> bool:
        return m.user_id in self.context["must_change"]

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_login(self, m) -> str | None:
        if m.user_id in self.context["shared"] or not m.user.last_login:
            return None
        return timestamp(m.user.last_login)

    def get_entra_linked(self, m) -> bool | None:
        return None if m.user_id in self.context["shared"] else m.user_id in self.context["linked"]

    def get_has_password(self, m) -> bool | None:
        return None if m.user_id in self.context["shared"] else m.user.has_usable_password()


class MemberWithPasswordSerializer(MemberSerializer):
    temporary_password = serializers.CharField(
        read_only=True, help_text="Shown once, in the reset response only; never stored or logged."
    )


class InviteInputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    name = serializers.CharField(
        required=False, help_text="A suggestion for a new account's name (first name, a space, then the rest)."
    )
    role = serializers.ChoiceField(Role.choices, help_text="The role the membership gets when they accept.")


INVITATION_STATUSES = ("pending", "expired", "accepted", "revoked")


class InvitationSerializer(ReadOnlySerializer):
    """An invitation: never its token or the token's hash. `status` is pending until it is accepted,
    revoked or past `expires_at`."""

    id = serializers.UUIDField(read_only=True)
    email = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    role = serializers.ChoiceField(Role.choices, read_only=True)
    status = serializers.SerializerMethodField()
    invited_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    accepted_at = serializers.DateTimeField(read_only=True, allow_null=True)
    accepted_by = UserBriefSerializer(read_only=True, allow_null=True)
    revoked_at = serializers.DateTimeField(read_only=True, allow_null=True)

    @extend_schema_field(serializers.ChoiceField(INVITATION_STATUSES))
    def get_status(self, invitation) -> str:
        from .invitations import status_of

        return status_of(invitation)


class InvitationCreatedSerializer(InvitationSerializer):
    link = serializers.CharField(
        read_only=True,
        help_text="The invitation link, {SOFTWARDEN_BASE_URL}/invite#<token> (the token in the fragment, so it never "
        "reaches a server log). In this response only: copy it now. "
        "Resending makes a new link and the old one stops working.",
    )
    emailed = serializers.BooleanField(
        read_only=True,
        help_text="Whether the link was also emailed to the invitee: only while the installation can send "
        "email (SOFTWARDEN_SMTP_HOST), and only when the server accepted the message. Copy the link either way.",
    )


class InvitationOrganizationSerializer(serializers.Serializer):
    name = serializers.CharField()
    slug = serializers.CharField()


class InvitationSsoSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(["entra", "okta", "google", "oidc", "saml"])
    label = serializers.CharField(  # type: ignore[assignment]  # the response member, not Field.label
        help_text="The provider's name for the button."
    )
    start_url = serializers.CharField(
        help_text="Single sign-on for the inviting organization, coming back to this invitation. It signs in only an "
        "account whose identity at that provider is already linked to the invited account."
    )


class InvitationOrganizationAccountSerializer(serializers.Serializer):
    organization = serializers.CharField(help_text="The organization whose single sign-on or SCIM made the account.")
    sign_in_url = serializers.CharField(help_text="Its sign-in page, /login?org=<slug>.")


class InvitationPreviewSerializer(serializers.Serializer):
    """What the invitation page shows before accepting (public, from the link alone)."""

    organization = InvitationOrganizationSerializer()
    email = serializers.CharField(help_text="The invited email, masked (j***@example.com).")
    role = serializers.ChoiceField(Role.choices)
    name = serializers.CharField(help_text="The name the Admin suggested for a new account; may be empty.")
    expires_at = serializers.DateTimeField()
    account_exists = serializers.BooleanField(
        allow_null=True,
        help_text="True: the email has an account, which accepts while signed in as itself (POST with no body). "
        "False: accepting creates the account (POST name and password). Null from a link that doesn't prove the "
        "mailbox (`can_create_account` false), which says nothing about the address (unless this browser is signed "
        "in as the invited account): offer signing in, and point a newcomer to the invitation email.",
    )
    can_create_account = serializers.BooleanField(
        help_text="This link can create a new account: it is the one emailed to the invited address, or the "
        "installation has one organization. False for the link the inviting Admin was shown on an installation with "
        "more than one organization (or in cloud mode): it joins an existing account signed in as itself, and "
        "accepting with it while nobody is signed in answers 403 /problems/invitation-emailed-link-required."
    )
    organization_account = InvitationOrganizationAccountSerializer(
        allow_null=True,
        help_text="From a link that proves the mailbox only: an organization made an account for this address through "
        "its single sign-on or SCIM, which never proved the mailbox. Creating an account here retires that one (it is "
        "deactivated in that organization); to keep using it, sign in through that organization instead. Null "
        "otherwise.",
    )
    session = serializers.ChoiceField(
        ["none", "invitee", "other_account", "other_sign_in"],
        help_text="Who this browser is signed in as: `none` (nobody), `invitee` (the invited account; accepting "
        "works), `other_account` (another account: sign out first; accepting answers 409 "
        "/problems/invitation-wrong-account), or `other_sign_in` (the invited account, signed in through another "
        "organization's single sign-on: sign in with a password or the inviting organization's single sign-on; "
        "accepting answers 409 /problems/invitation-other-sign-in).",
    )
    sso = InvitationSsoSerializer(
        allow_null=True,
        help_text="With account_exists (or from a link that doesn't prove the mailbox) and the inviting "
        "organization's single sign-on on: its button. Null otherwise; password sign-in (/login?next=/invite) always "
        "works.",
    )


class InvitationTokenInputSerializer(serializers.Serializer):
    token = serializers.CharField(
        help_text="The token from the invitation link's fragment ({SOFTWARDEN_BASE_URL}/invite#<token>)."
    )


class AcceptInvitationInputSerializer(InvitationTokenInputSerializer):
    name = serializers.CharField(required=False, help_text="A new account only: your name.")
    password = serializers.CharField(
        required=False, help_text="A new account only: 12 characters or more, not a common password. Never returned."
    )


# ---- pending access -------------------------------------------------------------------------------------


class PendingAccessSerializer(ReadOnlySerializer):
    """An identity waiting for access. Never the subject."""

    id = serializers.UUIDField(read_only=True)
    email = serializers.CharField(read_only=True, help_text="As the identity provider sent it; may be empty.")
    name = serializers.CharField(read_only=True)
    issuer = serializers.SerializerMethodField(help_text="The issuer; for SAML, the identity provider's entity ID.")
    provider = serializers.SerializerMethodField(help_text="SAML, Entra, Okta, Google, or the issuer's host.")
    first_seen = serializers.DateTimeField(read_only=True)
    last_seen = serializers.DateTimeField(read_only=True)

    def get_provider(self, row) -> str:
        from core.oidc_discovery import provider_label

        return provider_label(row.issuer)

    def get_issuer(self, row) -> str:
        from core.oidc_discovery import display_issuer

        return display_issuer(row.issuer)


class ApprovePendingInputSerializer(serializers.Serializer):
    role = serializers.ChoiceField(
        Role.choices, help_text="The new membership's role (left as it is for someone already a member)."
    )


class MemberInputSerializer(serializers.Serializer):
    role = serializers.ChoiceField(Role.choices, required=False)
    active = serializers.BooleanField(
        required=False, help_text="Refused for an account shared with another organization."
    )


# ---- organization settings ----------------------------------------------------------------------------------------


class LastDigestRunSerializer(serializers.Serializer):
    for_date = serializers.DateField()
    finished_at = serializers.DateTimeField(allow_null=True)
    sent = serializers.IntegerField()
    failed = serializers.IntegerField()
    pending = serializers.IntegerField()
    skipped = serializers.IntegerField()


class EmailStatusSerializer(serializers.Serializer):
    configured = serializers.BooleanField(
        help_text="Whether the organization can send email: its email settings (Graph or SMTP) are filled in, or the "
        "installation's SMTP settings apply."
    )
    last_run = LastDigestRunSerializer(allow_null=True, help_text="The latest digest run's outcome.")


def email_status(org) -> dict[str, Any]:
    from notify import channels
    from notify.digest import status_word

    run = DigestRun.objects.filter(organization=org).order_by("-for_date").first()
    last = None
    if run is not None:
        counts: dict[str, int] = {}
        for status, n in run.digests.values_list("delivery__status").annotate(n=Count("id")):
            word = status_word(status)
            counts[word] = counts.get(word, 0) + n
        last = {
            "for_date": run.for_date.isoformat(),
            "finished_at": timestamp(run.finished_at),
            **{status: counts.get(status, 0) for status in ("sent", "failed", "pending", "skipped")},
        }
    return {"configured": channels.email_configured(org), "last_run": last}


class AttachmentLimitValuesSerializer(serializers.Serializer):
    max_size_mb = serializers.IntegerField(help_text="The largest file, in MB.")
    allowed_extensions = serializers.ListField(child=serializers.CharField(), help_text="Such as pdf, docx, png.")
    quota_mb = serializers.IntegerField(allow_null=True, help_text="Total stored, in MB; null means no quota.")


class AttachmentLimitsSerializer(AttachmentLimitValuesSerializer):
    used_bytes = serializers.IntegerField(help_text="What the organization's files take now.")
    retention_days = serializers.IntegerField(
        help_text="Days a removed file is kept before the nightly sweep deletes it (SOFTWARDEN_ATTACHMENT_RETENTION_DAYS)."
    )
    installation = AttachmentLimitValuesSerializer(
        help_text="The installation's limits (environment settings); the organization's can only be tighter."
    )


def attachment_limits(org) -> dict[str, Any]:
    from . import attachments

    def values(limits) -> dict[str, Any]:
        return {
            "max_size_mb": limits.max_size_mb,
            "allowed_extensions": list(limits.extensions),
            "quota_mb": limits.quota_mb,
        }

    return {
        **values(attachments.limits_for(org)),
        "used_bytes": attachments.used_bytes(org),
        "retention_days": settings.ATTACHMENT_RETENTION_DAYS,
        "installation": values(attachments.installation_limits()),
    }


class ScimRoleGroupsSerializer(ReadOnlySerializer):
    admin = serializers.UUIDField(read_only=True, allow_null=True)
    editor = serializers.UUIDField(read_only=True, allow_null=True)


class OrganizationSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True, help_text="Set on creation; never changes.")
    edition = serializers.ChoiceField(
        Edition.choices, read_only=True, help_text="Read-only here; an operator sets it (manage.py set_edition)."
    )
    timezone = serializers.CharField(read_only=True)
    review_interval_days = serializers.IntegerField(read_only=True)
    coordinator = UserBriefSerializer(read_only=True, allow_null=True)
    renewal_lead_days = serializers.IntegerField(read_only=True)
    reminder_thresholds = serializers.ListField(child=serializers.IntegerField(), read_only=True)
    digest_time = serializers.SerializerMethodField(help_text="HH:MM in the organization's timezone.")
    email = serializers.SerializerMethodField()
    compliance_profile_available = serializers.SerializerMethodField(
        help_text="Whether the edition includes the compliance profile (capability compliance_profile)."
    )
    compliance_profile_enabled = serializers.SerializerMethodField(
        help_text="Whether records carry the compliance fields now: the edition includes the profile and an Admin "
        "turned it on."
    )

    def get_digest_time(self, org) -> str:
        return org.digest_time.strftime("%H:%M")

    def get_compliance_profile_available(self, org) -> bool:
        return has_capability(org, COMPLIANCE_PROFILE)

    deletion = serializers.SerializerMethodField(
        help_text="Set while the organization is scheduled for deletion; null otherwise. "
        "GET /organization/deletion has the detail."
    )

    @extend_schema_field(OrganizationDeletionSerializer(allow_null=True))
    def get_deletion(self, org) -> dict[str, Any] | None:
        found = deletion_of(org, self.context.get("role"))
        return OrganizationDeletionSerializer(found).data if found is not None else None

    def get_compliance_profile_enabled(self, org) -> bool:
        return compliance.enabled(org)

    data_quality_disabled_rules = serializers.SerializerMethodField(
        help_text="Data-quality rules turned off for this organization, by id (GET /data-quality/rules)."
    )

    def get_data_quality_disabled_rules(self, org) -> list[str]:
        return sorted(quality.disabled_ids(org))

    inactive_seat_days = serializers.SerializerMethodField(
        help_text="License waste: a license whose holder hasn't signed in successfully for this many days is inactive "
        "(default 90)."
    )

    def get_inactive_seat_days(self, org) -> int:
        from discovery import licence_waste

        return licence_waste.inactive_days(org)

    sso_auto_provision = serializers.SerializerMethodField(
        help_text="Whether an unknown single sign-on identity waits for an Admin's approval (Users, pending access) "
        "instead of being refused. Off by default; meaningful only while single sign-on is on."
    )

    def get_sso_auto_provision(self, org) -> bool:
        from core import sso

        return sso.auto_provision(org)

    scim_default_role = serializers.SerializerMethodField(
        help_text="SCIM provisioning (capability scim, from the optional ee package): the role a provisioned member gets, viewer or editor."
    )
    scim_role_groups = serializers.SerializerMethodField(
        help_text="SCIM provisioning (capability scim, from the optional ee package): the provisioned group mapped to each role, by SCIM group id, or "
        "null. Mapping is on while either is set: a provisioned member's role is the highest mapped group they are in, "
        "otherwise scim_default_role."
    )

    scim_endpoint = serializers.SerializerMethodField(
        help_text="The SCIM Tenant URL to give the identity provider ({SOFTWARDEN_BASE_URL}/scim/v2/<slug>); shown to "
        "Admins of an organization with the scim capability, else null."
    )

    def get_scim_endpoint(self, org) -> str | None:
        from core.capabilities import SCIM

        if self.context.get("role") != Role.ADMIN or not has_capability(org, SCIM):
            return None  # without the optional `ee` package no organization has `scim`
        from ee.scim import resources

        return resources.base(org)

    def get_scim_default_role(self, org) -> str:
        if not ee_installed():
            return Role.VIEWER.value  # what provisioning would use; nothing provisions without the `ee` package
        from ee.scim import config

        return config.default_role(org)

    @extend_schema_field(ScimRoleGroupsSerializer)
    def get_scim_role_groups(self, org) -> dict[str, str | None]:
        if not ee_installed():
            return {Role.ADMIN.value: None, Role.EDITOR.value: None}
        from ee.scim import config

        return config.role_groups(org)

    attachments = serializers.SerializerMethodField()

    @extend_schema_field(EmailStatusSerializer)
    def get_email(self, org) -> dict[str, Any]:
        return email_status(org)

    @extend_schema_field(AttachmentLimitsSerializer)
    def get_attachments(self, org) -> dict[str, Any]:
        return attachment_limits(org)


class OrganizationInputSerializer(serializers.Serializer):
    name = serializers.CharField(required=False)
    timezone = serializers.CharField(required=False, help_text="An IANA timezone such as America/New_York.")
    review_interval_days = serializers.IntegerField(required=False, min_value=7, max_value=730)
    renewal_lead_days = serializers.IntegerField(required=False, min_value=1, max_value=365)
    reminder_thresholds = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=365),
        required=False,
        help_text="One to six different numbers of days, largest first.",
    )
    digest_time = serializers.CharField(required=False, help_text="HH:MM")
    coordinator = serializers.IntegerField(required=False, help_text="An active Admin or Editor's user ID.")
    compliance_profile_enabled = serializers.BooleanField(
        required=False,
        help_text="Turn the compliance profile (GxP, validation, periodic review, risk tier) on or off. Needs the "
        "compliance_profile capability: otherwise 403 /problems/capability-required. Turning it off "
        "hides the fields and keeps their values.",
    )
    attachment_max_size_mb = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text="The largest attachment in MB, at most the installation's; null for the installation's.",
    )
    attachment_extensions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        help_text="The file types allowed, a subset of the installation's (attachments.installation); null for all "
        "of them.",
    )
    attachment_quota_mb = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text="Total attachment storage in MB, at most the installation's quota; null for the installation's.",
    )
    sso_auto_provision = serializers.BooleanField(
        required=False,
        help_text="Turn single sign-on auto-provision on or off: an unknown identity signing in through this "
        "organization's single sign-on becomes a pending access request (no account, no role) for an Admin to approve "
        "or decline. Audited.",
    )
    scim_default_role = serializers.ChoiceField(
        choices=["viewer", "editor"],
        required=False,
        help_text="SCIM provisioning: the role a provisioned member gets (default viewer). Needs the scim capability "
        "(from the optional ee package). Audited; with role mapping on, every provisioned member's role is recomputed.",
    )
    scim_role_groups = serializers.DictField(
        child=serializers.UUIDField(allow_null=True),
        required=False,
        allow_null=True,
        help_text='SCIM provisioning: {"admin": <group id or null>, "editor": <group id or null>}, provisioned groups '
        "(GET /scim/groups). Opt-in role mapping; null or both null turns it off. Needs the scim capability. The last "
        "active Admin is never demoted by it.",
    )
    data_quality_disabled_rules = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_null=True,
        help_text="Data-quality rule ids to turn off (the whole list; [] or null turns every rule on). Rules that are "
        "also ownership, AI agent or review gaps can't be turned off.",
    )
    inactive_seat_days = serializers.IntegerField(
        required=False,
        min_value=7,
        max_value=730,
        help_text="License waste: days without a successful sign-in before a license counts as inactive (7 to 730). "
        "Audited.",
    )


# ---- limits (core/limits.py) --------------------------------------------------------------------------------


class LimitSerializer(serializers.Serializer):
    name = serializers.ChoiceField(NAMES)
    label = serializers.CharField(help_text="What the limit counts.")  # type: ignore[assignment]  # the API field
    unit = serializers.CharField(help_text="records, connectors, members, bytes, requests per minute, or emails.")
    limit = serializers.IntegerField(
        allow_null=True,
        help_text="The maximum now; null is unlimited. A new cloud organization's invitation email budgets are lower "
        "than the edition's default until a second member is active.",
    )
    default = serializers.IntegerField(allow_null=True, help_text="The edition's default; null is unlimited.")
    overridden = serializers.BooleanField(help_text="Whether the service operator set this organization's value.")
    usage = serializers.IntegerField(
        allow_null=True, help_text="How much is used now (counted for this response); null for the API rates."
    )


class LimitsSerializer(serializers.Serializer):
    limits = LimitSerializer(many=True)
