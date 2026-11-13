"""Response shapes of the data-flow map (catalog/data_flows.py builds them). Schema only: the views return
the built dictionaries."""

from rest_framework import serializers

from core.drf import BriefSerializer

from .data_flows import RISKS
from .models import (
    DataCategory,
    DataClassification,
    DpaStatus,
    IntegrationAuthMethod,
    IntegrationDirection,
    Lifecycle,
    VendorRiskTier,
)

END_KINDS = ["application", "component"]


class DataFlowVendorRiskSerializer(serializers.Serializer):
    has_profile = serializers.BooleanField()
    risk_tier = serializers.ChoiceField(VendorRiskTier.choices, allow_null=True)
    processes_personal_data = serializers.BooleanField()
    dpa_status = serializers.ChoiceField(
        DpaStatus.choices, allow_null=True, help_text="As recorded on its risk profile; null when not recorded."
    )
    dpa = serializers.BooleanField(help_text="A signed DPA on its risk profile, or an unexpired DPA document.")
    assurance = serializers.BooleanField(help_text="A current SOC 2 report or equivalent.")


class DataFlowEndSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(END_KINDS)
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField(help_text="Archiving a record keeps its integrations.")
    path = serializers.CharField(help_text="Its route under /api/v1/.")
    classification = serializers.ChoiceField(
        DataClassification.choices, allow_null=True, help_text="The record's own data classification."
    )
    instance = BriefSerializer(allow_null=True, help_text="The application instance this end is about, if any.")
    vendor = BriefSerializer(allow_null=True, help_text="An application's vendor; components have none.")
    vendor_risk = DataFlowVendorRiskSerializer(
        allow_null=True, help_text="The vendor's risk file; null without a vendor or the vendor_risk capability."
    )


class DataFlowRiskSerializer(serializers.Serializer):
    code = serializers.ChoiceField(list(RISKS))
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    end = serializers.ChoiceField(["source", "target"], help_text="The receiving end it is about.")
    why = serializers.CharField()


class DataFlowSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The integration (GET /integrations/{id}).")
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    path = serializers.CharField()
    source = DataFlowEndSerializer()  # type: ignore[assignment]  # the field, not Field.source
    target = DataFlowEndSerializer()
    direction = serializers.ChoiceField(IntegrationDirection.choices, allow_null=True)
    classification = serializers.ChoiceField(DataClassification.choices, allow_null=True)
    categories = serializers.ListField(child=serializers.ChoiceField(DataCategory.choices))
    contains_personal_data = serializers.BooleanField()
    auth_method = serializers.ChoiceField(IntegrationAuthMethod.choices, allow_null=True)
    data_purpose = serializers.CharField()
    technical_owner = BriefSerializer(allow_null=True)
    risks = DataFlowRiskSerializer(many=True)


class DataFlowClassificationCountsSerializer(serializers.Serializer):
    restricted = serializers.IntegerField()
    confidential = serializers.IntegerField()
    internal = serializers.IntegerField()
    public = serializers.IntegerField()
    none = serializers.IntegerField(help_text="Flows with no classification.")


class DataFlowRiskCountsSerializer(serializers.Serializer):
    no_dpa = serializers.IntegerField()
    no_auth_method = serializers.IntegerField()
    down_classification = serializers.IntegerField()


class DataFlowSummarySerializer(serializers.Serializer):
    flows = serializers.IntegerField()
    risky = serializers.IntegerField(help_text="Flows with at least one risk.")
    personal_data = serializers.IntegerField(
        help_text="Flows carrying personal, special-category personal or health data."
    )
    by_classification = DataFlowClassificationCountsSerializer()
    by_risk = DataFlowRiskCountsSerializer(help_text="Flows with each risk (a flow counts once per risk).")


class DataFlowReportSerializer(serializers.Serializer):
    as_of = serializers.DateField(help_text="Today in the organization's timezone (vendor documents' expiry).")
    vendor_risk = serializers.BooleanField(
        help_text="Whether vendors' DPAs are known (the vendor_risk capability); without it no_dpa isn't raised."
    )
    summary = DataFlowSummarySerializer(help_text="Over the flows returned.")
    flows = DataFlowSerializer(many=True, help_text="The most sensitive first, then by name.")


class DataFlowNodeSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="The record's UUID.")
    kind = serializers.ChoiceField(END_KINDS)
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    retired = serializers.BooleanField()
    path = serializers.CharField(help_text="Its route under /api/v1/ (the SPA's route is the same).")
    map_path = serializers.CharField(help_text="Its own map.")
    expandable = serializers.BooleanField()
    classification = serializers.ChoiceField(DataClassification.choices, allow_null=True)
    weight = serializers.IntegerField(help_text="Its classification's rank: 0 unclassified to 4 restricted.")
    vendor = BriefSerializer(allow_null=True)
    risky = serializers.BooleanField(help_text="It receives data through a flow with a risk.")
    detail = serializers.DictField(help_text="vendor_risk: the vendor's risk facts, or null.")


class DataFlowEdgeSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="integration:<uuid>.")
    source = serializers.CharField()  # type: ignore[assignment]  # the field, not Field.source
    target = serializers.CharField()
    relation = serializers.CharField(help_text="Always integration.")
    category = serializers.CharField(help_text="Always integration.")
    label = serializers.CharField(  # type: ignore[assignment]  # the field, not Field.label
        help_text="Drawn on the edge: its data categories, or the integration's name when it has none."
    )
    reads = serializers.CharField(help_text="Read from the source: 'Sends data to', 'Exchanges data with'.")
    reads_inverse = serializers.CharField(help_text="Read from the target.")
    archived = serializers.BooleanField(help_text="Always false: archived integrations aren't flows.")
    retired = serializers.BooleanField(help_text="Always false: retired integrations aren't flows.")
    classification = serializers.ChoiceField(DataClassification.choices, allow_null=True)
    weight = serializers.IntegerField(help_text="Its classification's rank, for color and width: 0 to 4.")
    categories = serializers.ListField(child=serializers.ChoiceField(DataCategory.choices))
    contains_personal_data = serializers.BooleanField()
    risks = serializers.ListField(child=serializers.ChoiceField(list(RISKS)), help_text="Its risk codes, once each.")
    detail = serializers.DictField(
        help_text="integration {id, name, lifecycle, path}, direction, auth_method, source_instance, target_instance."
    )


class DataFlowLegendSerializer(serializers.Serializer):
    classification = serializers.ChoiceField(DataClassification.choices, allow_null=True)
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    weight = serializers.IntegerField()


class DataFlowGraphSerializer(serializers.Serializer):
    as_of = serializers.DateField()
    vendor_risk = serializers.BooleanField()
    nodes = DataFlowNodeSerializer(many=True, help_text="The records at the ends, by name.")
    edges = DataFlowEdgeSerializer(many=True, help_text="One per flow, the most sensitive first.")
    truncated = serializers.BooleanField(help_text="More flows matched than the graph shows.")
    legend = DataFlowLegendSerializer(many=True, help_text="Each classification's label and weight, highest first.")
