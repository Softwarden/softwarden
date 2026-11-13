"""OpenAPI descriptions of the composite responses (api/composite.py builds them; tests/test_composite.py checks that
each description has exactly the keys its endpoint returns). Describe-only: nothing here validates or writes."""

from rest_framework import serializers

from catalog.models import RECORD_KIND_CHOICES, Kind, Lifecycle
from catalog.search import RECORD_SEARCH_KINDS
from catalog.serializers import LINK_STATES
from core.drf import ReadOnlySerializer
from discovery.models import CredentialType
from money import reporting
from money.models import AutoRenew
from money.report_serializers import CommitmentSerializer, WindowSerializer

from .composite import GAPS

# The choice sets the responses use (named in SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"], so generated clients get
# stable enum names), each taken from the code that produces the values. tests/test_composite.py checks SEARCH_KINDS
# against core/search's registry and the rest against what the builders return.
SEARCH_KINDS = ["application", "component", "integration", "ai_agent", "vendor", "person", "attachment", "contract"]
# An attachment's parent (catalog/search.py AttachmentSearcher): a record, a vendor or a contract.
SEARCH_PARENT_KINDS = [*RECORD_SEARCH_KINDS, "vendor", "contract"]
# A dependency node or flow end (catalog/graph.py): any record kind, integrations included.
LINK_RECORD_KINDS = [*RECORD_KIND_CHOICES, Kind.INTEGRATION.value]
DEADLINE_SOURCES = ["renewal", "contract"]
DEADLINE_ACTIONS = list(reporting.NEUTRAL_LABELS)  # money/reporting.deadline_rows
CREDENTIAL_KINDS = reporting.CREDENTIAL_KINDS  # money/reporting.expiring_credentials

# (Comments, not docstrings: docstrings become OpenAPI text.)

PATH_HELP = "Its route under /api/v1/ (the SPA uses the same path)."
AS_OF_HELP = "Today in the organization's timezone: the day every date and day count is from."
COUNT_HELP = "Every match, not only those returned."
TRUNCATED_HELP = "True when more matched than were returned: narrow the query or raise `limit`."


class CompositeRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class CompositeKindRefSerializer(CompositeRefSerializer):
    kind = serializers.ChoiceField(LINK_RECORD_KINDS)


class CompositePersonSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    email = serializers.CharField(allow_null=True, help_text="Null when none is recorded.")
    active = serializers.BooleanField(help_text="False once the person has left.")


class CompositeOwnerRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    active = serializers.BooleanField(help_text="False once the person has left.")


class CompositeUserSerializer(ReadOnlySerializer):
    id = serializers.IntegerField()
    name = serializers.CharField(help_text="Full name, else email.")
    email = serializers.CharField(allow_null=True)


class CompositeRecordSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    path = serializers.CharField(help_text=PATH_HELP)


class CompositeOwnedRecordSerializer(CompositeRecordSerializer):
    business_owner = CompositePersonSerializer(allow_null=True)
    technical_owner = CompositePersonSerializer(allow_null=True)
    it_steward = CompositeUserSerializer(allow_null=True)
    support_team = CompositeRefSerializer(allow_null=True)


# ---- search


class CompositeSnippetSerializer(ReadOnlySerializer):
    field = serializers.CharField(help_text="Where the query matched: description, purpose, vendor, tags and so on.")
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    text = serializers.CharField(help_text="Plain text around the match.")


class CompositeSearchParentSerializer(ReadOnlySerializer):
    kind = serializers.ChoiceField(SEARCH_PARENT_KINDS)
    id = serializers.UUIDField()
    name = serializers.CharField()
    path = serializers.CharField()


class CompositeSearchHitSerializer(ReadOnlySerializer):
    kind = serializers.ChoiceField(SEARCH_KINDS)
    id = serializers.UUIDField()
    name = serializers.CharField()
    path = serializers.CharField(help_text=PATH_HELP)
    lifecycle = serializers.ChoiceField(Lifecycle.choices, allow_null=True, help_text="Records only.")
    archived = serializers.BooleanField()
    snippet = CompositeSnippetSerializer(allow_null=True, help_text="Null when the name matched.")
    parent = CompositeSearchParentSerializer(allow_null=True, help_text="Attachments: what they are on.")


class CompositeSearchSerializer(ReadOnlySerializer):
    query = serializers.CharField()
    ignored = serializers.ListField(child=serializers.CharField(), help_text="Words with nothing searchable in them.")
    fuzzy = serializers.BooleanField(help_text="Nothing matched every word: these are similar names.")
    count = serializers.IntegerField(help_text=COUNT_HELP)
    by_kind = serializers.DictField(child=serializers.IntegerField(), help_text="Matches per kind searched.")
    truncated = serializers.BooleanField(help_text="A kind had more than `limit` matches.")
    results = CompositeSearchHitSerializer(many=True)


# ---- deadlines and renewals


class CompositeDeadlineSerializer(ReadOnlySerializer):
    source = serializers.ChoiceField(DEADLINE_SOURCES)  # type: ignore[assignment]  # the API field
    id = serializers.UUIDField(help_text="The renewal's or the contract's id.")
    name = serializers.CharField()
    path = serializers.CharField(help_text="renewals/<id> or contracts/<id>.")
    date = serializers.DateField(
        help_text="The action deadline (cancel or renew by): the date reminders use, and what within_days filters on."
    )
    days = serializers.IntegerField(help_text="From today; negative when overdue.")
    overdue = serializers.BooleanField()
    action = serializers.ChoiceField(
        DEADLINE_ACTIONS, help_text="cancel_by (it auto-renews unless canceled) or renew_by."
    )
    label = serializers.CharField(  # type: ignore[assignment]  # the API field, not Field.label
        allow_null=True, help_text="How the date was found: Calculated, Assumed, Entered, Renewal action."
    )
    explanation = serializers.CharField(allow_null=True)
    term_ends = serializers.DateField(allow_null=True, help_text="The renewal date or the contract's end date.")
    auto_renew = serializers.ChoiceField(AutoRenew.choices)
    decision = serializers.CharField(allow_null=True, help_text="A decision recorded but not completed.")
    vendor_name = serializers.CharField(allow_null=True)
    contract = CompositeRefSerializer(allow_null=True, help_text="The contract the dates come from, if any.")
    annualized_cost = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    currency = serializers.CharField(allow_null=True)


class CompositeDeadlineRowSerializer(CompositeDeadlineSerializer):
    follow_up = CompositeUserSerializer(allow_null=True, help_text="The renewal's IT follow-up; null on contracts.")


class CompositeDeadlinesSerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    within_days = serializers.IntegerField()
    overdue = serializers.IntegerField()
    windows = WindowSerializer(many=True, help_text="Due within 30, 60 and 90 days, today included.")
    count = serializers.IntegerField(help_text=COUNT_HELP)
    truncated = serializers.BooleanField(help_text=TRUNCATED_HELP)
    results = CompositeDeadlineRowSerializer(many=True)


class CompositeVendorSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    account_manager = CompositePersonSerializer(allow_null=True)
    support_email = serializers.CharField(allow_null=True)


class CompositeOwnersSerializer(ReadOnlySerializer):
    renewal_owner = CompositePersonSerializer(
        allow_null=True, help_text="A renewal's responsible person; a contract's renewal owner."
    )
    contract_owner = CompositePersonSerializer(
        allow_null=True,
        help_text="The renewal owner of the contract on the row (the one driving a renewal, or the contract itself); "
        "null without a contract. Read it when renewal_owner is null.",
    )
    follow_up = CompositeUserSerializer(allow_null=True, help_text="A renewal's IT follow-up; null on contracts.")
    business_owners = CompositePersonSerializer(many=True, help_text="Of the records it covers, each once.")
    technical_owners = CompositePersonSerializer(many=True)
    it_stewards = CompositeUserSerializer(many=True)
    support_teams = CompositeRefSerializer(many=True)


class CompositeRenewalSerializer(CompositeDeadlineSerializer):
    vendor = CompositeVendorSerializer(allow_null=True)
    owners = CompositeOwnersSerializer()
    records = CompositeOwnedRecordSerializer(many=True, help_text="The records it covers, by name; at most 20.")
    records_total = serializers.IntegerField()
    gaps = serializers.ListField(
        child=serializers.ChoiceField(list(GAPS)),
        help_text="Who is missing: no_renewal_owner (neither renewal_owner nor contract_owner), renewal_owner_inactive "
        "(every one set has left), no_follow_up (renewals), no_vendor, no_vendor_contact (a vendor with no account "
        "manager or support email), no_records, and no_business_owner, no_it_steward or no_support_team when any "
        "record it covers lacks one.",
    )


class CompositeRenewalsSerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    within_days = serializers.IntegerField()
    basis = serializers.ChoiceField(
        ["action_deadline", "term_end"], help_text="The date `within_days` filtered on (the `basis` parameter)."
    )
    count = serializers.IntegerField(help_text=COUNT_HELP)
    truncated = serializers.BooleanField(help_text=TRUNCATED_HELP)
    results = CompositeRenewalSerializer(many=True)


# ---- spend


class CompositeSpendGroupSerializer(ReadOnlySerializer):
    key = serializers.CharField(
        allow_null=True, help_text="The vendor, department or category id, or the cost center; null for a gap."
    )
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    gap = serializers.CharField(
        allow_null=True,
        help_text="no_vendor, unallocated, no_department, no_category, not_application, no_cost_center, or other "
        "(the groups folded into Everything else).",
    )
    currency = serializers.CharField()
    amount = serializers.DecimalField(max_digits=16, decimal_places=2)
    contracts = serializers.IntegerField(allow_null=True, help_text="Null on the folded row.")
    records = serializers.IntegerField(allow_null=True, help_text="Null on the folded row.")
    folded = serializers.IntegerField(allow_null=True, help_text="With gap other: how many groups it folds.")


class CompositeSpendSerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    by = serializers.CharField(help_text="vendor, department, category or cost_center.")
    totals = CommitmentSerializer(many=True)
    groups = CompositeSpendGroupSerializer(many=True)
    truncated = serializers.BooleanField(help_text="Some groups were folded into Everything else.")


# ---- dependencies


class CompositeTargetSerializer(CompositeRecordSerializer):
    technical_owner = CompositePersonSerializer(allow_null=True)


class CompositeNodeSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    name = serializers.CharField()
    path = serializers.CharField(help_text=PATH_HELP)
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    hops = serializers.IntegerField(help_text="Links from the target; 1 is direct.")
    via = serializers.CharField(allow_null=True, help_text="The relationship type of the last link, e.g. Depends on.")
    through = serializers.ListField(child=serializers.CharField(), help_text="The records between, in order.")
    cycle = serializers.BooleanField(help_text="This link leads back to a record already on the path.")
    link_state = serializers.ChoiceField(LINK_STATES)
    technical_owner = CompositeOwnerRefSerializer(allow_null=True)


class CompositeFlowSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    path = serializers.CharField(help_text=PATH_HELP)
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    source = CompositeKindRefSerializer()  # type: ignore[assignment]  # the API field, not Field.source
    target = CompositeKindRefSerializer()
    protocol = serializers.CharField(allow_null=True)
    link_state = serializers.ChoiceField(LINK_STATES)
    technical_owner = CompositeOwnerRefSerializer(allow_null=True)


class CompositeGraphSummarySerializer(ReadOnlySerializer):
    nodes = serializers.IntegerField()
    data_flows = serializers.IntegerField()
    cycles = serializers.IntegerField()
    hidden_retired = serializers.IntegerField(help_text="Retired records left out (include_retired=false).")


class CompositeDependenciesSerializer(ReadOnlySerializer):
    target = CompositeTargetSerializer()
    direction = serializers.CharField()
    depth = serializers.CharField(help_text="1 to 10, or all.")
    include_retired = serializers.BooleanField()
    count = serializers.IntegerField(help_text="Every node and data flow the walk found.")
    truncated = serializers.BooleanField(help_text=TRUNCATED_HELP)
    summary = CompositeGraphSummarySerializer()
    depends_on = CompositeNodeSerializer(
        many=True, allow_null=True, help_text="What the target relies on; null unless direction is depends_on or both."
    )
    dependents = CompositeNodeSerializer(
        many=True, allow_null=True, help_text="What relies on the target; null unless direction is dependents or both."
    )
    data_flows = CompositeFlowSerializer(many=True)


# ---- discovery summary


class CompositeDiscoveryCountsSerializer(ReadOnlySerializer):
    candidates = serializers.IntegerField(help_text="new plus changed: what is waiting for a decision.")
    new = serializers.IntegerField()
    changed = serializers.IntegerField(help_text="Dismissed, then changed in the source.")
    linked = serializers.IntegerField()
    dismissed = serializers.IntegerField()
    gone = serializers.IntegerField()
    out_of_scope = serializers.IntegerField()
    missing = serializers.IntegerField(help_text="Linked, no longer seen in the source, not resolved yet.")
    differs = serializers.IntegerField(help_text="Linked, with a source difference nobody ignored.")


class CompositeRunSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    status = serializers.CharField(help_text="queued, running, succeeded or failed.")
    trigger = serializers.CharField(help_text="manual or scheduled.")
    queued_at = serializers.DateTimeField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_null=True, help_text="Why it failed; never a secret.")


class CompositeConnectorSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    source = serializers.CharField(  # type: ignore[assignment]  # the API field, not Field.source
        help_text="entra, entra_licenses, azure, powerplatform, halo, okta, google_workspace, ..."
    )
    enabled = serializers.BooleanField()
    available = serializers.BooleanField(
        help_text="The organization has the source's capability; false: kept, never run (as GET connectors)."
    )
    schedule = serializers.CharField()
    path = serializers.CharField(help_text=PATH_HELP)
    reviewer = CompositeUserSerializer(allow_null=True)
    counts = CompositeDiscoveryCountsSerializer()
    last_run = CompositeRunSerializer(allow_null=True, help_text="The newest run that finished (or never started).")
    active_run = CompositeRunSerializer(allow_null=True, help_text="A run queued or running now.")


class CompositeDiscoverySerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    counts = CompositeDiscoveryCountsSerializer(help_text="Every connector's, as GET discovery/candidates `counts`.")
    connectors_total = serializers.IntegerField()
    truncated = serializers.BooleanField(help_text="More than 100 connectors; the counts still cover all of them.")
    connectors = CompositeConnectorSerializer(many=True)


# ---- data gaps


class CompositeRuleCountSerializer(ReadOnlySerializer):
    id = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    severity = serializers.CharField()
    records = serializers.IntegerField(help_text="Live records the rule applies to.")
    failing = serializers.IntegerField()
    excepted = serializers.IntegerField()


class CompositeFailingRuleSerializer(ReadOnlySerializer):
    id = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    severity = serializers.CharField()


class CompositeGapRecordSerializer(CompositeOwnedRecordSerializer):
    criticality = serializers.CharField(allow_null=True)
    score = serializers.IntegerField(help_text="Completeness, 0 to 100.")
    failing = CompositeFailingRuleSerializer(many=True)


class CompositeDataGapsSerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    score = serializers.IntegerField(allow_null=True, help_text="Compliance percentage, as GET data-quality/summary.")
    records = serializers.IntegerField(help_text="Live records checked.")
    complete = serializers.IntegerField(help_text="Live records failing nothing.")
    by_rule = CompositeRuleCountSerializer(many=True, help_text="Every rule that is on, most severe first.")
    rule = serializers.CharField(allow_null=True)
    kind = serializers.CharField(allow_null=True)
    count = serializers.IntegerField(help_text="Live records failing the rule (or any), of the kind.")
    truncated = serializers.BooleanField(help_text=TRUNCATED_HELP)
    results = CompositeGapRecordSerializer(many=True)


# ---- expiring credentials


class CompositeCredentialAppSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(help_text="The discovery observation of the app registration.")
    name = serializers.CharField()


class CompositeCredentialSerializer(ReadOnlySerializer):
    kind = serializers.ChoiceField(CREDENTIAL_KINDS)
    id = serializers.UUIDField()
    name = serializers.CharField()
    prefix = serializers.CharField(allow_null=True, help_text="An API token's prefix (not the secret).")
    credential_type = serializers.ChoiceField(CredentialType.choices, allow_null=True, help_text="Discovered only.")
    key_id = serializers.CharField(allow_null=True, help_text="Discovered only.")
    expires_on = serializers.DateField()
    days = serializers.IntegerField(help_text="Negative once expired.")
    expired = serializers.BooleanField()
    owner = CompositeUserSerializer(allow_null=True, help_text="An API token's owner.")
    application = CompositeCredentialAppSerializer(allow_null=True, help_text="Discovered: the app it belongs to.")
    record = CompositeOwnedRecordSerializer(allow_null=True, help_text="Discovered: the record the app is linked to.")


class CompositeCredentialsSerializer(ReadOnlySerializer):
    as_of = serializers.DateField(help_text=AS_OF_HELP)
    within_days = serializers.IntegerField()
    count = serializers.IntegerField(help_text=COUNT_HELP)
    truncated = serializers.BooleanField(help_text=TRUNCATED_HELP)
    results = CompositeCredentialSerializer(many=True)
