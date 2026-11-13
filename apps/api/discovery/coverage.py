"""Reading SSO and MFA coverage (written by the `entra_coverage` source, discovery/entra_coverage.py): which
Entra app a catalog application is, its coverage row, the gap chips and where the source differs from the record's own
`sso_enabled` and `scim_provisioning`. Used by the application detail (`sso_coverage`), the coverage report
(GET /reports/sso-coverage) and the data-quality rules (catalog/quality.py, `coverage.*` fields).

Which Entra app: the application's own `entra_app_id`; without one, the app ID of a linked or created candidate of an
`entra` or `entra_shadow_it` connector (the most recently seen). Which row: that app's coverage row of any
`entra_coverage` connector of the organization that is turned on, the most recently seen when several tenants
list it. Everything is
computed when read, so a record edit or a new link shows at once (coverage itself comes with the connector's next run).

Coverage is derived data. The record's own fields are never written by a run; where they differ from what Entra says,
`differs` names the field with both values ("source differs"), and an Editor changes the record as any edit."""

from __future__ import annotations

from typing import Any

from django.db.models import F, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce, Lower, NullIf

from catalog.models import Kind, Lifecycle, Resource
from catalog.state import COVERAGE_LABELS
from core.drf import iso

from .models import (
    AppCoverage,
    Connector,
    DecisionKind,
    MfaCoverage,
    Observation,
    ObservationKind,
    ProvisioningState,
    Source,
    SsoMode,
)

APP_SOURCES = (Source.ENTRA, Source.ENTRA_SHADOW_IT)
UNKNOWN = "unknown"
DIMENSIONS = ("sso", "scim", "mfa")
CURATED_LABELS = {"sso_enabled": "SSO enabled", "scim_provisioning": "SCIM provisioning"}

NOT_LINKED = "Not linked to an Entra application: set its Entra application ID, or link its Entra candidate."
NO_CONNECTOR = (
    "No SSO and MFA coverage connector is on: add one (Microsoft Entra SSO and MFA coverage) in Discovery, or turn "
    "yours on."
)
NOT_SEEN = (
    "Not seen yet: the app isn't among the tenant's enterprise applications, or no coverage run has finished since it "
    "was linked."
)


def _linked_app():
    """The app ID of the record's linked or created Entra (or shadow IT) candidate, the most recently seen."""
    return (
        Observation.objects.filter(
            decision__resource_id=OuterRef("pk"), kind=ObservationKind.APPLICATION, connector__source__in=APP_SOURCES
        )
        .exclude(decision__decision=DecisionKind.DISMISSED)
        .order_by("-last_seen_at", "id")
        .values("external_id")[:1]
    )


def annotate(qs: QuerySet[Resource]) -> QuerySet[Resource]:
    """Resource rows with `curated_app` (its own Entra app ID, lowercased), `observed_app` (its candidate's),
    `entra_app` (the one used), `coverage_id`, and `sso`, `scim`, `mfa` (unknown without a row)."""
    qs = qs.annotate(
        curated_app=NullIf(Lower("application__entra_app_id"), Value("")),
        observed_app=Subquery(_linked_app()),
    ).annotate(entra_app=Coalesce(F("curated_app"), F("observed_app")))
    row = AppCoverage.objects.filter(
        organization_id=OuterRef("organization_id"), app_id=OuterRef("entra_app"), connector__enabled=True
    ).order_by("-seen_at", "id")
    return qs.annotate(
        coverage_id=Subquery(row.values("id")[:1]),
        **{d: Coalesce(Subquery(row.values(d)[:1]), Value(UNKNOWN)) for d in DIMENSIONS},
    )


def live_applications(org) -> QuerySet[Resource]:
    """The organization's applications that are neither archived nor retired, with their coverage annotations."""
    qs = Resource.objects.filter(organization=org, kind=Kind.APPLICATION, archived_at__isnull=True).exclude(
        lifecycle=Lifecycle.RETIRED
    )
    return annotate(qs.select_related("application"))


def gaps(sso: str, scim: str, mfa: str) -> list[str]:
    """The gap chips, in the ownership gaps' language (catalog/state.COVERAGE_LABELS)."""
    out = []
    if sso == SsoMode.NONE:
        out.append(COVERAGE_LABELS["no_sso"])
    if scim == ProvisioningState.OFF:
        out.append(COVERAGE_LABELS["no_scim"])
    if mfa == MfaCoverage.NONE:
        out.append(COVERAGE_LABELS["no_mfa"])
    elif mfa == MfaCoverage.SOME_USERS:
        out.append(COVERAGE_LABELS["mfa_partial"])
    if UNKNOWN in (sso, scim, mfa):
        out.append(COVERAGE_LABELS["unknown"])
    return out


def source_values(sso: str, scim: str) -> dict[str, bool]:
    """What Entra says of the record's own fields: SAML, OpenID Connect and password-based SSO are single sign-on (as
    Entra counts them), no SSO isn't; unknown says nothing."""
    out = {}
    if sso != UNKNOWN:
        out["sso_enabled"] = sso != SsoMode.NONE
    if scim != UNKNOWN:
        out["scim_provisioning"] = scim == ProvisioningState.ON
    return out


def differs(application, sso: str, scim: str) -> list[dict[str, Any]]:
    """{field, label, curated, source} for each of the record's fields Entra contradicts or that isn't recorded."""
    out = []
    for name, value in source_values(sso, scim).items():
        curated = getattr(application, name, None) if application is not None else None
        if curated is not value:
            out.append({"field": name, "label": CURATED_LABELS[name], "curated": curated, "source": value})
    return out


def blocks(org, resources: list[Resource]) -> dict[Any, dict[str, Any]]:
    """{resource ID: coverage block} for annotated resources (annotate): one query for the rows, and one more only
    when a linked record has none (whether the organization has a coverage connector, to say why)."""
    ids = [getattr(r, "coverage_id", None) for r in resources]
    rows = {
        c.id: c
        for c in AppCoverage.objects.filter(organization=org, id__in=[i for i in ids if i]).select_related("connector")
    }
    any_connector = None
    out = {}
    for r in resources:
        row = rows.get(getattr(r, "coverage_id", None))  # type: ignore[arg-type]  # None: no row
        app = getattr(r, "entra_app", None)
        why = ""
        if row is None:
            if not app:
                why = NOT_LINKED
            else:
                if any_connector is None:
                    any_connector = Connector.objects.filter(
                        organization=org, source=Source.ENTRA_COVERAGE, enabled=True
                    ).exists()
                why = NOT_SEEN if any_connector else NO_CONNECTOR
        sso = row.sso if row else UNKNOWN
        scim = row.scim if row else UNKNOWN
        mfa = row.mfa if row else UNKNOWN
        application = getattr(r, "application", None)
        out[r.id] = {
            "entra_app_id": app or None,
            "linked_by": "entra_app_id" if getattr(r, "curated_app", None) else ("candidate" if app else ""),
            "connector": {"id": str(row.connector_id), "name": row.connector.name} if row else None,
            "seen_at": iso(row.seen_at) if row else None,
            "sso": sso,
            "sso_mode": row.sso_mode if row else "",
            "sso_detail": row.sso_detail if row else why,
            "scim": scim,
            "scim_detail": row.scim_detail if row else why,
            "scim_checked_at": iso(row.scim_checked_at) if row else None,
            "mfa": mfa,
            "mfa_detail": row.mfa_detail if row else why,
            "mfa_checked_at": iso(row.mfa_checked_at) if row else None,
            "policies": list(row.policies or []) if row else [],
            "gaps": gaps(sso, scim, mfa),
            "differs": differs(application, sso, scim),
        }
    return out


def record_block(org, resource) -> dict[str, Any] | None:
    """An application's coverage block (the detail's `sso_coverage`), or None for another kind or without the
    discovery.entra capability."""
    from core.capabilities import DISCOVERY_ENTRA, has_capability

    if resource.kind != Kind.APPLICATION or not has_capability(org, DISCOVERY_ENTRA):
        return None
    found = list(annotate(Resource.objects.filter(organization=org, pk=resource.pk).select_related("application")))
    return blocks(org, found)[resource.pk] if found else None
