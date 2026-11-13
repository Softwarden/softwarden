"""Shapes of the vendor risk file (catalog/vendor_risk.py): the profile, its documents, the vendor detail's
summary and the risk overview. Responses are built in catalog/vendor_risk_viewsets.py from the module's derivations;
these serializers describe them in OpenAPI (and the input serializers document the request bodies; the services
validate)."""

from typing import Any

from rest_framework import serializers

from core.drf import ReadOnlySerializer, iso, timestamp

from . import vendor_risk as risk
from .models import (
    AssuranceType,
    AttachmentCategory,
    AttachmentKind,
    DpaStatus,
    QuestionnaireStatus,
    RiskDocumentKind,
    VendorRiskTier,
)

TIERS = VendorRiskTier.choices  # the criticality values and labels: CriticalityEnum in OpenAPI


class VendorRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class RiskAttachmentSerializer(serializers.Serializer):
    """An attachment of the vendor's (GET /attachments/{id}; download at /attachments/{id}/download)."""

    id = serializers.UUIDField()
    title = serializers.CharField()
    kind = serializers.ChoiceField(AttachmentKind.choices)
    category = serializers.ChoiceField(AttachmentCategory.choices)


def attachment_json(attachment) -> dict[str, Any] | None:
    if attachment is None:
        return None
    return {
        "id": str(attachment.id),
        "title": attachment.title,
        "kind": attachment.kind,
        "category": attachment.category,
    }


class RiskGapSerializer(serializers.Serializer):
    code = serializers.ChoiceField(list(risk.GAPS))
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label


class RiskCountsSerializer(serializers.Serializer):
    current = serializers.IntegerField()
    expiring = serializers.IntegerField()
    expired = serializers.IntegerField()


class AssuranceSummarySerializer(serializers.Serializer):
    type = serializers.ChoiceField(AssuranceType.choices, allow_null=True)
    expires_on = serializers.DateField(allow_null=True)
    status = serializers.ChoiceField(list(risk.ASSURANCE_STATUSES))


class VendorRiskSummarySerializer(serializers.Serializer):
    """A vendor's risk at a glance (catalog/vendor_risk.summary)."""

    has_profile = serializers.BooleanField(help_text="False until the vendor's risk file is first written.")
    risk_tier = serializers.ChoiceField(TIERS, allow_null=True, help_text="Null: not assessed.")
    processes_personal_data = serializers.BooleanField()
    questionnaire_status = serializers.ChoiceField(QuestionnaireStatus.choices)
    dpa_status = serializers.ChoiceField(DpaStatus.choices, allow_null=True, help_text="Null: not recorded.")
    assurance = AssuranceSummarySerializer()
    documents = RiskCountsSerializer(help_text="Documents per status, superseded ones left out.")
    next_expiry = serializers.DateField(
        allow_null=True, help_text="The earliest expiry date of the unsuperseded documents and assurance report."
    )
    gaps = RiskGapSerializer(many=True)


# ---- the profile -------------------------------------------------------------------------------------------------


class VendorRiskProfileSerializer(ReadOnlySerializer):
    """GET and PATCH /vendors/{id}/risk, rendered from the context's `files` (catalog/vendor_risk.files). A vendor
    whose file was never written reads as the defaults with `id` null."""

    id = serializers.UUIDField(allow_null=True, help_text="Null until the first write creates the profile.")
    vendor = VendorRefSerializer()
    risk_tier = serializers.ChoiceField(TIERS, allow_null=True, help_text="Null: not assessed.")
    processes_personal_data = serializers.BooleanField(
        help_text="Whether the vendor processes personal data for the organization; a DPA is then expected."
    )
    questionnaire_status = serializers.ChoiceField(QuestionnaireStatus.choices)
    questionnaire_requested_on = serializers.DateField(allow_null=True)
    questionnaire_reviewed_on = serializers.DateField(allow_null=True)
    assurance_type = serializers.ChoiceField(AssuranceType.choices, allow_null=True)
    assurance_issued_on = serializers.DateField(allow_null=True, help_text="The report period's end or issue date.")
    assurance_validity_months = serializers.IntegerField()
    assurance_expires_on = serializers.DateField(
        allow_null=True, help_text="Worked out: the issue date plus the validity. Never sent."
    )
    assurance_status = serializers.ChoiceField(list(risk.ASSURANCE_STATUSES))
    assurance_superseded = serializers.BooleanField(
        help_text="A document of the report's kind replaces it (a SOC 2 document a SOC 2 report, an ISO 27001 "
        "document an ISO 27001 certificate; an `other` report is never replaced): one that expires later, or on the "
        "same day and was added later. The report then raises no reminder and isn't counted."
    )
    assurance_attachment = RiskAttachmentSerializer(allow_null=True)
    dpa_status = serializers.ChoiceField(DpaStatus.choices, allow_null=True, help_text="Null: not recorded.")
    dpa_signed_on = serializers.DateField(allow_null=True)
    dpa_attachment = RiskAttachmentSerializer(allow_null=True)
    subprocessor_url = serializers.CharField(help_text="The vendor's published subprocessor list, or empty.")
    subprocessor_attachment = RiskAttachmentSerializer(allow_null=True)
    subprocessors_reviewed_on = serializers.DateField(allow_null=True)
    notes = serializers.CharField()
    gaps = RiskGapSerializer(many=True)
    documents = RiskCountsSerializer(help_text="The vendor's documents per status, superseded ones left out.")
    created_at = serializers.DateTimeField(allow_null=True)
    updated_at = serializers.DateTimeField(allow_null=True)

    def to_representation(self, instance):
        f: risk.VendorFile = self.context["files"][instance.id]  # the instance is the vendor
        today, days = self.context["today"], self.context["days"]
        p = f.profile
        shown = p or risk.VendorRiskProfile(vendor=f.vendor)
        superseded = risk.superseded_in(f)
        return {
            "id": str(p.id) if p else None,
            "vendor": {"id": str(f.vendor.id), "name": f.vendor.name},
            "risk_tier": shown.risk_tier or None,
            "processes_personal_data": shown.processes_personal_data,
            "questionnaire_status": shown.questionnaire_status,
            "questionnaire_requested_on": iso(shown.questionnaire_requested_on),
            "questionnaire_reviewed_on": iso(shown.questionnaire_reviewed_on),
            "assurance_type": shown.assurance_type or None,
            "assurance_issued_on": iso(shown.assurance_issued_on),
            "assurance_validity_months": shown.assurance_validity_months,
            "assurance_expires_on": iso(shown.assurance_expires_on),
            "assurance_status": risk.assurance_status(p, today, days),
            "assurance_superseded": p is not None and p.id in superseded,
            "assurance_attachment": attachment_json(p.assurance_attachment if p else None),
            "dpa_status": shown.dpa_status or None,
            "dpa_signed_on": iso(shown.dpa_signed_on),
            "dpa_attachment": attachment_json(p.dpa_attachment if p else None),
            "subprocessor_url": shown.subprocessor_url,
            "subprocessor_attachment": attachment_json(p.subprocessor_attachment if p else None),
            "subprocessors_reviewed_on": iso(shown.subprocessors_reviewed_on),
            "notes": shown.notes,
            "gaps": risk.gap_json(risk.gaps_of(p, f.documents, today)),
            "documents": risk.counts(f.documents, superseded, today, days),
            "created_at": timestamp(p.created_at) if p else None,
            "updated_at": timestamp(p.updated_at) if p else None,
        }


class VendorRiskProfileInputSerializer(serializers.Serializer):
    """Any subset; null or empty clears an optional field. `assurance_expires_on` is worked out and refused."""

    risk_tier = serializers.ChoiceField(TIERS, required=False, allow_null=True, allow_blank=True)
    processes_personal_data = serializers.BooleanField(required=False)
    questionnaire_status = serializers.ChoiceField(
        QuestionnaireStatus.choices,
        required=False,
        help_text="Moving to requested or reviewed with no date for it sets that date to today.",
    )
    questionnaire_requested_on = serializers.DateField(required=False, allow_null=True)
    questionnaire_reviewed_on = serializers.DateField(required=False, allow_null=True)
    assurance_type = serializers.ChoiceField(AssuranceType.choices, required=False, allow_null=True, allow_blank=True)
    assurance_issued_on = serializers.DateField(required=False, allow_null=True)
    assurance_validity_months = serializers.IntegerField(
        required=False, min_value=1, max_value=risk.VALIDITY_MAX, help_text="Default 12."
    )
    assurance_attachment = serializers.UUIDField(
        required=False, allow_null=True, help_text="One of the vendor's attachments."
    )
    dpa_status = serializers.ChoiceField(DpaStatus.choices, required=False, allow_null=True, allow_blank=True)
    dpa_signed_on = serializers.DateField(required=False, allow_null=True)
    dpa_attachment = serializers.UUIDField(
        required=False, allow_null=True, help_text="One of the vendor's attachments."
    )
    subprocessor_url = serializers.URLField(required=False, allow_blank=True, max_length=risk.URL_MAX)
    subprocessor_attachment = serializers.UUIDField(
        required=False, allow_null=True, help_text="One of the vendor's attachments."
    )
    subprocessors_reviewed_on = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=risk.TEXT_MAX)


# ---- documents -----------------------------------------------------------------------------------------------------


class VendorRiskDocumentSerializer(ReadOnlySerializer):
    """A dated document in a vendor's risk file. `status` is derived when read: expired after `expires_on`, expiring
    from the organization's largest reminder threshold before it, current otherwise (and when undated)."""

    id = serializers.UUIDField()
    vendor = VendorRefSerializer()
    kind = serializers.ChoiceField(RiskDocumentKind.choices)
    title = serializers.CharField()
    issued_on = serializers.DateField(allow_null=True)
    validity_months = serializers.IntegerField(
        allow_null=True, help_text="Set: `expires_on` is worked out from `issued_on`."
    )
    expires_on = serializers.DateField(allow_null=True)
    status = serializers.ChoiceField(list(risk.DOCUMENT_STATUSES))
    days_left = serializers.IntegerField(
        allow_null=True, help_text="Days from today to the expiry date (negative once expired); null when undated."
    )
    superseded = serializers.BooleanField(
        help_text="A later item of the same kind replaces it (a SOC 2 or ISO 27001 document also by the profile's "
        "report of that kind; `other` documents never): it raises no reminder and isn't counted."
    )
    attachment = RiskAttachmentSerializer(allow_null=True)
    notes = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def to_representation(self, instance):
        d = instance
        today, days = self.context["today"], self.context["days"]
        return {
            "id": str(d.id),
            "vendor": {"id": str(d.vendor_id), "name": d.vendor.name},
            "kind": d.kind,
            "title": d.title,
            "issued_on": iso(d.issued_on),
            "validity_months": d.validity_months,
            "expires_on": iso(d.expires_on),
            "status": risk.status_of(d.expires_on, today, days),
            "days_left": (d.expires_on - today).days if d.expires_on else None,
            "superseded": d.id in self.context["superseded"],
            "attachment": attachment_json(d.attachment),
            "notes": d.notes,
            "created_at": timestamp(d.created_at),
            "updated_at": timestamp(d.updated_at),
        }


class VendorRiskDocumentInputSerializer(serializers.Serializer):
    """`kind` is required when adding. Send `expires_on`, or `issued_on` with `validity_months` (then the expiry is
    worked out); not both."""

    kind = serializers.ChoiceField(RiskDocumentKind.choices, required=False)
    title = serializers.CharField(required=False, allow_blank=True, max_length=risk.TITLE_MAX)
    issued_on = serializers.DateField(required=False, allow_null=True)
    validity_months = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=risk.VALIDITY_MAX
    )
    expires_on = serializers.DateField(required=False, allow_null=True)
    attachment = serializers.UUIDField(required=False, allow_null=True, help_text="One of the vendor's attachments.")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=risk.TEXT_MAX)


# ---- the overview ------------------------------------------------------------------------------------------------


class RiskDocumentBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(RiskDocumentKind.choices)
    title = serializers.CharField()
    expires_on = serializers.DateField(allow_null=True)
    status = serializers.ChoiceField(list(risk.DOCUMENT_STATUSES))
    superseded = serializers.BooleanField()


class VendorRiskOverviewRowSerializer(VendorRiskSummarySerializer):
    vendor = VendorRefSerializer()
    document_list = RiskDocumentBriefSerializer(many=True, help_text="Every document, the soonest expiry first.")
