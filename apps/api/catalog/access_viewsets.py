"""Invitations and pending access. Admins list, create, revoke and resend
invitations and approve or decline pending access; the invitation link's page reads and accepts its invitation without
signing in (`POST auth/invitations/preview` and `.../accept`, public, CSRF-checked, on the sign-in throttle; the
token is in the body, since paths are logged). catalog/invitations.py validates, scopes and
audits every write.

An invitation's link is in the create and resend responses only; its token is stored hashed and is never in a list,
an ETag or an audit event. Where a new account needs proof of the mailbox (invitations.proof_required),
that link joins an existing account but creates none: the emailed link does."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from django.contrib.auth import login
from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import exceptions, status
from rest_framework.decorators import action, api_view, throttle_classes
from rest_framework.response import Response

from core import access, sso
from core.capabilities import SSO_OIDC
from core.drf import (
    ADMIN,
    PROBLEMS,
    ParamFilter,
    ProblemSerializer,
    TenantFilterSet,
    TenantViewSet,
    choice,
    etag,
    policy,
    public,
    revision,
    set_location,
    text,
)
from core.models import Invitation, PendingAccess
from core.throttling import ScopedThrottle

from . import invitations
from .account_serializers import (
    INVITATION_STATUSES,
    AcceptInvitationInputSerializer,
    ApprovePendingInputSerializer,
    InvitationCreatedSerializer,
    InvitationPreviewSerializer,
    InvitationSerializer,
    InvitationTokenInputSerializer,
    InviteInputSerializer,
    MemberSerializer,
    MeSerializer,
    PendingAccessSerializer,
    me_body,
)

WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
SECRET_FIELDS = ("token_hash",)  # not an ETag input; expires_at still moves the revision when it is resent


def created_response(invitation: Invitation, token: str, status_code: int, request=None) -> Response:
    """An invitation with its link: the only responses that carry it. Once the invitation is committed, the link is
    also emailed to the invitee when the installation can send email; `emailed` says whether it went."""
    found = Invitation.objects.select_related("organization", "invited_by", "accepted_by").get(pk=invitation.pk)
    emailed = invitations.email_link(found, token)
    body = {**InvitationSerializer(found).data, "link": invitations.link(token), "emailed": emailed}
    response = Response(body, status=status_code)
    response["ETag"] = etag(revision(found, skip=SECRET_FIELDS))
    if request is not None and status_code == status.HTTP_201_CREATED:  # from invitations or users
        set_location(request, response, f"{request.path_info.rsplit('/', 1)[0]}/invitations/{found.pk}")
    return response


class InvitationFilterSet(TenantFilterSet):
    status = ParamFilter(
        parse=choice(INVITATION_STATUSES),
        method="filter_status",
        choices=[(s, s) for s in INVITATION_STATUSES],
        help_text="pending, expired, accepted or revoked. Default: all.",
    )
    q = ParamFilter(parse=text, method="filter_q", help_text="Part of the email or the suggested name.")

    class Meta:
        model = Invitation
        fields: list[str] = []

    def filter_status(self, qs, name, value):
        from django.utils import timezone

        now = timezone.now()
        open_ = Q(accepted_at__isnull=True, revoked_at__isnull=True)
        return qs.filter(
            {
                "pending": open_ & Q(expires_at__gt=now),
                "expired": open_ & Q(expires_at__lte=now),
                "accepted": Q(accepted_at__isnull=False),
                "revoked": Q(revoked_at__isnull=False, accepted_at__isnull=True),
            }[value]
        )

    def filter_q(self, qs, name, value):
        return qs.filter(Q(email__icontains=value) | Q(name__icontains=value))


@extend_schema_view(
    list=extend_schema(summary="Invitations (Admin)"),
    retrieve=extend_schema(summary="Get an invitation (Admin); never its link"),
    create=extend_schema(
        summary="Invite someone by email (Admin)",
        description="Returns the invitation with its link, in this response only, and emails the link to the invitee "
        "when the installation can send email (`emailed`). Nobody is added yet: a new account "
        "is created, or an existing one joins, when they accept through the link. On an installation with more than "
        "one organization (or in cloud mode) the link returned here joins an existing account (signed in as itself) "
        "but creates none: only the emailed link, which proves the mailbox, does. A live invitation for the same "
        "email is replaced (revoked). Refused (400) for a member of this organization; the answer is otherwise the "
        "same whether or not the email has an account elsewhere.",
        request=InviteInputSerializer,
        responses={201: InvitationCreatedSerializer, **WRITE_ERRORS},
    ),
    revoke=extend_schema(
        summary="Revoke an invitation (Admin); its link stops working",
        request=None,
        responses={200: InvitationSerializer, **WRITE_ERRORS},
    ),
    resend=extend_schema(
        summary="Make a new link for an invitation (Admin)",
        description="The old link stops working; the invitation is good for 7 days from now. The link is in this "
        "response only, and emailed as on create.",
        request=None,
        responses={200: InvitationCreatedSerializer, **WRITE_ERRORS},
    ),
)
class InvitationViewSet(TenantViewSet):
    role_policy = policy(ADMIN)
    serializer_class = InvitationSerializer
    input_serializer_class = InviteInputSerializer
    filterset_class = InvitationFilterSet
    ordering_fields = ("created_at", "expires_at", "email")
    default_ordering = ("-created_at",)

    def base_queryset(self):
        return Invitation.objects.select_related("invited_by", "accepted_by")

    def revision_of(self, obj) -> str:
        return revision(obj, skip=SECRET_FIELDS)

    def create(self, request, *args, **kwargs):
        data = self.payload()
        with transaction.atomic():
            invitation, token = invitations.create_invitation(self.organization, request.user, data, emailing=True)
            return created_response(invitation, token, status.HTTP_201_CREATED, request)

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        with transaction.atomic():
            invitation = invitations.revoke_invitation(self.organization, request.user, self.get_object())
            return self.detail_response(self.fresh(invitation))

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        with transaction.atomic():
            invitation, token = invitations.resend_invitation(self.organization, request.user, self.get_object())
            return created_response(invitation, token, status.HTTP_200_OK)


PROVIDER_FILTERS = ("entra", "okta", "google", "saml", "other")  # saml (it was under other)


class PendingAccessFilterSet(TenantFilterSet):
    q = ParamFilter(parse=text, method="filter_q", help_text="Part of the email or the name.")
    provider = ParamFilter(
        parse=choice(PROVIDER_FILTERS),
        method="filter_provider",
        choices=[(p, p) for p in PROVIDER_FILTERS],
        help_text="entra, okta, google, saml (any SAML identity provider), or other (any other issuer).",
    )

    class Meta:
        model = PendingAccess
        fields: list[str] = []

    def filter_q(self, qs, name, value):
        return qs.filter(Q(email__icontains=value) | Q(name__icontains=value))

    def filter_provider(self, qs, name, value):
        from core import oidc_discovery

        known = {
            "entra": Q(issuer__startswith=f"https://{oidc_discovery.ENTRA_HOST}/"),
            "google": Q(issuer=oidc_discovery.GOOGLE_ISSUER),
        }
        okta = Q()
        for suffix in oidc_discovery.OKTA_HOSTS:
            okta |= Q(issuer__regex=r"^https://([^/]*\.)?" + re.escape(suffix[1:]) + r"(/|$)")
        known["okta"] = okta
        known["saml"] = Q(issuer__startswith=oidc_discovery.SAML_PREFIX)
        if value == "other":
            return qs.exclude(known["entra"] | known["google"] | known["okta"] | known["saml"])
        return qs.filter(known[value])


@extend_schema_view(
    list=extend_schema(summary="Pending access requests (Admin)"),
    retrieve=extend_schema(summary="Get a pending access request (Admin)"),
    approve=extend_schema(
        summary="Approve a pending access request with a role (Admin)",
        description="Creates the account (no password: they sign in with single sign-on), its membership and the "
        "pinned identity, or only pins the identity for someone already a member here. Any other account with this "
        "email (in another organization, or in none) is 409 /problems/invitation-required: invite them instead. "
        "Returns the member.",
        request=ApprovePendingInputSerializer,
        responses={200: MemberSerializer, **WRITE_ERRORS},
    ),
    destroy=extend_schema(summary="Decline a pending access request (Admin); the request is deleted"),
)
class PendingAccessViewSet(TenantViewSet):
    role_policy = policy(ADMIN, capability=SSO_OIDC)
    serializer_class = PendingAccessSerializer
    filterset_class = PendingAccessFilterSet
    ordering_fields = ("last_seen", "first_seen", "email")
    default_ordering = ("-last_seen",)

    def base_queryset(self):
        return PendingAccess.objects.all()

    def destroy_object(self, obj) -> None:
        invitations.decline_pending(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .account_viewsets import member_body

        data = self.payload()
        with transaction.atomic():
            membership = invitations.approve_pending(self.organization, request.user, self.get_object(), data)
            return Response(member_body(membership))


# ---- the invitation link's page (public) ----------------------------------------------------------------------------


UNAVAILABLE = OpenApiResponse(
    ProblemSerializer,
    description="The link is unknown, expired, used or revoked, or its account is deactivated "
    "(type /problems/invitation-unavailable; the same answer for each)",
)
THROTTLED = OpenApiResponse(description="Too many attempts; Retry-After says when to try again")
CSRF_NOTE = (
    " Needs the CSRF token (GET auth/csrf sets the cookie; send it as X-CSRFToken), signed in or not. The token is "
    "in the body, never the URL: paths are logged."
)


def _csrf(request) -> None:
    """Public, but a write from a browser all the same: without this an anonymous POST could create an account and
    sign a victim's browser into it (login CSRF). The setup endpoint's pattern (core/setup_views.py)."""
    from core.setup_views import _CsrfSession

    _CsrfSession().check(request)


def _live(request):
    """(invitation, whether the link proves the mailbox, body)."""
    _csrf(request)
    data = request.data if isinstance(request.data, dict) else {}
    found = invitations.lookup(data.get("token"))
    if found is None:
        raise invitations.InvitationUnavailable()
    return found[0], found[1], data


def _session_of(request, invitation) -> str:
    """Who this browser is signed in as, in the order accepting checks it: another account answers 409
    invitation-wrong-account first; the invited account in a session confined to another organization's single
    sign-on answers 409 invitation-other-sign-in."""
    user = request.user
    if not user.is_authenticated:
        return "none"
    if (user.username or "").lower() != invitation.email:
        return "other_account"
    confined = access.sso_confinement(request)
    if confined and str(confined) != str(invitation.organization_id):
        return "other_sign_in"
    return "invitee"


def _sso_option(request, invitation) -> dict[str, Any] | None:
    """The inviting organization's SSO button. The session remembers which invitation it is for (its id and a prefix
    of its token hash, never the token), so the callback can sign in the invited account by an identity already pinned
    to it (core/oidc.invitee); a resend ends that. With no button, an entry left by an earlier preview is cleared."""
    config = sso.effective(invitation.organization)
    if config is None:
        _forget(request)
        return None
    request.session[invitations.SESSION_INVITATION] = invitations.remember(invitation)
    query = urllib.parse.urlencode({"org": invitation.organization.slug, "next": invitations.INVITE_NEXT})
    route = "saml" if config.saml is not None else "oidc"  # SAML (the optional ee package) has its own start
    return {
        "provider": config.provider.preset,
        "label": config.label,
        "start_url": f"/api/v1/auth/{route}/start?{query}",
    }


def _preview(request, invitation, proven: bool) -> dict[str, Any]:
    """From a link that doesn't prove the mailbox, nothing depends on the address: no `account_exists` (unless
    this browser is signed in as the invited account, which knows it exists), and the inviting organization's single
    sign-on button whenever it has one."""
    session = _session_of(request, invitation)
    account = invitations.holder_for(invitation.email) if proven else None
    if account is not None or not proven:
        option = _sso_option(request, invitation)
    else:
        option = None
        _forget(request)
    if proven:
        exists: bool | None = account is not None
    else:
        exists = True if session in ("invitee", "other_sign_in") else None
    # Only the link that proves the mailbox says that an organization made an account for the address: its
    # holder is the address's owner, who may prefer signing in through that organization to retiring that account.
    made_by = invitations.organization_account(invitation.email) if proven else None
    return {
        "organization": {"name": invitation.organization.name, "slug": invitation.organization.slug},
        "email": invitations.mask_email(invitation.email),
        "role": invitation.role,
        "name": invitation.name,
        "expires_at": invitation.expires_at,
        "account_exists": exists,
        "can_create_account": proven,
        "organization_account": made_by,
        "session": session,
        "sso": option,
    }


def _forget(request) -> None:
    """No SSO button on this page: drop any invitation an earlier preview left in the session."""
    request.session.pop(invitations.SESSION_INVITATION, None)


@extend_schema(
    summary="Read an invitation from its link's token (public)",
    description="What the invitation page shows: the organization, the masked email, the role, whether the email "
    "has an account (null from a link that doesn't prove the mailbox), whether this link can create an account, and "
    "who this browser is signed in as." + CSRF_NOTE,
    auth=[],
    request=InvitationTokenInputSerializer,
    responses={200: InvitationPreviewSerializer, 403: PROBLEMS[403], 404: UNAVAILABLE, 429: THROTTLED},
)
@api_view(["POST"])
@throttle_classes([ScopedThrottle])
@public
def invitation_preview(request):
    try:
        found, proven, _ = _live(request)
    except invitations.InvitationUnavailable:
        _forget(request)  # a used, revoked, expired or rotated link: its invitation may no longer sign anyone in
        raise
    return Response(InvitationPreviewSerializer(_preview(request, found, proven)).data)


@extend_schema(
    summary="Accept an invitation (public)",
    description="A new account: send `token`, `name` and `password`; the account and its membership are created and "
    "this browser is signed in. An existing account: sign in as it first (password, or the inviting organization's "
    "single sign-on with next=/invite), then send `token` alone; the membership is created. Answers `me`. Creating "
    "an account needs a link that proves the mailbox (`can_create_account` in the preview: the emailed link where "
    "more than one organization shares the installation); with the other link and nobody signed in the answer is "
    "403 /problems/invitation-emailed-link-required, whether or not the email has an account. 403 "
    "/problems/not-signed-in when the email has an account and nobody is signed in; 409 "
    "/problems/invitation-wrong-account when another account is, and /problems/invitation-other-sign-in for a "
    "session signed in through another organization's single sign-on." + CSRF_NOTE,
    auth=[],
    request=AcceptInvitationInputSerializer,
    responses={
        200: MeSerializer,
        400: PROBLEMS[400],
        403: PROBLEMS[403],
        404: UNAVAILABLE,
        409: PROBLEMS[409],
        429: THROTTLED,
    },
)
@api_view(["POST"])
@throttle_classes([ScopedThrottle])
@public
def invitation_accept(request):
    if access.pinned_organization_id(request) is not None:
        raise exceptions.PermissionDenied("Accept an invitation from a browser, not with an API token.")
    found, proven, data = _live(request)
    token = data["token"]
    user = request.user if request.user.is_authenticated else None
    org_id = str(found.organization_id)
    # Signed in: it must be the invited email's account (an organization-made one included: it accepts
    # as itself); the session proves that account.
    if user is not None and (user.username or "").lower() != found.email:
        raise invitations.WrongAccount()
    if user is None and not proven:  # before anything that depends on the address
        raise invitations.EmailedLinkRequired()
    account = invitations.holder_for(found.email)
    if user is None and account is None:
        with transaction.atomic():
            user = invitations.accept_as_new_account(token, dict(data))
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session.pop(access.SSO_ORGANIZATION, None)  # like a password sign-in: no confinement
        request.session[access.CURRENT_ORGANIZATION] = org_id
        return Response(me_body(request))
    if user is None:
        raise invitations.SignInFirst()
    confined = access.sso_confinement(request)
    if confined and confined != org_id:  # that session can't reach the organization it would join
        raise invitations.ConfinedElsewhere()
    with transaction.atomic():
        invitations.accept_as_existing_account(token, user)
    request.session[access.CURRENT_ORGANIZATION] = org_id
    request.session.pop(invitations.SESSION_INVITATION, None)
    return Response(me_body(request))


invitation_preview.cls.throttle_scope = "invitation"  # type: ignore[attr-defined]
invitation_accept.cls.throttle_scope = "invitation"  # type: ignore[attr-defined]
