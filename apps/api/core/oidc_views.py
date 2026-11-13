"""Sign-in options and the OpenID Connect sign-in endpoints (per-organization SSO). The browser is sent
to the provider and back, so start and callback answer with redirects, not JSON; a refusal lands on the sign-in page
with a reason code it turns into a sentence. A test sign-in (core/sso.start_test) comes back to the same callback and
lands on the Admin's sign-in settings with ?sso_test=passed or failed instead; it never signs anyone in. Unlinking a
member's identity is `DELETE users/<id>/identity` (catalog/account_viewsets.py).

The organization comes from `?org=<slug>`, or from `?email=` (its domain against the organizations' sign-in domains),
or is the installation's only organization. An unknown slug or domain gets the same answer as an organization with
local sign-in only. With SOFTWARDEN_FORCE_LOCAL_LOGIN every route here offers and does password sign-in only."""

import logging
import re
import urllib.parse

from django.contrib.auth import login
from django.http import HttpResponseRedirect
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from catalog import password_reset, services, signup

from . import access, bootstrap, oidc, sso
from .drf import public, refuse_unknown_params
from .models import Membership, Organization, Role, SsoProvider
from .throttling import LookupThrottle, ScopedThrottle

log = logging.getLogger("softwarden.oidc")


# Control characters and backslashes anywhere: browsers strip tabs and newlines and read "\\" as "/", so "/\t/evil.example"
# would become "//evil.example".
_UNSAFE = re.compile(r"[\x00-\x1f\\]")
EMAIL_MAX = 254


def _local(path):
    """Only a path on this site (as the SPA's own redirect check): anything else becomes "/" (no page asked for)."""
    if isinstance(path, str) and path.startswith("/") and not path.startswith("//") and not _UNSAFE.search(path):
        return path
    return "/"


# Where a signed-in person lands when no page was asked for: the dashboard, for every role.
# Needs attention stays at "/".
HOME = "/dashboard"


def _landing(path):
    """After a successful sign-in: the page that was asked for (validated as on this site), else the dashboard."""
    target = _local(path)
    return HOME if target == "/" else target


def _refused(code, next_path=None, chooser=None):
    """Back to the sign-in page with the reason, the org or email the sign-in started with (so the SSO button comes
    back) and the page they were going to: /login?error=<code>&org=<slug>&next=<path>."""
    query = {"error": code}
    for key in ("org", "email"):
        value = (chooser or {}).get(key) if isinstance(chooser, dict) else None
        if isinstance(value, str) and value:
            query[key] = value[:EMAIL_MAX]
            break
    if next_path and _local(next_path) != "/":
        query["next"] = _local(next_path)
    return HttpResponseRedirect("/login?" + urllib.parse.urlencode(query))


def _chooser_of(pending):
    return pending.get("chooser") if isinstance(pending, dict) else None


def _chooser(params) -> tuple[dict[str, str], object]:
    """({the identifying query parameter the caller sent}, the organization it names or None)."""
    slug = params.get("org")
    email = params.get("email")
    if isinstance(slug, str) and slug.strip():
        return {"org": slug.strip()[:60]}, sso.org_for_sign_in(slug=slug)
    if isinstance(email, str) and email.strip():
        email = email.strip()[:EMAIL_MAX]
        return {"email": email}, sso.org_for_sign_in(email=email)
    return {}, sso.org_for_sign_in()


class SsoOptionSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        ["entra", "okta", "google", "oidc", "saml"], help_text="The provider preset: shows which button to draw."
    )
    label = serializers.CharField(  # type: ignore[assignment]  # the response member, not Field.label
        help_text="The provider's name for the button, such as Microsoft Entra ID."
    )
    start_url = serializers.CharField(
        help_text="Where the button goes: the start route with the same org or email parameter this request sent "
        "(add next=<path> to come back to a page)."
    )


class AuthOptionsSerializer(serializers.Serializer):
    entra = serializers.BooleanField(
        help_text="Whether Sign in with Microsoft Entra ID is offered (sso.provider is entra). Kept for the sign-in "
        "page that predates per-organization SSO."
    )
    setup_pending = serializers.BooleanField(
        help_text="True on a fresh installation until first-run setup creates the first organization and Admin: there "
        "is no account to sign in with yet, so the sign-in page sends visitors to /setup."
    )
    password = serializers.BooleanField(help_text="Password sign-in is offered: always true; it is never removed.")
    signup_enabled = serializers.BooleanField(
        help_text="Public signup is offered: the installation turned it on (SOFTWARDEN_SIGNUP_ENABLED) and "
        "can send email to confirm addresses. Show a link to /signup when true."
    )
    password_reset_enabled = serializers.BooleanField(
        help_text="Self-service password reset is offered: the installation can send email. Show a link to "
        "/forgot-password when true."
    )
    sso = SsoOptionSerializer(
        allow_null=True,
        help_text="The single sign-on button to show, or null for password sign-in only. Null too for an unknown "
        "organization or email domain, so the answer reveals nothing about which organizations exist.",
    )


REDIRECT = OpenApiResponse(description="A redirect: to the provider, back to the page, or to /login?error=<reason>")
CHOOSER = [
    OpenApiParameter("org", str, description="The organization's slug (a sign-in link such as /login?org=acme)."),
    OpenApiParameter(
        "email", str, description="A work email: its domain finds the organization by its sign-in domains."
    ),
]


@extend_schema(
    summary="Sign-in methods offered (public)",
    description="Which sign-in methods the sign-in page shows for an organization, named by `org` or found from "
    "`email`; with neither, the installation's only organization.",
    auth=[],
    parameters=CHOOSER,
    responses={200: AuthOptionsSerializer},
)
@api_view(["GET"])
@throttle_classes([LookupThrottle])
@public
def options(request):
    refuse_unknown_params(request, ("org", "email"))
    query, org = _chooser(request.query_params)
    config = sso.effective(org) if org is not None else None
    option = None
    if config is not None:
        label = config.label
        if "email" in query and config.saml is not None:
            # A lookup by email domain doesn't learn the Admin's SAML button text (often the company's
            # name); a sign-in link naming the organization (?org=) still shows it.
            label = sso.LABELS[SsoProvider.SAML]
        option = {
            "provider": config.provider.preset,
            "label": label,
            "start_url": f"/api/v1/auth/{'saml' if config.saml is not None else 'oidc'}/start"
            + (f"?{urllib.parse.urlencode(query)}" if query else ""),
        }
    body = {
        "entra": option is not None and option["provider"] == "entra",
        "setup_pending": bootstrap.web_setup_open(),
        "password": True,
        "signup_enabled": signup.available(),
        "password_reset_enabled": password_reset.available(),
        "sso": option,
    }
    return Response(AuthOptionsSerializer(body).data)


@extend_schema(
    summary="Start single sign-on (public): redirects the browser to the organization's identity provider",
    auth=[],
    parameters=[
        *CHOOSER,
        OpenApiParameter("next", str, description="A path on this site to return to after signing in."),
    ],
    responses={302: REDIRECT},
)
@api_view(["GET"])
@throttle_classes([ScopedThrottle])
@public
def start(request):
    query, org = _chooser(request.query_params)
    try:
        url = oidc.start(
            request.session,
            org,
            _local(request.query_params.get("next")),
            login_hint=query.get("email", ""),
            chooser=query,
        )
        return HttpResponseRedirect(url)
    except oidc.SignInRefused as e:
        return _refused(e.code, e.next_path, query)


def _test_admin(request, pending):
    """The Admin who started a test sign-in, when this is still their session and they are still an Admin there."""
    user = request.user
    admin_id = pending.get("admin")
    if not (user.is_authenticated and admin_id is not None and user.pk == admin_id):
        return None
    if not Membership.objects.filter(user=user, organization_id=pending.get("org"), role=Role.ADMIN).exists():
        return None
    return user


def _test_result(request, pending, ok, code="", detail=""):
    """A test sign-in's end: recorded on the draft revision it started with (only from the session of the Admin who
    started it), then back to the sign-in settings. Never signs anyone in or changes the session's user."""
    admin = _test_admin(request, pending)
    if admin is None:
        log.info("sso test refused: not the session that started it")
        return HttpResponseRedirect(f"{sso.TEST_LANDING}?sso_test=failed&reason=test_session")
    sso.record_test(pending.get("org"), pending.get("revision"), admin, ok, code, detail)
    query = {"sso_test": "passed" if ok else "failed", **({"reason": code} if not ok and code else {})}
    return HttpResponseRedirect(f"{sso.TEST_LANDING}?{urllib.parse.urlencode(query)}")


@extend_schema(
    summary="Single sign-on callback (public): the identity provider redirects the browser here",
    auth=[],
    parameters=[OpenApiParameter("code", str), OpenApiParameter("state", str)],
    responses={302: REDIRECT},
)
@api_view(["GET"])
@throttle_classes([ScopedThrottle])
@public
def callback(request):
    next_path = None
    try:
        claims, next_path, config, pending = oidc.finish(request.session, request.query_params)
    except oidc.SignInRefused as e:
        if e.pending is not None and e.pending.get("mode") == "test":
            return _test_result(request, e.pending, False, e.code, f"Test sign-in failed: {e.detail or e.code}")
        log.info("sso sign-in refused", extra={"reason": e.code})
        return _refused(e.code, e.next_path or next_path, e.chooser or _chooser_of(e.pending))
    return complete_sign_in(request, claims, config, pending, next_path)


def complete_sign_in(request, claims, config, pending, next_path):
    """After a checked ID token (callback) or SAML Response (ee/saml/views.acs): a test sign-in's result, or the
    member's sign-in confined to the organization, an invitation's acceptance, or a pending access request. `claims`
    are OpenID Connect shaped (iss, sub, email, names); the audit method is the configuration's preset."""
    if config.mode == "test":
        admin = _test_admin(request, pending)
        if admin is None:
            return _test_result(request, pending, False, "test_session")
        ok, code, detail = oidc.own_account(claims, config, admin)
        return _test_result(request, pending, ok, code, detail if ok else f"Test sign-in failed: {detail}")
    method = config.provider.preset
    try:
        user = oidc.match_user(claims, config)
    except oidc.SignInRefused as e:
        if e.user is not None:  # audited, in each of the account's organizations, as local sign-in
            services.audit_account(e.user, None, "user.login_failed", {"user": e.user.id, "method": method})
        if e.code == "no_account":
            from catalog.invitations import SESSION_INVITATION  # lazily, as core/oidc.invitee imports catalog

            remembered = request.session.get(SESSION_INVITATION)
            invitee = oidc.invitee(claims, config, next_path, remembered)
            if invitee is not None:  # accepting an invitation as an account this identity already signs in
                return _signed_in_for_invitation(request, invitee, config, method, next_path)
            if e.provisionable:
                waiting = oidc.request_access(claims, config)
                if waiting is not None:  # no account, no session: an Admin approves or declines
                    log.info("sso sign-in pending", extra={"reason": waiting})
                    slug = Organization.objects.filter(pk=config.org_id).values_list("slug", flat=True).first()
                    return _refused(waiting, None, {"org": slug} if slug else None)
        log.info("sso sign-in refused", extra={"reason": e.code})
        return _refused(e.code, next_path, _chooser_of(pending))
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # The organization whose settings signed them in, and the only one this session reaches (core/access.py).
    # The legacy single-organization settings confine too: there is only that organization.
    request.session[access.CURRENT_ORGANIZATION] = config.org_id
    request.session[access.SSO_ORGANIZATION] = config.org_id
    membership, _ = access.resolve(request)
    services.audit(membership.organization, user, "user.login", None, {"user": user.id, "method": method})
    return HttpResponseRedirect(_landing(next_path))


def _signed_in_for_invitation(request, user, config, method, next_path):
    """Sign in an invited account through the inviting organization's single sign-on, confined to that organization
    (where it has no membership until it accepts), and go back to the invitation's page."""
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session[access.CURRENT_ORGANIZATION] = config.org_id
    request.session[access.SSO_ORGANIZATION] = config.org_id
    org = Organization.objects.get(pk=config.org_id)
    # Not a member of `org` yet: the event names neither the account nor the user, only the email's domain, until the
    # acceptance records them.
    domain = (user.email or user.username or "").rsplit("@", 1)[-1]
    services.audit(org, None, "user.login", None, {"method": method, "invitation": True, "email_domain": domain})
    return HttpResponseRedirect(_local(next_path))


# Start and callback share local sign-in's throttle: start can make an outbound discovery fetch.
start.cls.throttle_scope = "sso"  # type: ignore[attr-defined]
# Looking an organization or an email domain up counts in the same scope (core/throttling.LookupThrottle).
options.cls.throttle_scope = "sso"  # type: ignore[attr-defined]
callback.cls.throttle_scope = "sso"  # type: ignore[attr-defined]
