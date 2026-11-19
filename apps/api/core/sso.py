"""Per-organization single sign-on settings (a draft / test / activate flow over core/oidc_discovery.py). Routes: core/sso_views.py (Admin only, capability `sso.oidc`); the sign-in
flow itself: core/oidc.py. Every write here validates, scopes to the organization and audits in one transaction.

- **Draft.** An Admin edits the draft (`update_draft`): the provider preset and its values, the client ID and the
  client secret (a Fernet slot, write-only). A change to any of them bumps `draft_revision`, which invalidates the
  draft's test. Sign-in domains (`SsoDomain`) are an organization setting outside the draft: they only route the
  sign-in page and apply at once, or with SOFTWARDEN_SSO_DOMAIN_VERIFICATION (cloud) once a DNS TXT record verifies
  them ("sign-in domain verification" below).
- **Test.** `start_test` fetches the draft's discovery document (a failure is recorded as a failed test) and starts a
  real sign-in of the Admin's own account against the draft, pinned in the session as mode `test` with the draft
  revision. The callback (core/oidc_views.py) records the result on that revision only (`record_test`) and never signs
  anyone in or changes the session's user.
- **Activate.** Serialized under select_for_update: only a draft whose passed test is of its current revision is
  copied to the active side, `active_revision` is bumped and the status becomes `active`.
- **Disable / legacy.** `disable` turns SSO off for the organization (`disabled`, the environment included);
  `use_env` hands it back to the legacy `SOFTWARDEN_OIDC_*` Entra settings (`legacy`), which apply only while the
  installation has exactly one organization and it has nothing active.
- **Break-glass.** `SOFTWARDEN_FORCE_LOCAL_LOGIN=1` makes `effective` answer None for everyone and the test and
  activation refuse; `manage.py sso_recovery` shows the state and disables or hands back from the host.

Sign-in pins {mode, organization, revision} in the session (core/oidc.begin); `config_for` gives the configuration
back only while it is still the one that started, so a change in between refuses the callback.

**SAML (capability `sso.saml`, from the optional `ee` package).** The `saml` preset keeps the same draft / test / activate flow over its
own values: the IdP's entity ID (which becomes `issuer`), its HTTP-Redirect sign-in URL and signing certificates,
typed in or imported once from pasted metadata XML (`idp_metadata_xml`, never fetched), and a button `label`. Their
validation and the sign-in itself are in ee/saml (idp.py, sp.py), imported here only when a SAML value is in play.
Choosing the preset or sending a SAML value without the capability is the 403 capability problem; so are testing and
activating a SAML draft, and active SAML settings stop signing anyone in when the capability goes."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.services import audit

from . import credentials, dns_txt, oidc_discovery, outbound
from .capabilities import SSO_OIDC, SSO_SAML, ee_installed, has_capability
from .models import ISSUER_MAX, Membership, Organization, Role, SsoDomain, SsoProvider, SsoSettings, SsoStatus
from .oidc_discovery import WELL_KNOWN, Provider, ProviderError
from .problems import CapabilityRequired, Conflict, FieldErrors

log = logging.getLogger("softwarden.sso")

SECRET_COLUMNS = ("secret_ciphertext", "secret_key_id", "draft_secret_ciphertext", "draft_secret_key_id")
# The draft's settable values: {API name: column suffix}.
INPUT = {
    "provider": "provider",
    "tenant_id": "tenant",
    "okta_domain": "okta_domain",
    "okta_authorization_server": "okta_server",
    "discovery_url": "discovery_url",
    "hosted_domains": "hosted_domains",
    "client_id": "client_id",
    # SAML (the optional `ee` package): the identity provider's entity ID, sign-in URL and signing certificates, and the button label
    "idp_entity_id": "saml_entity_id",
    "idp_sso_url": "saml_sso_url",
    "idp_certificates": "saml_certs",
    "label": "label",
}
SAML_INPUT = frozenset({"idp_entity_id", "idp_sso_url", "idp_certificates", "label", "idp_metadata_xml"})
COPIED = (*INPUT.values(), "issuer", "secret_ciphertext", "secret_key_id", "secret_set_at")
SECRET_MAX = 1000
CLIENT_ID_MAX = 255
DOMAINS_MAX = 50
DETAIL_MAX = 300
LABELS = dict(SsoProvider.choices)
DRAFT_CHANGED = "/problems/sso-draft-changed"
TEST_REQUIRED = "/problems/sso-test-required"
LOCAL_ONLY = "/problems/sso-local-only"
# Where the test sign-in comes back to in the SPA (Admin > Sign-in), with ?sso_test=passed|failed.
TEST_LANDING = "/admin/sign-in"


# Organization.settings key (access and provisioning (b)): an unknown identity at the organization's active
# settings waits as a PendingAccess row for an Admin instead of being refused. Off by default; an Admin changes it in
# Organization settings (catalog/admin_services.update_organization, audited).
AUTO_PROVISION = "sso_auto_provision"


def auto_provision(org) -> bool:
    return (org.settings or {}).get(AUTO_PROVISION) is True


def force_local() -> bool:
    return bool(getattr(settings, "SOFTWARDEN_FORCE_LOCAL_LOGIN", False))


def _local_only() -> Conflict:
    return Conflict(
        "Single sign-on is turned off on this installation (SOFTWARDEN_FORCE_LOCAL_LOGIN). Remove the setting and "
        "restart to test or activate sign-in settings.",
        {"type": LOCAL_ONLY},
    )


def settings_of(org) -> SsoSettings | None:
    return SsoSettings.objects.filter(organization=org).first()


def domains_of(org) -> list[str]:
    """Every sign-in domain the organization claims, verified or pending (what `sign_in_domains` sets)."""
    return list(SsoDomain.objects.filter(organization=org).order_by("domain").values_list("domain", flat=True))


# ---- sign-in domain verification ------------------------------------------------------
# With SOFTWARDEN_SSO_DOMAIN_VERIFICATION (on by default in cloud mode) a claimed domain is pending: it routes no
# sign-in (org_for_sign_in) and blocks nobody, until the organization proves it controls the domain's DNS with a TXT
# record (verify_domain). Only one organization holds a verified domain (a partial unique constraint); a claim pending
# for PENDING_DAYS lapses (the sso.domains_lapse schedule). Every refusal to verify, a missing record, a DNS failure or
# a domain another organization holds, is DOMAIN_UNCONFIRMED in the same words, so it tells nobody who is a customer.
# The service operator moves or releases a domain from the platform console (move_domain, release_domain). Without the
# setting (self-hosted) a claimed domain is verified at once and the first organization keeps it.
CHALLENGE_PREFIX = "_softwarden-challenge"
CHALLENGE_VALUE = "softwarden-domain-verification="
PENDING_DAYS = 14
DOMAIN_UNCONFIRMED = (
    "We couldn't confirm {domain} for this organization. Check that the TXT record shown for it is published (DNS "
    "changes can take a while to appear), then try again. If it still fails, contact support."
)
DOMAIN_UNAVAILABLE = "{domain} can't be a sign-in domain of this organization. Ask the operator if it should be."


def verification_required() -> bool:
    return bool(getattr(settings, "SOFTWARDEN_SSO_DOMAIN_VERIFICATION", False))


def lapses_at(row: SsoDomain):
    return row.created_at + timedelta(days=PENDING_DAYS)


def domain_states(org) -> list[dict[str, Any]]:
    """The organization's sign-in domains with their state: verified, or pending with the TXT record to publish."""
    out = []
    for row in SsoDomain.objects.filter(organization=org).order_by("domain"):
        pending = row.verified_at is None
        out.append(
            {
                "domain": row.domain,
                "status": "pending" if pending else "verified",
                "claimed_at": row.created_at,
                "verified_at": row.verified_at,
                "lapses_at": lapses_at(row) if pending else None,
                "record_name": f"{CHALLENGE_PREFIX}.{row.domain}" if pending else None,
                "record_value": f"{CHALLENGE_VALUE}{row.challenge}" if pending else None,
            }
        )
    return out


def domain_keys(org) -> list[str]:
    """The domains with their state, for the settings' ETag: verifying one changes it."""
    rows = SsoDomain.objects.filter(organization=org).order_by("domain").values_list("domain", "verified_at")
    return [f"{domain}{'' if verified else '?'}" for domain, verified in rows]


def challenge_values(domain: str) -> list[str]:
    """The TXT values every pending claim of `domain` asks for (the simulated resolver, core/dns_txt.py)."""
    rows = SsoDomain.objects.filter(domain=domain, verified_at__isnull=True).values_list("challenge", flat=True)
    return [f"{CHALLENGE_VALUE}{challenge}" for challenge in rows]


def _new_domain(org, domain: str) -> SsoDomain:
    verified = None if verification_required() else timezone.now()
    return SsoDomain.objects.create(
        organization=org, domain=domain, verified_at=verified, challenge=secrets.token_urlsafe(24)
    )


def verify_domain(org, actor, domain: Any) -> dict[str, Any]:
    """Check the TXT record of a pending sign-in domain and, when it carries this claim's value and no other
    organization holds the domain, mark it verified (audited). Answers the domain's state. The DNS lookup runs before
    the transaction; any refusal but "not claimed here" is DOMAIN_UNCONFIRMED."""
    clean = _domain(domain)
    if clean is None:
        raise FieldErrors({"domain": [f"{str(domain)[:60]!r} isn't a domain name, such as example.com."]})
    row = SsoDomain.objects.filter(organization=org, domain=clean).first()
    if row is None:
        raise FieldErrors({"domain": [f"Add {clean} to sign_in_domains first."]})
    if row.verified_at is None:
        expected = f"{CHALLENGE_VALUE}{row.challenge}"
        try:
            found = expected in dns_txt.txt_records(f"{CHALLENGE_PREFIX}.{clean}")
        except dns_txt.DnsError as e:
            log.info("sign-in domain lookup failed org=%s error=%s", org.pk, e)
            found = False
        _confirm(org, actor, row.pk, row.challenge, found)
    state = next((state for state in domain_states(org) if state["domain"] == clean), None)
    if state is None:  # removed from sign_in_domains meanwhile
        raise FieldErrors({"domain": [f"Add {clean} to sign_in_domains first."]})
    return state


@transaction.atomic
def _confirm(org, actor, pk, challenge: str, found: bool) -> None:
    row = SsoDomain.objects.select_for_update().filter(pk=pk, organization=org).first()
    if row is None or row.verified_at is not None:
        return  # removed or verified meanwhile: the answer shows the state now
    unconfirmed = FieldErrors({"domain": [DOMAIN_UNCONFIRMED.format(domain=row.domain)]})
    if not found or row.challenge != challenge:
        raise unconfirmed
    if SsoDomain.objects.filter(domain=row.domain, verified_at__isnull=False).exclude(pk=row.pk).exists():
        raise unconfirmed  # another organization holds it: the same words
    row.verified_at = timezone.now()
    try:
        with transaction.atomic():
            row.save(update_fields=["verified_at", "updated_at"])
    except IntegrityError:  # verified by another organization a moment ago
        raise unconfirmed from None
    settings_row, _created = _locked(org)
    audit(org, actor, "sso_settings.updated", settings_row, {"sign_in_domain_verified": row.domain})


def lapse_pending(now=None) -> dict[str, int]:
    """The `sso.domains_lapse` schedule: forget claims still pending PENDING_DAYS after they were made, audited in
    their organization."""
    cutoff = (now or timezone.now()) - timedelta(days=PENDING_DAYS)
    lapsed = 0
    rows = SsoDomain.objects.filter(verified_at__isnull=True, created_at__lt=cutoff).values_list("organization_id")
    for org_id in sorted({org_id for (org_id,) in rows}):
        with transaction.atomic():
            org = Organization.objects.select_for_update().filter(pk=org_id).first()
            if org is None:
                continue
            stale = SsoDomain.objects.filter(organization=org, verified_at__isnull=True, created_at__lt=cutoff)
            gone = sorted(stale.values_list("domain", flat=True))
            if not gone:
                continue
            row, _created = _locked(org)
            stale.delete()
            audit(org, None, "sso_settings.updated", row, {"sign_in_domains_lapsed": gone, "via": "schedule"})
            lapsed += len(gone)
    return {"lapsed": lapsed}


# ---- the configuration a sign-in uses -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """What one sign-in transaction uses. `mode` is active, test or legacy; (mode, org_id, revision) is pinned in the
    session. `secret` reads the client secret only when the code is redeemed."""

    mode: str
    org_id: str
    revision: int
    provider: Provider
    secret: Callable[[], str]
    saml: SamlIdp | None = None  # the `saml` preset's identity provider (ee/saml/sp.py); None for OpenID Connect

    @property
    def label(self) -> str:
        if self.saml is not None:
            return self.saml.label or LABELS[SsoProvider.SAML]
        return LABELS.get(self.provider.preset, "Single sign-on")


@dataclass(frozen=True)
class SamlIdp:
    """A SAML identity provider as one configuration names it."""

    entity_id: str
    sso_url: str
    certificates: tuple[str, ...]  # base64 DER
    label: str = ""


def _no_secret() -> str:
    return ""


def _simulated_ok(row: SsoSettings, prefix: str) -> bool:
    from .oidc_simulator import allowed

    if getattr(row, f"{prefix}provider") == SsoProvider.SAML:
        if not ee_installed():
            return False  # SAML settings can't be used without the optional `ee` package
        from ee.saml.idp import SIMULATED_ENTITY_ID

        return getattr(row, f"{prefix}saml_entity_id") != SIMULATED_ENTITY_ID or allowed()
    simulated = getattr(row, f"{prefix}tenant") == oidc_discovery.SIMULATED or (
        getattr(row, f"{prefix}discovery_url") == oidc_discovery.SIMULATED_GOOGLE_DISCOVERY
    )
    return not simulated or allowed()


def saml_of(row: SsoSettings, draft: bool) -> SamlIdp | None:
    p = "draft_" if draft else ""
    if getattr(row, f"{p}provider") != SsoProvider.SAML:
        return None
    return SamlIdp(
        entity_id=getattr(row, f"{p}saml_entity_id"),
        sso_url=getattr(row, f"{p}saml_sso_url"),
        certificates=tuple(getattr(row, f"{p}saml_certs") or ()),
        label=getattr(row, f"{p}label"),
    )


def provider_of(row: SsoSettings, draft: bool) -> Provider:
    p = "draft_" if draft else ""
    preset = getattr(row, f"{p}provider")
    if preset == SsoProvider.SAML:
        return Provider(preset=preset, discovery_url="", issuer=getattr(row, f"{p}issuer"), client_id="")
    tenant = getattr(row, f"{p}tenant")
    return Provider(
        preset=preset,
        discovery_url=getattr(row, f"{p}discovery_url"),
        issuer=getattr(row, f"{p}issuer"),
        client_id=getattr(row, f"{p}client_id"),
        tenant=oidc_discovery.entra_tenant(tenant) if preset == "entra" and tenant else "",
        hosted_domains=tuple(getattr(row, f"{p}hosted_domains") or ()),
    )


def legacy_env() -> dict[str, str] | None:
    """The installation's legacy Entra settings (SOFTWARDEN_OIDC_*), when all three are set and the tenant is a GUID
    (or `simulated` where the simulator is allowed)."""
    from .oidc_simulator import allowed

    tenant = os.environ.get("SOFTWARDEN_OIDC_TENANT", "").strip()
    client_id = os.environ.get("SOFTWARDEN_OIDC_CLIENT_ID", "").strip()
    secret = os.environ.get("SOFTWARDEN_OIDC_CLIENT_SECRET", "")
    if not (tenant and client_id and secret):
        return None
    if tenant == oidc_discovery.SIMULATED:
        return {"tenant": tenant, "client_id": client_id, "secret": secret} if allowed() else None
    if not oidc_discovery.GUID.match(tenant):
        return None
    return {"tenant": tenant, "client_id": client_id, "secret": secret}


def _legacy_revision(env: dict[str, str]) -> int:
    """A fingerprint of the legacy settings, pinned like a revision: a changed environment refuses a callback."""
    return int(hashlib.sha256(f"{env['tenant']}|{env['client_id']}".encode()).hexdigest()[:8], 16)


def legacy_org() -> Organization | None:
    """The one organization the legacy settings can serve: the installation's only one."""
    orgs = list(Organization.objects.order_by("pk")[:2])
    return orgs[0] if len(orgs) == 1 else None


def _legacy(org, row: SsoSettings | None) -> Config | None:
    env = legacy_env()
    if env is None or (row is not None and row.status != SsoStatus.LEGACY):
        return None
    only = legacy_org()
    if only is None or only.pk != org.pk:
        return None
    discovery_url, issuer = oidc_discovery.urls("entra", tenant=env["tenant"])
    provider = Provider(
        "entra", discovery_url, issuer, env["client_id"], tenant=oidc_discovery.entra_tenant(env["tenant"])
    )
    return Config("legacy", str(org.pk), _legacy_revision(env), provider, lambda: env["secret"])


def effective(org) -> Config | None:
    """How this organization's members sign in with SSO right now, or None (local sign-in only)."""
    if org is None or force_local() or not has_capability(org, SSO_OIDC):
        return None
    row = settings_of(org)
    if row is not None and row.status == SsoStatus.ACTIVE and row.provider == SsoProvider.SAML:
        # The capability first: without the optional `ee` package no organization has it, and nothing below runs.
        if not has_capability(org, SSO_SAML):
            return None
        if not (row.issuer and row.saml_sso_url and row.saml_certs and _simulated_ok(row, "")):
            return None
        provider = provider_of(row, draft=False)
        return Config("active", str(org.pk), row.active_revision, provider, _no_secret, saml_of(row, draft=False))
    if row is not None and row.status == SsoStatus.ACTIVE:
        if not (row.provider and row.client_id and row.secret_ciphertext and _simulated_ok(row, "")):
            return None
        return Config(
            "active",
            str(org.pk),
            row.active_revision,
            provider_of(row, draft=False),
            lambda: credentials.slot_value(row, "secret"),
        )
    return _legacy(org, row)


def _test_config(row: SsoSettings) -> Config:
    saml = saml_of(row, draft=True)
    return Config(
        "test",
        str(row.organization_id),
        row.draft_revision,
        provider_of(row, draft=True),
        _no_secret if saml is not None else lambda: credentials.slot_value(row, "draft_secret"),
        saml,
    )


def config_for(pending: dict[str, Any]) -> Config | None:
    """The configuration a pinned transaction started with, while it still is the current one; else None."""
    if force_local():
        return None
    org = Organization.objects.filter(pk=str(pending["org"])).first() if pending.get("org") else None
    if org is None:
        return None
    mode = pending.get("mode")
    if mode == "test":
        row = settings_of(org)
        if row is None or not row.draft_revision or row.draft_revision != pending.get("revision"):
            return None
        return _test_config(row)
    current = effective(org)
    if current is None or current.mode != mode or current.revision != pending.get("revision"):
        return None
    return current


def org_for_sign_in(slug: str | None = None, email: str | None = None) -> Organization | None:
    """The organization a sign-in page is for: by `?org=<slug>`, else by the work email's domain against the sign-in
    domains, else the installation's only organization. None when nothing matches (local sign-in only)."""
    if slug:
        return Organization.objects.filter(slug=slug.strip().lower()[:60]).first()
    if email:
        domain = email.strip().lower().rsplit("@", 1)[-1] if "@" in email else ""
        # A pending claim routes nobody: only a verified domain finds an organization.
        verified = SsoDomain.objects.filter(domain=domain[:253], verified_at__isnull=False)
        found = verified.select_related("organization").first()
        return found.organization if found is not None and domain else None
    return legacy_org()


# ---- validation ---------------------------------------------------------------------------------------------------


def _text(value: Any, key: str, errors: dict, limit: int) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[key] = ["Enter text."]
        return ""
    value = value.strip()
    if len(value) > limit:
        errors[key] = [f"Use {limit} characters or fewer."]
    return value


def _domain(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower().rstrip(".")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return value if oidc_discovery.DOMAIN.match(value) else None


def _domains(value: Any, key: str, errors: dict, limit: int = DOMAINS_MAX) -> list[str]:
    if not isinstance(value, list):
        errors[key] = ["Send a list of domain names."]
        return []
    out: list[str] = []
    for item in value:
        domain = _domain(item)
        if domain is None:
            errors[key] = [f"{str(item)[:60]!r} isn't a domain name, such as example.com."]
            return []
        if domain not in out:
            out.append(domain)
    if len(out) > limit:
        errors[key] = [f"Enter at most {limit} domains."]
    return sorted(out)


GOOGLE_TEST_DOMAINS = (
    "Workspace domains under .test are for development only: they select the simulated Google provider, which this "
    "installation doesn't allow. Enter your Google Workspace domain."
)
ENTRA_THROUGH_GENERIC = (
    "This is a Microsoft Entra ID address. Choose the Microsoft Entra ID provider instead and enter your directory "
    "(tenant) ID: it accepts sign-ins from your tenant only."
)


def _set_values(row: SsoSettings, data: dict[str, Any], errors: dict) -> None:
    from .oidc_simulator import allowed

    if "provider" in data:
        if data["provider"] not in SsoProvider.values:
            errors["provider"] = [f"Choose one of {', '.join(SsoProvider.values)}."]
        else:
            row.draft_provider = data["provider"]
    if "tenant_id" in data:
        tenant = _text(data["tenant_id"], "tenant_id", errors, 64).lower()
        if "tenant_id" not in errors and tenant:
            if tenant in oidc_discovery.NOT_TENANTS:
                errors["tenant_id"] = [
                    "Enter your tenant's directory (tenant) ID; multi-tenant endpoints like common aren't allowed."
                ]
            elif not (oidc_discovery.GUID.match(tenant) or (tenant == oidc_discovery.SIMULATED and allowed())):
                errors["tenant_id"] = ["Enter the directory (tenant) ID, a GUID."]
        row.draft_tenant = tenant
    if "okta_domain" in data:
        raw = _text(data["okta_domain"], "okta_domain", errors, 253)
        domain = _domain(raw) if raw else ""
        if "okta_domain" not in errors and domain is None:
            errors["okta_domain"] = ["Enter the Okta domain only, such as example.okta.com."]
        row.draft_okta_domain = domain or ""
    if "okta_authorization_server" in data:
        server = _text(data["okta_authorization_server"], "okta_authorization_server", errors, 100)
        if "okta_authorization_server" not in errors and server and not oidc_discovery.OKTA_SERVER.match(server):
            errors["okta_authorization_server"] = ["Enter the authorization server ID, such as default."]
        row.draft_okta_server = server
    if "discovery_url" in data:
        url = _text(data["discovery_url"], "discovery_url", errors, 2000)
        if "discovery_url" not in errors and url:
            try:
                outbound.check_service_url(url, max_length=2000)
            except outbound.UnsafeDestination as e:
                errors["discovery_url"] = [str(e)]
            else:
                if not url.endswith(WELL_KNOWN) or len(url) - len(WELL_KNOWN) > ISSUER_MAX:
                    errors["discovery_url"] = [f"Enter the issuer's address followed by {WELL_KNOWN}."]
        row.draft_discovery_url = url
    if "hosted_domains" in data:
        row.draft_hosted_domains = _domains(data["hosted_domains"], "hosted_domains", errors, 20)
    if (
        row.draft_provider == "google"
        and "hosted_domains" not in errors
        and oidc_discovery.google_simulated(row.draft_hosted_domains)
        and not allowed()
    ):
        errors["hosted_domains"] = [GOOGLE_TEST_DOMAINS]
    if "client_id" in data:
        row.draft_client_id = _text(data["client_id"], "client_id", errors, CLIENT_ID_MAX)
    _set_saml_values(row, data, errors)
    if row.draft_provider == "entra" and row.draft_client_id and "client_id" not in errors:
        if not oidc_discovery.GUID.match(row.draft_client_id):
            errors["client_id"] = ["Enter the application (client) ID, a GUID."]
    # A preset computes its discovery URL and issuer (a discovery_url sent with one is ignored); a generic provider's
    # issuer is its discovery URL without the well-known suffix.
    preset = row.draft_provider
    if preset == SsoProvider.SAML:
        # Identities are pinned on the IdP's entity ID namespaced by this organization.
        entity = row.draft_saml_entity_id
        row.draft_issuer = oidc_discovery.saml_issuer(row.organization_id, entity) if entity else ""
    elif preset == "oidc":
        row.draft_issuer = oidc_discovery.urls("oidc", discovery_url=row.draft_discovery_url)[1]
        if "discovery_url" not in errors and oidc_discovery.entra_login_host(row.draft_discovery_url):
            # A tenant-specific discovery URL does pin `iss`, but Entra documents no `email_verified` claim
            # and its `email` is mutable and set by administrators (a B2B guest's comes from their home directory), so
            # the generic preset's email linking can't be trusted. The entra preset keys identities on `tid` + `oid`.
            errors["discovery_url"] = [ENTRA_THROUGH_GENERIC]
    elif preset:
        complete = {"entra": bool(row.draft_tenant), "okta": bool(row.draft_okta_domain), "google": True}[preset]
        url, issuer = (
            oidc_discovery.urls(
                preset,
                row.draft_tenant,
                row.draft_okta_domain,
                row.draft_okta_server,
                hosted_domains=row.draft_hosted_domains,
            )
            if complete
            else ("", "")
        )
        row.draft_discovery_url, row.draft_issuer = url, issuer


def _set_saml_values(row: SsoSettings, data: dict[str, Any], errors: dict) -> None:
    """The SAML values (ee/saml/idp.py validates them). Pasted metadata fills the entity ID, sign-in URL and
    certificates; any of those sent beside it win."""
    if not SAML_INPUT & set(data):
        return
    if not ee_installed():  # _require_saml refused already; never reached without the package
        raise CapabilityRequired(SSO_SAML)
    from ee.saml import idp

    if "idp_metadata_xml" in data:
        try:
            imported = idp.parse_metadata(data["idp_metadata_xml"])
        except idp.Invalid as e:
            errors["idp_metadata_xml"] = [str(e)]
        else:
            row.draft_saml_entity_id = imported["entity_id"]
            row.draft_saml_sso_url = imported["sso_url"]
            row.draft_saml_certs = imported["certificates"]
    cleaners: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "idp_entity_id": ("draft_saml_entity_id", idp.clean_entity_id),
        "idp_sso_url": ("draft_saml_sso_url", idp.clean_sso_url),
        "idp_certificates": ("draft_saml_certs", idp.clean_certificates),
        "label": ("draft_label", idp.clean_label),
    }
    for key, (column, clean) in cleaners.items():
        if key not in data:
            continue
        try:
            setattr(row, column, clean(data[key]))
        except idp.Invalid as e:
            errors[key] = [str(e)]


def _audited(values: dict[str, Any]) -> dict[str, Any]:
    """Draft values for an audit payload: certificates by fingerprint, not their whole text."""
    if not values.get("idp_certificates"):
        return values
    # The certificate's SHA-256 fingerprint, short (the same as the `ee` package's, without needing it).
    return {**values, "idp_certificates": [_fingerprint(c) for c in values["idp_certificates"]]}


def _fingerprint(cert: str) -> str:
    import base64

    try:
        return hashlib.sha256(base64.b64decode(cert)).hexdigest()[:16]
    except ValueError:
        return "unreadable"


def _require_saml(org) -> None:
    if not has_capability(org, SSO_SAML):
        raise CapabilityRequired(SSO_SAML)


def _secret(row: SsoSettings, data: dict[str, Any], errors: dict) -> list[str]:
    if "client_secret" not in data:
        return []
    value = data["client_secret"]
    if not isinstance(value, str) or not value.strip():
        errors["client_secret"] = ["Enter the client secret, or leave the field out to keep the stored one."]
        return []
    if len(value.strip()) > SECRET_MAX:
        errors["client_secret"] = [f"Use {SECRET_MAX} characters or fewer."]
        return []
    try:
        credentials.slot_set(row, "draft_secret", value.strip())
    except credentials.CredentialError as e:
        errors["client_secret"] = [str(e)]
        return []
    return ["client_secret"]


def missing(row: SsoSettings) -> dict[str, list[str]]:
    """What the draft lacks before it can be tested."""
    errors: dict[str, list[str]] = {}
    preset = row.draft_provider
    if not preset:
        return {"provider": ["Choose a provider first."]}
    if preset == SsoProvider.SAML:
        saml_need = {
            "idp_entity_id": (row.draft_saml_entity_id, "Enter the identity provider's entity ID."),
            "idp_sso_url": (row.draft_saml_sso_url, "Enter the identity provider's sign-in URL (HTTP-Redirect)."),
            "idp_certificates": (row.draft_saml_certs, "Add the identity provider's signing certificate."),
        }
        return {key: [message] for key, (value, message) in saml_need.items() if not value}
    need: dict[str, tuple[str, Any]] = {"entra": ("tenant_id", row.draft_tenant)}
    need["okta"] = ("okta_domain", row.draft_okta_domain)
    need |= {"google": ("hosted_domains", row.draft_hosted_domains), "oidc": ("discovery_url", row.draft_discovery_url)}
    key, value = need[preset]
    if not value:
        errors[key] = ["This is required for this provider."]
    if not row.draft_client_id:
        errors["client_id"] = ["Enter the client ID."]
    if not row.draft_secret_ciphertext:
        errors["client_secret"] = ["Enter the client secret."]
    return errors


def draft_values(row: SsoSettings) -> dict[str, Any]:
    return {key: getattr(row, f"draft_{column}") for key, column in INPUT.items()}


def _locked(org) -> tuple[SsoSettings, bool]:
    row = SsoSettings.objects.select_for_update().filter(organization=org).first()
    if row is not None:
        return row, False
    try:
        with transaction.atomic():
            SsoSettings.objects.create(organization=org)
    except IntegrityError:  # another request created it first
        pass
    return SsoSettings.objects.select_for_update().get(organization=org), True


# ---- writes -------------------------------------------------------------------------------------------------------


@transaction.atomic
def update_draft(org, actor, data: dict[str, Any]) -> SsoSettings:
    """Change the draft (and the sign-in domains). Nothing that signs people in changes."""
    errors: dict[str, list[str]] = {}
    for key in sorted(set(data) - set(INPUT) - SAML_INPUT - {"client_secret", "sign_in_domains"}):
        errors[key] = ["This isn't a sign-in setting."]
    if data.get("provider") == SsoProvider.SAML or SAML_INPUT & set(data):
        _require_saml(org)  # before anything is written, a community organization's row included
    row, created = _locked(org)
    before = draft_values(row)
    revision_before = row.draft_revision
    destination_before, had_secret = outbound.origin(row.draft_discovery_url), bool(row.draft_secret_ciphertext)
    _set_values(row, data, errors)
    replaced = _secret(row, data, errors)
    # The draft client secret goes to the token endpoint the discovery document names; a provider, Okta domain
    # or discovery URL that moves the discovery document to another scheme, host or port needs it entered again.
    if had_secret and "client_secret" not in data and not errors:
        if outbound.secret_must_follow(destination_before, outbound.origin(row.draft_discovery_url)):
            errors["client_secret"] = ["The identity provider's address changed: enter the client secret again."]
    domains_before = domains_of(org)
    domains = domains_before
    if "sign_in_domains" in data:
        domains = _domains(data["sign_in_domains"], "sign_in_domains", errors)
        # With verification a new domain is a pending claim that blocks nobody, so nothing is refused here and
        # nothing tells whether another organization holds it. Without it the first organization keeps a domain.
        if not verification_required():
            taken = SsoDomain.objects.filter(domain__in=set(domains) - set(domains_before)).exclude(organization=org)
            claimed = sorted(taken.values_list("domain", flat=True))
            if claimed and "sign_in_domains" not in errors:
                errors["sign_in_domains"] = [DOMAIN_UNAVAILABLE.format(domain=", ".join(claimed))]
    if errors:
        raise FieldErrors(errors)
    after = draft_values(row)
    shown_before, shown_after = _audited(before), _audited(after)
    changes: dict[str, Any] = {k: [shown_before[k], shown_after[k]] for k in INPUT if before[k] != after[k]}
    if changes and "idp_metadata_xml" in data:
        changes["imported_metadata"] = True
    if changes or replaced:
        row.draft_revision += 1
        changes["revision"] = [revision_before, row.draft_revision]
    if replaced:
        changes["replaced"] = replaced
    if domains != domains_before:
        SsoDomain.objects.filter(organization=org).exclude(domain__in=domains).delete()
        for domain in sorted(set(domains) - set(domains_before)):
            try:
                with transaction.atomic():
                    _new_domain(org, domain)
            except IntegrityError:  # claimed by another organization a moment ago (verified at once: self-hosted)
                raise FieldErrors({"sign_in_domains": [DOMAIN_UNAVAILABLE.format(domain=domain)]}) from None
        changes["sign_in_domains"] = [domains_before, domains]
    if not changes and not created:
        return row
    row.save()
    if created:
        changes["created"] = True
    audit(org, actor, "sso_settings.updated", row, changes)
    return row


@transaction.atomic
def record_test(org_id, revision: int, actor, ok: bool, code: str, detail: str) -> bool:
    """Record a test sign-in's result on draft `revision`. A draft changed since keeps its state; the audit event
    says the result was for an older revision (`current: false`). Returns whether it was recorded."""
    row = SsoSettings.objects.select_for_update().filter(organization_id=org_id).first()
    if row is None:
        return False
    current = row.draft_revision == revision
    if current:
        row.draft_tested_revision = revision
        row.draft_test_ok = ok
        row.draft_tested_at = timezone.now()
        row.draft_tested_by = actor
        row.draft_test_code = "" if ok else (code or "failed")[:60]
        row.draft_test_detail = detail[:DETAIL_MAX]
        row.save()
    payload = {"ok": ok, "code": "" if ok else code, "revision": revision, "current": current}
    audit(row.organization, actor, "sso_settings.tested", row, payload)
    return current


def start_test(org, actor, session, draft_revision: Any) -> dict[str, Any]:
    """Start a test sign-in of the current draft for this Admin. Returns {"authorize_url"} (None when the provider's
    discovery document already failed; the failure is recorded on the draft)."""
    from . import oidc

    if force_local():
        raise _local_only()
    with transaction.atomic():
        row = SsoSettings.objects.select_for_update().filter(organization=org).first()
        if row is None or not row.draft_revision:
            raise FieldErrors({"provider": ["Save a draft first."]})
        _same_draft(row, draft_revision)
        if row.draft_provider == SsoProvider.SAML:
            _require_saml(org)
        problems = missing(row)
        if problems:
            raise FieldErrors(problems)
        if not _simulated_ok(row, "draft_"):
            if row.draft_provider == SsoProvider.SAML:
                raise FieldErrors({"idp_entity_id": ["This identity provider can't be used on this installation."]})
            if row.draft_provider == "google":
                raise FieldErrors({"hosted_domains": [GOOGLE_TEST_DOMAINS]})
            raise FieldErrors({"tenant_id": ["Enter the directory (tenant) ID, a GUID."]})
        config = _test_config(row)
    if config.saml is not None:
        from ee.saml import sp

        try:
            return {"authorize_url": sp.begin(session, config, None, admin_id=actor.pk)}
        except sp.SamlSetupError as e:
            record_test(org.pk, config.revision, actor, False, "settings", f"The test didn't start: {e}")
            return {"authorize_url": None}
    try:
        url = oidc.begin(session, config, None, admin_id=actor.pk)
    except ProviderError as e:
        record_test(org.pk, config.revision, actor, False, e.code, f"The test didn't start: {e}")
        return {"authorize_url": None}
    return {"authorize_url": url}


def _same_draft(row: SsoSettings, draft_revision: Any) -> None:
    if not isinstance(draft_revision, int) or isinstance(draft_revision, bool):
        raise FieldErrors({"draft_revision": ["Send the draft revision you reviewed (draft.revision)."]})
    if draft_revision != row.draft_revision:
        raise Conflict(
            "The draft changed since you read it. Review it again.",
            {"type": DRAFT_CHANGED, "revision": str(row.draft_revision)},
        )


@transaction.atomic
def activate(org, actor, draft_revision: Any, via: str = "api") -> SsoSettings:
    """Make the tested draft the configuration sign-in uses. Serialized on the settings row."""
    if force_local():
        raise _local_only()
    row = SsoSettings.objects.select_for_update().filter(organization=org).first()
    if row is None or not row.draft_revision:
        raise Conflict("There is no draft to activate.", {"type": TEST_REQUIRED})
    _same_draft(row, draft_revision)
    if row.draft_provider == SsoProvider.SAML:
        _require_saml(org)
    if not row.draft_is_tested:
        raise Conflict(
            "Only a draft that passed a test sign-in, unchanged since, can be activated. Run the test first.",
            {"type": TEST_REQUIRED},
        )
    before = {"status": row.status, "active_revision": row.active_revision, "provider": row.provider}
    for column in COPIED:
        setattr(row, column, getattr(row, f"draft_{column}"))
    row.active_revision += 1
    row.status = SsoStatus.ACTIVE
    row.activated_at = timezone.now()
    row.activated_by = actor
    row.save()
    changes = {
        "status": [before["status"], row.status],
        "provider": [before["provider"], row.provider],
        "active_revision": [before["active_revision"], row.active_revision],
        "draft_revision": row.draft_revision,
        "via": via,
    }
    audit(org, actor, "sso_settings.activated", row, changes)
    return row


@transaction.atomic
def set_status(org, actor, status: str, via: str = "api") -> tuple[SsoSettings, bool]:
    """Turn SSO off (`disabled`) or hand it back to the legacy environment settings (`legacy`). Returns (row,
    changed)."""
    row, _created = _locked(org)
    before = row.status
    if before == status:
        return row, False
    row.status = status
    row.save()
    action = "sso_settings.disabled" if status == SsoStatus.DISABLED else "sso_settings.legacy_restored"
    audit(org, actor, action, row, {"status": [before, status], "via": via})
    return row, True


def disable(org, actor, via: str = "api") -> tuple[SsoSettings, bool]:
    return set_status(org, actor, SsoStatus.DISABLED, via)


def use_env(org, actor=None, via: str = "cli") -> tuple[SsoSettings, bool]:
    return set_status(org, actor, SsoStatus.LEGACY, via)


def _change_domains(org, actor, via: str, change: Callable[[], object]) -> None:
    row, _created = _locked(org)
    before = domains_of(org)
    change()
    after = domains_of(org)
    if before != after:
        audit(org, actor, "sso_settings.updated", row, {"sign_in_domains": [before, after], "via": via})


def _clean_domain(domain: str) -> str:
    clean = _domain(domain)
    if clean is None:
        raise FieldErrors({"domain": [f"{str(domain)[:60]!r} isn't a domain name, such as example.com."]})
    return clean


@transaction.atomic
def move_domain(domain: str, org, actor=None, via: str = "cli"):
    """Give a sign-in domain to `org`, verified, taking it from the organization that holds it verified (an operator
    freeing a squatted domain: `sso_recovery --move-domain`, the platform console). Other organizations' pending claims
    of it are left to fail or lapse. Audited in both. Returns the organization it came from, or None."""
    clean = _clean_domain(domain)
    rows = list(SsoDomain.objects.select_for_update().select_related("organization").filter(domain=clean))
    held = next((row for row in rows if row.verified_at is not None), None)
    own = next((row for row in rows if row.organization_id == org.pk), None)
    if held is not None and held.organization_id == org.pk:
        return held.organization
    previous = held.organization if held is not None else None
    if held is not None:
        held_pk = held.pk
        _change_domains(held.organization, actor, via, lambda: SsoDomain.objects.filter(pk=held_pk).delete())
    if own is not None:  # its own pending claim: verified by the operator
        SsoDomain.objects.filter(pk=own.pk).update(verified_at=timezone.now(), updated_at=timezone.now())
        row, _created = _locked(org)
        audit(org, actor, "sso_settings.updated", row, {"sign_in_domain_verified": clean, "via": via})
    else:
        _change_domains(org, actor, via, lambda: _verified_domain(org, clean))
    return previous


def _verified_domain(org, domain: str) -> SsoDomain:
    return SsoDomain.objects.create(
        organization=org, domain=domain, verified_at=timezone.now(), challenge=secrets.token_urlsafe(24)
    )


@transaction.atomic
def release_domain(domain: str, actor=None, via: str = "cli"):
    """Free a sign-in domain (`sso_recovery --release-domain`, the platform console): the verified claim, or when
    nobody holds it verified, the oldest pending one. Audited in the organization that had it, which is returned
    (None when nobody claimed it)."""
    clean = _clean_domain(domain)
    rows = list(
        SsoDomain.objects.select_for_update().select_related("organization").filter(domain=clean).order_by("created_at")
    )
    held = next((row for row in rows if row.verified_at is not None), rows[0] if rows else None)
    if held is None:
        return None
    pk = held.pk
    _change_domains(held.organization, actor, via, lambda: SsoDomain.objects.filter(pk=pk).delete())
    return held.organization


# ---- reading ------------------------------------------------------------------------------------------------------


def state(org) -> dict[str, str]:
    """How this organization signs in with SSO now: {"source", "provider", "detail"} (the settings page, the
    recovery command). source: force_local, capability, active, legacy, disabled or off."""
    row = settings_of(org)
    if force_local():
        return {"source": "force_local", "provider": "", "detail": "Turned off by SOFTWARDEN_FORCE_LOCAL_LOGIN."}
    if not has_capability(org, SSO_OIDC):
        return {"source": "capability", "provider": "", "detail": "This organization's edition has no SSO."}
    if row is not None and row.status == SsoStatus.ACTIVE and row.provider == SsoProvider.SAML:
        if not has_capability(org, SSO_SAML):
            return {
                "source": "capability",
                "provider": "saml",
                "detail": "SAML single sign-on isn't part of this release.",
            }
    current = effective(org)
    if current is not None:
        where = "these settings" if current.mode == "active" else "SOFTWARDEN_OIDC_* (legacy)"
        return {"source": current.mode, "provider": current.provider.preset, "detail": f"Signs in with {where}."}
    if row is not None and row.status == SsoStatus.DISABLED:
        return {"source": "disabled", "provider": "", "detail": "Turned off for this organization."}
    if row is not None and row.status == SsoStatus.ACTIVE:
        return {"source": "off", "provider": row.provider, "detail": "The active settings can't be used."}
    if legacy_env() is not None and legacy_org() is None:
        return {"source": "off", "provider": "", "detail": "SOFTWARDEN_OIDC_* applies only to a single organization."}
    return {"source": "off", "provider": "", "detail": "Not set up."}


def local_admins(org) -> int:
    """Active Admins of the organization who can sign in with a password (the recovery command prints it)."""
    rows = Membership.objects.filter(organization=org, role=Role.ADMIN, user__is_active=True).select_related("user")
    return sum(1 for m in rows if m.user.has_usable_password())
