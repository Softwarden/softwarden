"""An organization's single sign-on settings (core/sso.py). Admin only, reads included, and capability
`sso.oidc`. Every write goes through core/sso.py. The client secrets are write-only: no response, ETag or
audit event carries them; `secret_set` and `secret_set_at` say whether and since when one is stored.

- GET / PATCH `sso/settings`: the active configuration, the draft and its test, the sign-in domains. PATCH changes
  the draft (with If-Match like every resource); nothing that signs people in changes until activation.
- POST `sso/settings/test`: start a test sign-in of the draft with your own account (signed-in sessions only: it is a
  browser round trip). Answers the provider's authorize URL to send the browser to.
- POST `sso/settings/activate`: make the draft that passed its test, unchanged since, the sign-in configuration.
- POST `sso/settings/disable`: turn single sign-on off for the organization; password sign-in stays.

The `saml` preset (capability `sso.saml` on top of `sso.oidc`, from the optional `ee` package) has its own values on both sides
(`idp_entity_id`, `idp_sso_url`, `idp_certificates`, `label`) and `saml` gives the organization's service provider
addresses to register with the identity provider; `idp_metadata_xml` (write-only) imports the IdP's values once."""

from typing import Any

from django.db import transaction
from drf_spectacular.utils import extend_schema
from rest_framework import exceptions, serializers
from rest_framework.response import Response

from . import access, credentials, oidc, sso
from .capabilities import SSO_OIDC, SSO_SAML, ee_installed, has_capability
from .drf import (
    ADMIN,
    IF_MATCH,
    PROBLEMS,
    ReadOnlySerializer,
    RoleAPIView,
    UserBriefSerializer,
    etag,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
    timestamp,
)
from .models import SsoProvider, SsoSettings, SsoStatus

ERRORS = {c: PROBLEMS[c] for c in (403,)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 409)}


def settings_revision(row: SsoSettings | None, domains: list[str]) -> str:
    if row is None:
        return revision(extra={"sso_settings": None, "domains": domains})
    return revision(row, skip=sso.SECRET_COLUMNS, extra={"domains": domains})


# ---- serializers --------------------------------------------------------------------------------------------------

PROVIDERS = SsoProvider.values


class SamlCertificateSerializer(ReadOnlySerializer):
    sha256 = serializers.CharField(help_text="The certificate's SHA-256 fingerprint.")
    subject = serializers.CharField()
    not_after = serializers.CharField(help_text="When the certificate expires (ISO 8601).")
    pem = serializers.CharField(help_text="The certificate (public).")


class SsoSamlServiceProviderSerializer(ReadOnlySerializer):
    entity_id = serializers.CharField(help_text="Register this SP entity ID (also the metadata URL) with the IdP.")
    metadata_url = serializers.CharField()
    acs_url = serializers.CharField(help_text="The Assertion Consumer Service URL (HTTP-POST binding).")


class SsoSideSerializer(ReadOnlySerializer):
    """One configuration: the provider preset and its values. Never the secret."""

    provider = serializers.ChoiceField(PROVIDERS, allow_blank=True, help_text="entra, okta, google, oidc or saml.")
    tenant_id = serializers.CharField(help_text="Entra: the directory (tenant) ID, a GUID.")
    okta_domain = serializers.CharField(help_text="Okta: the Okta domain, such as example.okta.com.")
    okta_authorization_server = serializers.CharField(
        help_text="Okta: a custom authorization server ID (such as default); empty for the org authorization server."
    )
    discovery_url = serializers.CharField(
        help_text="The provider's OpenID Connect discovery document; computed for the presets."
    )
    issuer = serializers.CharField(help_text="The issuer the discovery document and ID tokens must name.")
    hosted_domains = serializers.ListField(
        child=serializers.CharField(), help_text="Google: the Workspace domains whose accounts may sign in (hd)."
    )
    client_id = serializers.CharField()
    secret_set = serializers.BooleanField(help_text="A client secret is stored (it is never returned).")
    secret_set_at = serializers.DateTimeField(allow_null=True)
    idp_entity_id = serializers.CharField(help_text="SAML: the identity provider's entity ID (also `issuer`).")
    idp_sso_url = serializers.CharField(help_text="SAML: the identity provider's sign-in URL (HTTP-Redirect binding).")
    idp_certificates = SamlCertificateSerializer(
        many=True, help_text="SAML: the certificates a Response or Assertion may be signed with (any of them)."
    )
    label = serializers.CharField(  # type: ignore[assignment]  # the response member, not Field.label
        help_text='SAML: the sign-in button\'s text; empty for "SAML single sign-on".'
    )


class SsoActiveSerializer(SsoSideSerializer):
    revision = serializers.IntegerField(help_text="Bumped by every activation; sign-ins in progress must restart.")
    activated_at = serializers.DateTimeField(allow_null=True)
    activated_by = UserBriefSerializer(allow_null=True)


class SsoTestSerializer(ReadOnlySerializer):
    revision = serializers.IntegerField(allow_null=True, help_text="The draft revision the last test was of.")
    ok = serializers.BooleanField(allow_null=True)
    at = serializers.DateTimeField(allow_null=True)
    by = UserBriefSerializer(allow_null=True)
    code = serializers.CharField(help_text="Why the last test failed, as a short code; empty when it passed.")
    detail = serializers.CharField(help_text="What the last test showed, in Softwarden's words.")


class SsoDraftSerializer(SsoSideSerializer):
    revision = serializers.IntegerField(help_text="Bumped by every saved change; send it to test and activate.")
    test = SsoTestSerializer()
    tested = serializers.BooleanField(help_text="The draft passed a test sign-in of its current revision.")
    missing = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text="What the draft still needs before it can be tested, by field.",
    )


class SsoEffectiveSerializer(ReadOnlySerializer):
    source = serializers.ChoiceField(  # type: ignore[assignment]  # the response member, not Field.source
        ["force_local", "capability", "active", "legacy", "disabled", "off"],
        help_text="How members sign in with SSO now: these settings (active), the installation's legacy "
        "SOFTWARDEN_OIDC_* (legacy), not at all (disabled, off), or blocked by SOFTWARDEN_FORCE_LOCAL_LOGIN.",
    )
    provider = serializers.CharField()
    detail = serializers.CharField()


class SsoDomainStateSerializer(ReadOnlySerializer):
    domain = serializers.CharField()
    status = serializers.ChoiceField(
        ["verified", "pending"],
        help_text="verified: routes sign-ins. pending (SOFTWARDEN_SSO_DOMAIN_VERIFICATION): routes nobody until "
        "the TXT record is published and POST sso/settings/domains/verify confirms it.",
    )
    claimed_at = serializers.DateTimeField()
    verified_at = serializers.DateTimeField(allow_null=True)
    lapses_at = serializers.DateTimeField(
        allow_null=True,
        help_text="A pending claim is forgotten then, 14 days after it was made (removing and re-adding the "
        "domain makes a new claim with a new date).",
    )
    record_name = serializers.CharField(allow_null=True, help_text="Pending: the TXT record's name to publish.")
    record_value = serializers.CharField(allow_null=True, help_text="Pending: the TXT record's value to publish.")


class SsoDomainVerifyInputSerializer(serializers.Serializer):
    domain = serializers.CharField(help_text="One of sign_in_domains.")


class SsoSettingsSerializer(ReadOnlySerializer):
    status = serializers.ChoiceField(SsoStatus.values, help_text="legacy (not set up here), active or disabled.")
    effective = SsoEffectiveSerializer()
    force_local_login = serializers.BooleanField(help_text="SOFTWARDEN_FORCE_LOCAL_LOGIN is set on the installation.")
    redirect_uri = serializers.CharField(help_text="Register this redirect URI with the identity provider.")
    sign_in_domains = serializers.ListField(
        child=serializers.CharField(),
        help_text="Work email domains this organization claims; a verified one finds this organization on the "
        "sign-in page and is unique on the installation (sign_in_domain_states says which).",
    )
    sign_in_domain_states = SsoDomainStateSerializer(many=True)
    saml = SsoSamlServiceProviderSerializer(
        allow_null=True,
        help_text="This organization's SAML service provider addresses; null without the sso.saml capability.",
    )
    active = SsoActiveSerializer(allow_null=True, help_text="Null until a draft is activated.")
    draft = SsoDraftSerializer()


class SsoSettingsInputSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(PROVIDERS, required=False)
    tenant_id = serializers.CharField(required=False, help_text="Entra: the tenant GUID (never common).")
    okta_domain = serializers.CharField(required=False)
    okta_authorization_server = serializers.CharField(required=False, allow_blank=True)
    discovery_url = serializers.CharField(
        required=False, help_text="Generic OpenID Connect only: https://…/.well-known/openid-configuration."
    )
    hosted_domains = serializers.ListField(child=serializers.CharField(), required=False)
    client_id = serializers.CharField(required=False)
    client_secret = serializers.CharField(required=False, write_only=True, help_text="Write-only; send to replace.")
    idp_entity_id = serializers.CharField(required=False, help_text="SAML: the identity provider's entity ID.")
    idp_sso_url = serializers.CharField(required=False, help_text="SAML: https sign-in URL (HTTP-Redirect binding).")
    idp_certificates = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="SAML: signing certificates in PEM form (1 to 5)."
    )
    idp_metadata_xml = serializers.CharField(
        required=False,
        write_only=True,
        help_text="SAML: the identity provider's metadata XML, pasted (never fetched). Fills idp_entity_id, "
        "idp_sso_url and idp_certificates; any of those sent beside it win.",
    )
    label = serializers.CharField(required=False, allow_blank=True, help_text="SAML: the sign-in button's text.")  # type: ignore[assignment]
    sign_in_domains = serializers.ListField(child=serializers.CharField(), required=False)


class SsoRevisionInputSerializer(serializers.Serializer):
    draft_revision = serializers.IntegerField(help_text="The draft revision you reviewed (draft.revision).")


class SsoTestStartSerializer(ReadOnlySerializer):
    authorize_url = serializers.CharField(
        allow_null=True,
        help_text="Send the browser here to sign in with your own account. Null when the test already failed (the "
        "provider's discovery document couldn't be used); the settings' draft.test says why.",
    )
    settings = SsoSettingsSerializer()


def _certificates(row: SsoSettings, prefix: str) -> list[dict[str, Any]]:
    """Stored SAML certificates described for people, by the optional `ee` package; none without it."""
    stored = getattr(row, f"{prefix}saml_certs") or []
    if not stored or not ee_installed():
        return []
    from ee.saml.idp import certificate_info

    return [certificate_info(c) for c in stored]


def _side(row: SsoSettings, prefix: str) -> dict[str, Any]:

    return {
        "provider": getattr(row, f"{prefix}provider"),
        "tenant_id": getattr(row, f"{prefix}tenant"),
        "okta_domain": getattr(row, f"{prefix}okta_domain"),
        "okta_authorization_server": getattr(row, f"{prefix}okta_server"),
        "discovery_url": getattr(row, f"{prefix}discovery_url"),
        "issuer": getattr(row, f"{prefix}issuer"),
        "hosted_domains": list(getattr(row, f"{prefix}hosted_domains") or []),
        "client_id": getattr(row, f"{prefix}client_id"),
        "secret_set": credentials.slot_configured(row, f"{prefix}secret"),
        "secret_set_at": timestamp(getattr(row, f"{prefix}secret_set_at")),
        "idp_entity_id": getattr(row, f"{prefix}saml_entity_id"),
        "idp_sso_url": getattr(row, f"{prefix}saml_sso_url"),
        "idp_certificates": _certificates(row, prefix),
        "label": getattr(row, f"{prefix}label"),
    }


def _saml(org) -> dict[str, str] | None:
    if not has_capability(org, SSO_SAML):
        return None
    from ee.saml.sp import addresses

    return addresses(org)


def body(org) -> dict[str, Any]:
    row = sso.settings_of(org) or SsoSettings(organization=org)
    active = None
    if row.active_revision:
        active = {
            **_side(row, ""),
            "revision": row.active_revision,
            "activated_at": timestamp(row.activated_at),
            "activated_by": row.activated_by,
        }
    draft = {
        **_side(row, "draft_"),
        "revision": row.draft_revision,
        "test": {
            "revision": row.draft_tested_revision,
            "ok": row.draft_test_ok,
            "at": timestamp(row.draft_tested_at),
            "by": row.draft_tested_by,
            "code": row.draft_test_code,
            "detail": row.draft_test_detail,
        },
        "tested": row.draft_is_tested,
        "missing": sso.missing(row),
    }
    return {
        "status": row.status,
        "effective": sso.state(org),
        "force_local_login": sso.force_local(),
        "redirect_uri": oidc.redirect_uri(),
        "sign_in_domains": sso.domains_of(org),
        "sign_in_domain_states": sso.domain_states(org),
        "saml": _saml(org),
        "active": active,
        "draft": draft,
    }


# ---- views --------------------------------------------------------------------------------------------------------


class SsoView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN, capability=SSO_OIDC)

    def respond(self) -> Response:
        org = self.organization
        response = Response(SsoSettingsSerializer(body(org)).data)
        response["ETag"] = etag(settings_revision(sso.settings_of(org), sso.domain_keys(org)))
        return response


class SsoSettingsView(SsoView):
    @extend_schema(summary="Single sign-on settings (Admin)", responses={200: SsoSettingsSerializer, **ERRORS})
    def get(self, request):
        refuse_unknown_params(request)
        return self.respond()

    @extend_schema(
        summary="Change the single sign-on draft and the sign-in domains (Admin)",
        description="Saving a change to the draft bumps draft.revision, which invalidates its test; sign-in keeps "
        "using the active configuration until a tested draft is activated. client_secret is write-only: send it "
        "only to replace it. Presets compute the discovery URL and issuer: entra from tenant_id, okta from "
        "okta_domain and okta_authorization_server, google from Google's document (hosted_domains required); oidc "
        "takes discovery_url; saml (needs the optional ee package, else 403 /problems/capability-required) takes idp_entity_id, "
        "idp_sso_url and idp_certificates, or idp_metadata_xml. sign_in_domains apply at once. With "
        "SOFTWARDEN_SSO_DOMAIN_VERIFICATION (cloud) a new one is pending, routing nobody, until its TXT record is "
        "verified (POST sso/settings/domains/verify); without it a domain another organization holds is refused.",
        parameters=[IF_MATCH],
        request=SsoSettingsInputSerializer,
        responses={200: SsoSettingsSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    )
    def patch(self, request):
        data = self.payload()
        org = self.organization
        with transaction.atomic():
            row = SsoSettings.objects.select_for_update().filter(organization=org).first()
            require_match(request, settings_revision(row, sso.domain_keys(org)))
            sso.update_draft(org, request.user, data)
            return self.respond()


class SsoTestView(SsoView):
    @extend_schema(
        summary="Start a test sign-in of the draft with your own account (Admin, signed-in session)",
        description="A real sign-in round trip against the draft: send the browser to authorize_url, sign in as "
        "yourself, and the callback records the result on this draft revision and returns to /admin/sign-in?"
        "sso_test=passed|failed. Nobody is signed in and your session's user doesn't change. Refused with an API "
        "token, while SOFTWARDEN_FORCE_LOCAL_LOGIN is set (409 /problems/sso-local-only) and for a draft changed "
        "since you read it (409 /problems/sso-draft-changed).",
        request=SsoRevisionInputSerializer,
        responses={200: SsoTestStartSerializer, **WRITE_ERRORS},
    )
    def post(self, request):
        if access.pinned_organization_id(request) is not None:
            raise exceptions.PermissionDenied("Run a test sign-in from a signed-in browser session, not with a token.")
        data = self.payload()
        org = self.organization
        started = sso.start_test(org, request.user, request.session, data.get("draft_revision"))
        out = SsoTestStartSerializer({"authorize_url": started["authorize_url"], "settings": body(org)})
        response = Response(out.data)
        response["ETag"] = etag(settings_revision(sso.settings_of(org), sso.domain_keys(org)))
        return response


class SsoActivateView(SsoView):
    @extend_schema(
        summary="Activate the tested draft (Admin)",
        description="Only a draft whose test of its current revision passed can be activated (409 "
        "/problems/sso-test-required otherwise). Sign-ins in progress must start again. Password sign-in is "
        "unchanged.",
        request=SsoRevisionInputSerializer,
        responses={200: SsoSettingsSerializer, **WRITE_ERRORS},
    )
    def post(self, request):
        data = self.payload()
        sso.activate(self.organization, request.user, data.get("draft_revision"))
        return self.respond()


class SsoDisableView(SsoView):
    @extend_schema(
        summary="Turn single sign-on off for this organization (Admin)",
        description="Members sign in with their passwords; the installation's legacy SOFTWARDEN_OIDC_* isn't used "
        "either. Activate a tested draft to turn it on again.",
        request=None,
        responses={200: SsoSettingsSerializer, **{c: PROBLEMS[c] for c in (403,)}},
    )
    def post(self, request):
        sso.disable(self.organization, request.user)
        return self.respond()


class SsoDomainVerifyView(SsoView):
    @extend_schema(
        summary="Verify a pending sign-in domain by its DNS TXT record (Admin)",
        description="Looks up the TXT record `record_name` of one of sign_in_domains and, when it carries "
        "`record_value` and no other organization holds the domain, marks it verified: it then finds this "
        "organization on the sign-in page. Every refusal (no record yet, a DNS failure, a domain held elsewhere) is "
        "the same 400 on `domain`. A verified domain answers its state again. Audited.",
        request=SsoDomainVerifyInputSerializer,
        responses={200: SsoDomainStateSerializer, **WRITE_ERRORS},
    )
    def post(self, request):
        data = self.payload()
        state = sso.verify_domain(self.organization, request.user, data.get("domain"))
        return Response(SsoDomainStateSerializer(state).data)
