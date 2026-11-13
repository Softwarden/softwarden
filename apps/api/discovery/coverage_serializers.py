"""OpenAPI shapes of SSO and MFA coverage (discovery/coverage.py): an application's `sso_coverage` block and
one row of GET /reports/sso-coverage. The data is built as plain dicts (coverage.blocks); these describe it."""

from typing import Any

from rest_framework import serializers

from catalog.models import Criticality, Kind, Lifecycle
from core.drf import ReadOnlySerializer

from .models import MfaCoverage, ProvisioningState, SsoMode

SSO_HELP = (
    "saml, oidc, password (Entra's password-based SSO: behind My Apps, not federated), none (Entra says single sign-on "
    "isn't set up) or unknown (Entra gives no mode, as for older SAML and OIDC apps; or not linked or not read: see "
    "sso_detail). Unknown is never counted as no SSO."
)
SCIM_HELP = "on (a provisioning job runs on schedule), off (none, paused, in quarantine or not scheduled) or unknown."
MFA_HELP = (
    "all_users (an enabled conditional access policy requires MFA of everyone signing in to it), some_users (only of "
    "some users or sign-ins), none (no enabled policy requires it; report-only and MFA-or-something-else don't count), "
    "unknown (no P1, not read, an application filter or Microsoft app group that can't be evaluated: see "
    "mfa_detail), or blocked (an enabled policy blocks sign-in for all users: no gap)."
)


class CoveragePolicySerializer(serializers.Serializer):
    """A conditional access policy that covers, or may cover, the app."""

    id = serializers.CharField()
    name = serializers.CharField()
    state = serializers.ChoiceField(["enabled", "enabledForReportingButNotEnforced"])
    applies = serializers.ChoiceField(["yes", "maybe"], help_text="maybe: an application filter or app group.")
    users = serializers.ChoiceField(["all", "some"])
    mfa = serializers.BooleanField(help_text="It requires MFA (not as one option among others).")
    effect = serializers.ChoiceField(
        ["blocked", "mfa_all_users", "mfa_some_users", "may_require_mfa", "mfa_optional", "no_mfa", "report_only"]
    )
    exclusions = serializers.IntegerField(help_text="Users, groups and roles it excludes (break-glass accounts...).")
    why = serializers.CharField(allow_blank=True)


class CoverageDifferenceSerializer(serializers.Serializer):
    """A field of the record Entra contradicts ("source differs"), or that isn't recorded. Never applied by a run."""

    field = serializers.ChoiceField(["sso_enabled", "scim_provisioning"])
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    curated = serializers.BooleanField(allow_null=True, help_text="The record's value; null: not recorded.")
    source = serializers.BooleanField(help_text="What Entra says.")  # type: ignore[assignment]  # the API field


class CoverageConnectorSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class SsoCoverageSerializer(serializers.Serializer):
    """An application's SSO and MFA coverage (discovery/coverage.py)."""

    entra_app_id = serializers.CharField(allow_null=True, help_text="The Entra application ID it is matched by.")
    linked_by = serializers.ChoiceField(
        ["entra_app_id", "candidate", ""],
        help_text="entra_app_id: the record's own field; candidate: a linked Entra or shadow IT candidate; empty: not "
        "linked.",
    )
    connector = CoverageConnectorSerializer(allow_null=True, help_text="The coverage connector whose run it is.")
    seen_at = serializers.DateTimeField(allow_null=True, help_text="That run.")
    sso = serializers.ChoiceField(SsoMode.choices, help_text=SSO_HELP)
    sso_mode = serializers.CharField(allow_blank=True, help_text="preferredSingleSignOnMode as Entra gave it.")
    sso_detail = serializers.CharField(allow_blank=True)
    scim = serializers.ChoiceField(ProvisioningState.choices, help_text=SCIM_HELP)
    scim_detail = serializers.CharField(allow_blank=True)
    scim_checked_at = serializers.DateTimeField(allow_null=True, help_text="When its provisioning jobs were read.")
    mfa = serializers.ChoiceField(MfaCoverage.choices, help_text=MFA_HELP)
    mfa_detail = serializers.CharField(allow_blank=True)
    mfa_checked_at = serializers.DateTimeField(
        allow_null=True, help_text="When the conditional access policies behind `mfa` were read."
    )
    policies = CoveragePolicySerializer(many=True)
    gaps = serializers.ListField(
        child=serializers.CharField(),
        help_text="No SSO, No SCIM, No MFA policy, MFA for some users only, Coverage unknown.",
    )
    differs = CoverageDifferenceSerializer(many=True)


class CoverageRecordSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.ChoiceField([Kind.APPLICATION])


class SsoCoverageRowSerializer(SsoCoverageSerializer, ReadOnlySerializer):
    """One application of the coverage report: the record, its criticality and lifecycle, and its coverage. Context:
    `coverage` {record ID: block}, computed once per page."""

    record = CoverageRecordSerializer()
    criticality = serializers.ChoiceField([*Criticality.values, ""])
    lifecycle = serializers.ChoiceField(Lifecycle.choices)

    def to_representation(self, instance) -> dict[str, Any]:
        return {
            "record": {"id": str(instance.id), "name": instance.name, "kind": instance.kind},
            "criticality": instance.criticality or "",
            "lifecycle": instance.lifecycle,
            **self.context["coverage"][instance.id],
        }
