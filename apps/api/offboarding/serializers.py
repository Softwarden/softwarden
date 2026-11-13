"""OpenAPI shapes of the offboarding and access review endpoints. They describe; the views build plain dicts."""

from rest_framework import serializers

from .access_review import FLAGS
from .reassign import ROLE_NAMES, ROW_STATUSES
from .references import SECTIONS


class FootprintItemSerializer(serializers.Serializer):
    type = serializers.CharField(
        help_text="application, component, ai_agent, integration, team, vendor, contract, renewal, relationship, "
        "assessment, quality_exception, notification_rule, connector, observation, license (its id is the SKU ID), "
        "membership, coordinator, api_token, calendar_feed, saved_view or subscription."
    )
    id = serializers.CharField(allow_null=True)
    name = serializers.CharField()
    path = serializers.CharField(allow_null=True, help_text="The SPA page to open; null when there is none.")
    roles = serializers.ListField(
        child=serializers.CharField(),
        help_text="How the person appears on it: business_owner, technical_owner, run_as, it_steward, member, "
        "account_manager, renewal_owner, responsible, followup, confirmed_review, confirmed_link, owner, accountable, "
        "audience, escalation, fallback, reviewer, source_owner, assigned (a license), coordinator, subscriber, or a "
        "membership's role.",
    )
    archived = serializers.BooleanField()
    detail = serializers.CharField(help_text="A short note in words, such as how a match was made; may be empty.")


class FootprintSectionSerializer(serializers.Serializer):
    key = serializers.ChoiceField(list(SECTIONS))
    label = serializers.CharField()  # type: ignore[assignment]  # the member, not Field.label
    available = serializers.BooleanField(help_text="False: see `reason`; `items` is empty.")
    reason = serializers.CharField(
        help_text="Why the section is unavailable: a capability the organization lacks, Admins only, no user "
        "account, or 'Licenses: not read; needs User.Read.All' until a Microsoft Entra license usage connector has "
        "read the tenant's licensed users. Empty when available."
    )
    count = serializers.IntegerField()
    items = FootprintItemSerializer(many=True)


class FootprintAccountSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.CharField()
    active = serializers.BooleanField()
    role = serializers.CharField(allow_null=True, help_text="Their role here; null when no longer a member.")


class FootprintPersonSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    email = serializers.CharField()
    active = serializers.BooleanField()
    anonymized = serializers.BooleanField()
    account = FootprintAccountSerializer(
        allow_null=True, help_text="Their user account: Admins only; null for other roles or when they have none."
    )


class FootprintSerializer(serializers.Serializer):
    person = FootprintPersonSerializer()
    total = serializers.IntegerField(help_text="Items over every available section.")
    sections = FootprintSectionSerializer(many=True)


class ReassignInputSerializer(serializers.Serializer):
    to = serializers.UUIDField(help_text="The active person who takes over.")
    roles = serializers.ListField(child=serializers.ChoiceField(ROLE_NAMES), min_length=1)
    dry_run = serializers.BooleanField(required=False, default=False, help_text="Only say what would change.")
    plan_hash = serializers.CharField(
        required=False,
        help_text="A real run only: the dry run's plan_hash. If the rows it would change differ now, the run is refused "
        "(409 /problems/reassign-stale) and nothing is saved.",
    )


class ReassignRowSerializer(serializers.Serializer):
    type = serializers.CharField()
    id = serializers.CharField()
    name = serializers.CharField()
    path = serializers.CharField(allow_null=True)
    roles = serializers.ListField(child=serializers.ChoiceField(ROLE_NAMES))
    status = serializers.ChoiceField(ROW_STATUSES)
    reason = serializers.CharField(help_text="Why it was skipped; empty otherwise.")
    errors = serializers.ListField(  # type: ignore[assignment]  # the member, not Serializer.errors
        child=serializers.CharField(), required=False, help_text="Refused rows only: the service's messages."
    )
    field_errors = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        help_text="Refused rows only: the single edit's field errors the row's service refused it with.",
    )


class PersonRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class ReassignResultSerializer(serializers.Serializer):
    person = PersonRefSerializer(help_text="The person leaving.")
    to = PersonRefSerializer(help_text="The person taking over.")
    roles = serializers.ListField(child=serializers.ChoiceField(ROLE_NAMES))
    dry_run = serializers.BooleanField()
    changed = serializers.IntegerField(help_text="Rows changed (or, in a dry run, that would change).")
    skipped = serializers.IntegerField(help_text="Rows left as they are: archived.")
    refused = serializers.IntegerField(
        help_text="Rows a dry run found the real run would refuse (status refused, with errors). Always 0 in a real "
        "run: a real run that would refuse any row is a 400 and saves nothing."
    )
    rows = ReassignRowSerializer(many=True)
    plan_hash = serializers.CharField(
        help_text="SHA-256 of the organization, both people, the roles and the rows that change: send it with the real "
        "run to refuse it if anything changed since this dry run."
    )


class AccessReviewOwnerSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    active = serializers.BooleanField()
    status = serializers.ChoiceField(
        ["active", "inactive", "left"],
        help_text="left: an active Person whose user account was deactivated or removed from the organization.",
    )


class AccessReviewRecordSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()
    lifecycle = serializers.CharField()
    path = serializers.CharField(allow_null=True)


class EntraPersonSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    active = serializers.BooleanField()


class EntraOwnerSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Empty when the source couldn't name the owner (owner not resolved).")
    email = serializers.CharField()
    resolved = serializers.BooleanField()
    source = serializers.CharField(  # type: ignore[assignment]  # the member, not Field.source
        help_text="The discovery connector's name."
    )
    person = EntraPersonSerializer(allow_null=True, help_text="The Person matched by Entra object ID or email.")


class AccessReviewRowSerializer(serializers.Serializer):
    record = AccessReviewRecordSerializer()
    business_owner = AccessReviewOwnerSerializer(allow_null=True)
    technical_owner = AccessReviewOwnerSerializer(allow_null=True)
    flags = serializers.ListField(child=serializers.ChoiceField(list(FLAGS)))
    entra_owners = EntraOwnerSerializer(many=True)
    why = serializers.CharField()


class AccessReviewCountsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    flagged = serializers.IntegerField()
    inactive_owner = serializers.IntegerField()
    owner_left = serializers.IntegerField()
    no_owner = serializers.IntegerField()
    entra_owner_mismatch = serializers.IntegerField()


class AccessReviewSerializer(serializers.Serializer):
    counts = AccessReviewCountsSerializer(help_text="Over the records in scope, before the flag filter.")
    results = AccessReviewRowSerializer(many=True)
