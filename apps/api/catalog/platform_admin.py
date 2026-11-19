"""The platform admin console's services (routes in catalog/platform_views.py, access in
core/platform_access.py, impersonation's session mechanics in core/impersonation.py).

Every write validates, scopes and audits in one transaction, like every tenant write. Platform actions are written
to the installation-wide PlatformAuditEvent stream (`platform_audit`); an action that touches an organization (its
edition, suspension, scheduled deletion, capability overrides, an impersonation of one of its members) also writes an
AuditEvent in that organization, with the staff account as the actor and `via: platform` plus the staff's reason in
the payload, so the organization's Admins see that the operator acted. The same holds for its limit overrides
(core/limits.py) and an export made on its request through support (the zip is downloadable only by the organization's
own Admins, never through the platform API).

Platform staff are granted and revoked only by `manage.py platform_staff` (grant_staff, revoke_staff). There is no
platform hard delete: an organization goes away only through its own lifecycle (catalog/org_lifecycle.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from core import capabilities, impersonation, limits, sso
from core.access import authenticated_at
from core.drf import timestamp
from core.models import (
    CapabilityOverride,
    ExportPurpose,
    Membership,
    Organization,
    OrganizationLimitOverride,
    OrgExport,
    OverrideEffect,
    PlatformAuditEvent,
    PlatformStaff,
    SsoDomain,
)
from core.platform_access import is_platform_staff
from core.problems import Conflict, FieldErrors, ImpersonationRestricted
from core.request_context import acting_via, current_request_id, forget_organization, impersonating

from . import org_lifecycle
from .redaction import redact
from .services import audit, audit_account

REASON_MAX = 500

# Every action the platform stream records, with its label (tests/test_platform.py checks each one written is here).
PLATFORM_ACTIONS = {
    "staff.granted": "Platform staff granted",
    "staff.revoked": "Platform staff revoked",
    "staff.signed_in": "Platform staff signed in",
    "staff.sign_in_failed": "Platform staff sign-in failed",
    "staff.password_check_failed": "Platform staff password refused",
    "staff.second_factor_enrollment_started": "Second factor enrollment started",
    "staff.second_factor_enrolled": "Second factor set up",
    "staff.second_factor_verified": "Second factor code accepted",
    "staff.second_factor_failed": "Second factor code refused",
    "staff.second_factor_reset": "Second factor reset by the operator",
    "signin.failures_forgotten": "Failed sign-ins cleared for an account",
    "organization.edition_changed": "Edition changed",
    "organization.deletion_cancelled": "Scheduled deletion canceled",
    "organization.suspended": "Organization suspended",
    "organization.unsuspended": "Organization unsuspended",
    "capability.granted": "Capability granted",
    "capability.revoked": "Capability revoked",
    "capability.override_removed": "Capability override removed",
    "limit.set": "Limit set",
    "limit.override_removed": "Limit override removed",
    "export.requested": "Data export made for the organization",
    "sign_in_domain.moved": "Sign-in domain moved to the organization",
    "sign_in_domain.released": "Sign-in domain released",
    "impersonation.started": "Impersonation started",
    "impersonation.stopped": "Impersonation stopped",
    "impersonation.expired": "Impersonation expired",
    "backup.key_acknowledged": "Backup key acknowledged",
}


def platform_audit(actor, action: str, organization_id=None, details: dict[str, Any] | None = None):
    """Write one PlatformAuditEvent in the caller's transaction. `details` is redacted like an audit payload."""
    if action not in PLATFORM_ACTIONS:
        raise ValueError(f"Unknown platform action {action!r}; add it to PLATFORM_ACTIONS.")
    return PlatformAuditEvent.objects.create(
        actor=actor,
        actor_email=getattr(actor, "email", "") or "",
        action=action,
        organization_id=organization_id,
        details=redact(details or {}),
        request_id=current_request_id(),
    )


def clean_reason(data: dict[str, Any]) -> str:
    reason = data.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise FieldErrors({"reason": ["Say why; the organization's Admins see it in their audit log."]})
    if len(reason) > REASON_MAX:
        raise FieldErrors({"reason": [f"Keep it to {REASON_MAX} characters."]})
    return reason


# ---- platform staff (manage.py platform_staff) ---------------------------------------------------------------------


def find_user(email: str):
    """The account with this email (case-insensitive), or None."""
    return get_user_model().objects.filter(email__iexact=(email or "").strip()).order_by("pk").first()


@transaction.atomic
def grant_staff(user) -> bool:
    """Make `user` platform staff. Audited in the platform stream (the CLI has no actor). Returns whether it changed."""
    if not user.is_active:
        raise FieldErrors({"email": ["This account is deactivated; reactivate it first."]})
    _, created = PlatformStaff.objects.get_or_create(user=user)
    if created:
        platform_audit(None, "staff.granted", details={"user": user.pk, "email": user.email, "via": "cli"})
    return created


@transaction.atomic
def revoke_staff(user) -> bool:
    """Remove `user` from platform staff; an impersonation they hold ends on its next request. Returns whether it
    changed."""
    removed, _ = PlatformStaff.objects.filter(user=user).delete()
    if removed:
        platform_audit(None, "staff.revoked", details={"user": user.pk, "email": user.email, "via": "cli"})
    return bool(removed)


# ---- the installation ----------------------------------------------------------------------------------------------


@transaction.atomic
def acknowledge_backup_key(staff) -> bool:
    """Platform staff confirm the generated backup key is copied off the host (core/backup.acknowledge_key with no
    organization: every organization's log records it with no actor and `via: platform`). Audited in the platform
    stream when it changed anything. Returns whether it did."""
    from core import backup

    changed = backup.acknowledge_key(None, staff)
    if changed:
        platform_audit(staff, "backup.key_acknowledged", details={"key_source": "generated"})
    return changed


# ---- organizations -------------------------------------------------------------------------------------------------


def _locked(org) -> Organization:
    return Organization.objects.select_for_update().get(pk=org.pk)


@transaction.atomic
def change_edition(staff, org, data: dict[str, Any]) -> tuple[Organization, bool]:
    """Set the organization's edition (core/capabilities.set_edition). Returns (organization, changed); an unchanged
    edition writes nothing."""
    reason = clean_reason(data)
    edition = data.get("edition")
    before = org.edition
    with acting_via(via="platform", reason=reason):
        org, changed = capabilities.set_edition(org, edition, actor=staff, via="platform")
    if changed:
        details = {"edition": [before, org.edition], "reason": reason}
        platform_audit(staff, "organization.edition_changed", org.pk, details)
    return org, changed


@transaction.atomic
def cancel_deletion(staff, org, data: dict[str, Any]) -> Organization:
    """Cancel a scheduled deletion (catalog/org_lifecycle.cancel_deletion); 409 when none is scheduled."""
    reason = clean_reason(data)
    due = org.deletion_due_at
    with acting_via(via="platform", reason=reason):
        org = org_lifecycle.cancel_deletion(org, staff)
    details = {"due_at": timestamp(due), "reason": reason}
    platform_audit(staff, "organization.deletion_cancelled", org.pk, details)
    return org


@transaction.atomic
def suspend(staff, org, data: dict[str, Any]) -> Organization:
    """Suspend the organization: its members are refused on every request but `auth/me` (core/drf.RolePermission) and
    the worker leaves its jobs queued (core/jobs.claim). Its data is kept. A scheduled deletion is paused: the purge
    skips a suspended organization, and unsuspend gives back the time that was left; both
    events say so (`deletion_paused`, `deletion_due_at`). 409 when already suspended."""
    reason = clean_reason(data)
    org = _locked(org)
    if org.suspended_at is not None:
        raise Conflict("This organization is already suspended.")
    org.suspended_at = timezone.now()
    org.suspended_reason = reason
    org.save(update_fields=["suspended_at", "suspended_reason"])
    forget_organization(org.pk)
    changes: dict[str, Any] = {"suspended": True}
    if org.deletion_due_at is not None:
        changes.update(deletion_paused=True, deletion_due_at=timestamp(org.deletion_due_at))
    with acting_via(via="platform", reason=reason):
        audit(org, staff, "organization.suspended", org, changes)
    platform_audit(staff, "organization.suspended", org.pk, {**changes, "reason": reason})
    return org


@transaction.atomic
def unsuspend(staff, org, data: dict[str, Any]) -> Organization:
    """Lift the suspension. A deletion it paused resumes with the time it had left, at least MIN_GRACE_DAYS
    (org_lifecycle.resume_deletion, audited as organization.deletion_postponed). 409 when it isn't suspended."""
    reason = clean_reason(data)
    org = _locked(org)
    if org.suspended_at is None:
        raise Conflict("This organization isn't suspended.")
    suspended_at = org.suspended_at
    org.suspended_at = None
    org.suspended_reason = ""
    org.save(update_fields=["suspended_at", "suspended_reason"])
    forget_organization(org.pk)
    changes: dict[str, Any] = {"suspended": False}
    # Outside acting_via, whose `reason` would replace the postponement's own (`suspension`).
    due = org_lifecycle.resume_deletion(org, suspended_at)
    if due is not None:
        changes.update(deletion_resumed=True, deletion_due_at=timestamp(due))
    with acting_via(via="platform", reason=reason):
        audit(org, staff, "organization.unsuspended", org, changes)
    platform_audit(staff, "organization.unsuspended", org.pk, {**changes, "reason": reason})
    return org


# ---- capability overrides (feature flags) ---------------------------------------------------------------------------


def _expiry(value, now) -> datetime | None:
    if value in (None, ""):
        return None
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or timezone.is_naive(parsed):
        raise FieldErrors({"expires_at": ["Give a date and time with its offset, such as 2026-12-31T23:59:00Z."]})
    if parsed <= now:
        raise FieldErrors({"expires_at": ["Choose a time in the future, or leave it out for no expiry."]})
    return parsed


@transaction.atomic
def set_override(staff, org, data: dict[str, Any]) -> CapabilityOverride:
    """Grant or revoke one capability for this organization, replacing any override of the same name. Applies from
    the next request (core/capabilities.capabilities)."""
    now = timezone.now()
    errors: dict[str, list[str]] = {}
    try:
        name = capabilities.check_capability_name(data.get("capability"))
    except FieldErrors as e:
        errors.update(e.errors)
        name = ""
    effect = data.get("effect")
    if not isinstance(effect, str) or effect not in OverrideEffect.values:
        errors["effect"] = ["Choose grant or revoke."]
        effect = ""
    try:
        expires = _expiry(data.get("expires_at"), now)
    except FieldErrors as e:
        errors.update(e.errors)
        expires = None
    try:
        reason = clean_reason(data)
    except FieldErrors as e:
        errors.update(e.errors)
        reason = ""
    if errors:
        raise FieldErrors(errors)
    org = _locked(org)
    override, _ = CapabilityOverride.objects.update_or_create(
        organization=org,
        capability=name,
        defaults={"effect": effect, "reason": reason, "expires_at": expires, "created_by": staff},
    )
    forget_organization(org.pk)
    changes = {"capability": name, "expires_at": timestamp(expires)}
    details = {**changes, "reason": reason}
    with acting_via(via="platform", reason=reason):
        if effect == OverrideEffect.GRANT:
            audit(org, staff, "organization.capability_granted", org, changes)
        else:
            audit(org, staff, "organization.capability_revoked", org, changes)
    if effect == OverrideEffect.GRANT:
        platform_audit(staff, "capability.granted", org.pk, details)
    else:
        platform_audit(staff, "capability.revoked", org.pk, details)
    return override


@transaction.atomic
def remove_override(staff, org, override: CapabilityOverride, data: dict[str, Any]) -> None:
    """Remove an override: the capability follows the edition again from the next request."""
    reason = clean_reason(data)
    org = _locked(org)
    changes = {"capability": override.capability, "effect": override.effect}
    override.delete()
    forget_organization(org.pk)
    with acting_via(via="platform", reason=reason):
        audit(org, staff, "organization.capability_override_removed", org, changes)
    platform_audit(staff, "capability.override_removed", org.pk, {**changes, "reason": reason})


# ---- limits (core/limits.py) ----------------------------------------------------------------------------------------

LIMIT_VALUE_MAX = 10**15  # far above any real use; keeps a typo from becoming a meaningless number


def _limit_value(data: dict[str, Any], errors: dict[str, list[str]]) -> int | None:
    if "value" not in data:
        errors["value"] = ["Give the limit as a whole number, or null for unlimited."]
        return None
    value = data["value"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= LIMIT_VALUE_MAX:
        errors["value"] = [f"Give a whole number from 0 to {LIMIT_VALUE_MAX}, or null for unlimited."]
        return None
    return value


@transaction.atomic
def set_limit(staff, org, data: dict[str, Any]) -> OrganizationLimitOverride:
    """Set one limit for this organization, replacing any override of the same limit; `value` None is unlimited.
    Applies from the next request. A value below the current usage removes nothing: new creates are refused until the
    usage is under it again."""
    now = timezone.now()
    errors: dict[str, list[str]] = {}
    try:
        name = limits.check_limit_name(data.get("limit"))
    except FieldErrors as e:
        errors.update(e.errors)
        name = ""
    value = _limit_value(data, errors)
    if name in limits.RATES and value == 0:
        # A rate of 0 would refuse the organization's every API request; suspending it is how that is done.
        errors["value"] = [
            "An API rate is at least 1 request a minute, or null for unlimited. Suspend the organization to stop it."
        ]
    try:
        expires = _expiry(data.get("expires_at"), now)
    except FieldErrors as e:
        errors.update(e.errors)
        expires = None
    try:
        reason = clean_reason(data)
    except FieldErrors as e:
        errors.update(e.errors)
        reason = ""
    if errors:
        raise FieldErrors(errors)
    caller = org
    org = _locked(org)
    before = limits.limits(org)[name]
    override, _ = OrganizationLimitOverride.objects.update_or_create(
        organization=org,
        limit=name,
        defaults={"value": value, "reason": reason, "expires_at": expires, "created_by": staff},
    )
    if not org.has_limit_overrides:
        org.has_limit_overrides = caller.has_limit_overrides = True
        org.save(update_fields=["has_limit_overrides"])
    forget_organization(org.pk)
    changes = {"limit": name, "value": [before, value], "expires_at": timestamp(expires)}
    with acting_via(via="platform", reason=reason):
        audit(org, staff, "organization.limit_set", org, changes)
    platform_audit(staff, "limit.set", org.pk, {**changes, "reason": reason})
    return override


@transaction.atomic
def remove_limit(staff, org, name: str, data: dict[str, Any]) -> None:
    """Remove the organization's override of one limit: the edition's default applies again from the next request.
    404 when there is none."""
    reason = clean_reason(data)
    caller = org
    org = _locked(org)
    override = OrganizationLimitOverride.objects.filter(organization=org, limit=name).first()
    if override is None:
        raise NotFound("This organization has no override of that limit.")
    changes = {"limit": name, "value": [override.value, limits.edition_limits(org).get(name)]}
    override.delete()
    if not OrganizationLimitOverride.objects.filter(organization=org).exists():
        org.has_limit_overrides = caller.has_limit_overrides = False
        org.save(update_fields=["has_limit_overrides"])
    forget_organization(org.pk)
    with acting_via(via="platform", reason=reason):
        audit(org, staff, "organization.limit_override_removed", org, changes)
    platform_audit(staff, "limit.override_removed", org.pk, {**changes, "reason": reason})


# ---- data export on request (catalog/org_lifecycle.py) --------------------------------------------------------------


@transaction.atomic
def request_export(staff, org, data: dict[str, Any]) -> OrgExport:
    """Make a data export for an organization that asked for one through support. It is the organization's like any
    other export: listed to its Admins with purpose `operator` and downloadable by them only (the platform API has no
    download). Audited in both streams; 409 while another export is being prepared."""
    reason = clean_reason(data)
    with acting_via(via="platform", reason=reason):
        export = org_lifecycle.request_export(org, staff, purpose=ExportPurpose.OPERATOR)
    platform_audit(staff, "export.requested", org.pk, {"export": str(export.pk), "reason": reason})
    return export


# ---- sign-in domains -----------------------------------------------------------------


def sign_in_domain_claims(domain: Any) -> list[SsoDomain]:
    """Every organization's claim of a sign-in domain, verified first, then the oldest pending."""
    clean = sso._clean_domain(domain if isinstance(domain, str) else "")
    rows = SsoDomain.objects.select_related("organization").filter(domain=clean)
    return sorted(rows, key=lambda row: (row.verified_at is None, row.created_at))


@transaction.atomic
def move_sign_in_domain(staff, data: dict[str, Any]) -> SsoDomain:
    """Give a sign-in domain, verified, to an organization, taking it from the one that holds it verified (a squatted
    or disputed domain). Audited in both organizations (via platform, with the reason) and in the platform stream."""
    reason = clean_reason(data)
    org = Organization.objects.filter(pk=_org_id(data.get("organization_id"))).first()
    if org is None:
        raise FieldErrors({"organization_id": ["Choose an organization."]})
    raw = data.get("domain")
    domain = raw if isinstance(raw, str) else ""
    with acting_via(via="platform", reason=reason):
        previous = sso.move_domain(domain, org, actor=staff, via="platform")
    clean = sso._clean_domain(domain)
    details = {"domain": clean, "from": str(previous.pk) if previous else None, "reason": reason}
    platform_audit(staff, "sign_in_domain.moved", org.pk, details)
    return SsoDomain.objects.select_related("organization").get(organization=org, domain=clean)


@transaction.atomic
def release_sign_in_domain(staff, data: dict[str, Any]) -> Organization | None:
    """Free a sign-in domain: the verified claim, else the oldest pending one. Audited in the organization that had it
    and in the platform stream. Returns that organization, or None when nobody claimed it."""
    reason = clean_reason(data)
    raw = data.get("domain")
    domain = raw if isinstance(raw, str) else ""
    with acting_via(via="platform", reason=reason):
        held = sso.release_domain(domain, actor=staff, via="platform")
    if held is not None:
        details = {"domain": sso._clean_domain(domain), "reason": reason}
        platform_audit(staff, "sign_in_domain.released", held.pk, details)
    return held


def _org_id(value: Any):
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


# ---- impersonation --------------------------------------------------------------------------------------------------


def impersonation_minutes() -> int:
    return int(settings.SOFTWARDEN_IMPERSONATION_MINUTES)


@transaction.atomic
def audit_failed_reauthentication(staff) -> None:
    """A wrong staff password when starting an impersonation is audited like every other wrong current password
    (catalog/admin_services.check_current_password): in the platform stream, and as user.password_check_failed in each
    organization the account belongs to. The view calls this after start_impersonation's transaction rolled back, so it
    stays written; an SSO-only account's stale sign-in checked no password and isn't recorded."""
    if not staff.has_usable_password():
        return
    details = {"user": staff.pk, "purpose": "impersonation"}
    platform_audit(staff, "staff.password_check_failed", details=details)
    audit_account(staff, staff, "user.password_check_failed", details)


@transaction.atomic
def start_impersonation(request, staff, data: dict[str, Any]) -> dict[str, Any]:
    """Start impersonating a member: re-authentication (the staff password, or a single sign-on sign-in in this session
    within 15 minutes), a reason, the member's id and organization. Audited in both streams; then the session switches
    (core/impersonation.begin). Returns the session's impersonation record."""
    if impersonation.state_of(request) is not None:
        raise ImpersonationRestricted("You are already impersonating someone; stop first.")
    errors: dict[str, list[str]] = {}
    try:
        reason = clean_reason(data)
    except FieldErrors as e:
        errors.update(e.errors)
        reason = ""
    org_id, user_id = data.get("organization_id"), data.get("user_id")
    org = None
    try:
        org = Organization.objects.filter(pk=uuid.UUID(str(org_id))).first() if org_id else None
    except ValueError:
        org = None
    if org is None:
        errors["organization_id"] = ["Choose an existing organization."]
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        errors["user_id"] = ["Choose a member by their user ID."]
    if errors or org is None or not isinstance(user_id, int):
        raise FieldErrors(errors)
    # Counted around the whole request by the view (platform_views.ImpersonationView), with the second factor.
    org_lifecycle.reauthenticate(staff, data.get("password"), authenticated_at(request), counted_by_caller=True)
    membership = Membership.objects.select_related("user").filter(organization=org, user_id=user_id).first()
    if membership is None:
        raise FieldErrors({"user_id": ["This account isn't a member of that organization."]})
    member = membership.user
    if not member.is_active:
        raise ImpersonationRestricted("This account is deactivated and can't be impersonated.")
    if is_platform_staff(member) or member.pk == staff.pk:
        raise ImpersonationRestricted("Platform staff can't be impersonated.")
    if org.suspended_at is not None:
        raise Conflict("This organization is suspended; unsuspend it first.")
    now = timezone.now()
    ends = now + timedelta(minutes=impersonation_minutes())
    state = {
        "id": str(uuid.uuid4()),
        "by": staff.pk,
        "user": member.pk,
        "organization": str(org.pk),
        "reason": reason,
        "started_at": timestamp(now),
        "expires_at": timestamp(ends),
    }
    changes = {"user": member.pk, "reason": reason, "expires_at": timestamp(ends), "impersonation": state["id"]}
    with impersonating(None), acting_via(via="platform"):
        audit(org, staff, "impersonation.started", None, changes)
    # The member by user id only (`changes["user"]`), never their email: the platform stream is append-only and
    # outlives the organization's purge, which erases the account.
    platform_audit(staff, "impersonation.started", org.pk, changes)
    impersonation.begin(request, state, str(org.pk))
    return state


def end_impersonation(request, staff, why: str) -> dict[str, Any] | None:
    """End the session's impersonation: audited in both streams, then the staff session comes back exactly as it was.
    `why`: stopped (staff asked), signed_out, expired, staff_unavailable (no longer staff, or another account),
    member_unavailable (deactivated, removed from the organization or made staff). None if there was none."""
    state = impersonation.state_of(request)
    if state is None:
        return None
    org = Organization.objects.filter(pk=str(state.get("organization"))).first()
    changes = {"user": state.get("user"), "reason": why, "impersonation": state.get("id")}
    actor = staff if staff is not None and getattr(staff, "pk", None) == state.get("by") else None
    with transaction.atomic(), impersonating(None), acting_via(via="platform"):
        if why == "expired":
            if org is not None:
                audit(org, actor, "impersonation.expired", None, changes)
            platform_audit(actor, "impersonation.expired", state.get("organization"), changes)
        else:
            if org is not None:
                audit(org, actor, "impersonation.stopped", None, changes)
            platform_audit(actor, "impersonation.stopped", state.get("organization"), changes)
        # In the same transaction: when another request (a second Stop, a sign-out) rotated the session first, finish
        # raises SessionInterrupted (core/sessions.py) and the end isn't audited twice.
        impersonation.finish(request)
    return state
