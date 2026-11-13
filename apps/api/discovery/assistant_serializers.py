"""Serializers for the assistant (capability `assistant`; discovery/assistant.py). Read-only renderings; the
input serializers only document request bodies (the services validate). The API key is write-only: responses say
whether one is set, since when and where it comes from, never the value."""

from typing import Any

from rest_framework import serializers

from core.drf import ReadOnlySerializer, UserBriefSerializer, iso, represent, timestamp

from . import assistant
from .models import (
    AssistantBatch,
    AssistantBatchStatus,
    AssistantOutcome,
    AssistantSettings,
    AssistantSuggestion,
    AssistantTrigger,
    AssistantUsage,
    SuggestionOutcome,
    SuggestionState,
)


class AssistantMonthSerializer(ReadOnlySerializer):
    start = serializers.DateField(help_text="The month's first day, in the organization's timezone.")
    end = serializers.DateField(help_text="The next month's first day.")
    to_date_usd = serializers.CharField(
        help_text="The cost of the month's calls in USD (6 decimals), calls in progress "
        "counted at their reserved worst case."
    )
    cap_usd = serializers.CharField(help_text="The monthly cap in USD (2 decimals).")
    remaining_usd = serializers.CharField(help_text="Cap minus to_date_usd, never below 0.")
    cap_reached = serializers.BooleanField(
        help_text="This month a batch stopped because the next call could pass the cap. Only for the current month."
    )
    cap_reached_at = serializers.DateTimeField(allow_null=True)


class AssistantPricesSerializer(ReadOnlySerializer):
    input = serializers.CharField(help_text="USD per million input tokens.")
    output = serializers.CharField(help_text="USD per million output tokens.")
    cache_read = serializers.CharField(help_text="USD per million cache-read input tokens.")
    cache_write = serializers.CharField(help_text="USD per million cache-write input tokens.")


class AssistantSettingsSerializer(ReadOnlySerializer):
    """The organization's assistant settings, or the defaults until an Admin saves them (`saved` false)."""

    saved = serializers.BooleanField()
    enabled = serializers.BooleanField(help_text="Suggestions are made only while this is on.")
    api_key_set = serializers.BooleanField(help_text="The organization's own key is stored. Never returned.")
    api_key_set_on = serializers.DateField(allow_null=True, help_text="When it was set (date only).")
    api_key_source = serializers.ChoiceField(
        ["organization", "installation", "none"],
        help_text="The key calls use: the organization's, the installation's (ANTHROPIC_API_KEY), or none.",
    )
    installation_key_available = serializers.BooleanField(
        help_text="The installation's key may be used: ANTHROPIC_API_KEY and SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD "
        "are both set."
    )
    installation_cap_usd = serializers.CharField(
        allow_null=True,
        help_text="The installation's monthly ceiling per organization on its key; month.cap_usd is the lower of it "
        "and monthly_cap_usd while the installation key is in use.",
    )
    model = serializers.CharField(help_text="The Claude model (installation-wide, SOFTWARDEN_ASSISTANT_MODEL).")
    model_priced = serializers.BooleanField(help_text="False: the model has no price, so the assistant can't run.")
    prices = AssistantPricesSerializer(allow_null=True)
    monthly_cap_usd = serializers.CharField(help_text="USD, 2 decimals. Default 10.00.")
    month = AssistantMonthSerializer(help_text="This month against the effective cap.")
    key_refused_at = serializers.DateTimeField(
        allow_null=True, help_text="The API refused the key and the assistant was turned off."
    )
    key_refused_detail = serializers.CharField()
    updated_at = serializers.DateTimeField(allow_null=True)

    def to_representation(self, instance):
        row: AssistantSettings | None = instance
        org = self.context["organization"]
        price = assistant.prices()
        ceiling = assistant.installation_cap() if assistant.installation_key_available() else None
        return {
            "saved": row is not None,
            "enabled": bool(row and row.enabled),
            "api_key_set": bool(row and row.api_key_ciphertext),
            "api_key_set_on": row.api_key_set_at.date().isoformat() if row and row.api_key_set_at else None,
            "api_key_source": assistant.key_source(row),
            "installation_key_available": assistant.installation_key_available(),
            "installation_cap_usd": f"{ceiling:.2f}" if ceiling is not None else None,
            "model": assistant.current_model(),
            "model_priced": price is not None,
            "prices": {k: str(v) for k, v in price.items()} if price is not None else None,
            "monthly_cap_usd": f"{row.monthly_cap_usd:.2f}" if row else f"{assistant.DEFAULT_CAP:.2f}",
            "month": assistant.month_summary(org),
            "key_refused_at": iso(row.key_refused_at) if row else None,
            "key_refused_detail": row.key_refused_detail if row else "",
            "updated_at": iso(row.updated_at) if row else None,
        }


class AssistantSettingsInputSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False, help_text="Needs a key (organization or installation).")
    api_key = serializers.CharField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="An Anthropic API key; replaces the stored one, null removes it. Never returned.",
    )
    monthly_cap_usd = serializers.CharField(required=False, help_text="USD above 0, up to 10000, at most 2 decimals.")


class AssistantUsageRowSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    created_at = serializers.DateTimeField()
    model = serializers.CharField()
    input_tokens = serializers.IntegerField()
    output_tokens = serializers.IntegerField()
    cache_read_input_tokens = serializers.IntegerField()
    cache_creation_input_tokens = serializers.IntegerField()
    estimated_cost_usd = serializers.CharField(help_text="The worst case reserved before the call.")
    cost_usd = serializers.CharField(help_text="The actual cost (the reservation while in progress).")
    outcome = serializers.ChoiceField(AssistantOutcome.choices)
    outcome_label = serializers.CharField()
    candidate_count = serializers.IntegerField()
    batch = serializers.UUIDField(allow_null=True)

    def to_representation(self, instance):
        u: AssistantUsage = instance
        return {
            "id": str(u.id),
            "created_at": timestamp(u.created_at),
            "model": u.model,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": u.cache_read_input_tokens,
            "cache_creation_input_tokens": u.cache_creation_input_tokens,
            "estimated_cost_usd": f"{u.estimated_cost_usd:.6f}",
            "cost_usd": f"{u.cost_usd:.6f}",
            "outcome": u.outcome,
            "outcome_label": str(AssistantOutcome(u.outcome).label),
            "candidate_count": u.candidate_count,
            "batch": str(u.batch_id) if u.batch_id else None,
        }


class AssistantUsageSerializer(ReadOnlySerializer):
    month = AssistantMonthSerializer()
    results = AssistantUsageRowSerializer(many=True, help_text="The month's calls, newest first.")
    truncated = serializers.BooleanField(help_text="The month has more calls than the list shows (USAGE_SHOWN).")


class AssistantBatchSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    trigger = serializers.ChoiceField(AssistantTrigger.choices)
    status = serializers.ChoiceField(AssistantBatchStatus.choices)
    connector = serializers.DictField(allow_null=True, help_text="{id, name}, or null for every connector.")
    requested_by = UserBriefSerializer(allow_null=True)
    queued_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)
    counts = serializers.DictField(
        help_text="candidates, unchanged (not sent again: fingerprint unchanged since its suggestion), suggested, "
        "refused, failed, calls, remaining (left for the next batch)."
    )
    stop_reason = serializers.CharField(help_text="Why it ended early (cap_reached, key_refused, ...), or empty.")
    error = serializers.CharField(help_text="The stop reason in words.")
    job_id = serializers.UUIDField(allow_null=True)

    def to_representation(self, instance):
        b: AssistantBatch = instance
        return {
            "id": str(b.id),
            "trigger": b.trigger,
            "status": b.status,
            "connector": {"id": str(b.connector.id), "name": b.connector.name} if b.connector else None,
            "requested_by": represent(UserBriefSerializer, b.requested_by),
            "queued_at": timestamp(b.queued_at),
            "started_at": iso(b.started_at),
            "finished_at": iso(b.finished_at),
            "counts": b.counts or {},
            "stop_reason": b.stop_reason,
            "error": b.error,
            "job_id": str(b.job_id) if b.job_id else None,
        }


class AssistantBatchRequestSerializer(serializers.Serializer):
    connector = serializers.UUIDField(
        required=False, allow_null=True, help_text="One connector's candidates; omit for all."
    )


class AssistantRecordSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField()


class AssistantSuggestionSerializer(ReadOnlySerializer):
    """A candidate's newest assistant suggestion (only with the assistant capability)."""

    id = serializers.UUIDField()
    state = serializers.ChoiceField(SuggestionState.choices, help_text="ready, or why there is none (refused, failed).")
    action = serializers.CharField(help_text="link, create or dismiss; empty unless ready.")
    record = AssistantRecordSerializer(allow_null=True, help_text="For link: the suggested record.")
    confidence = serializers.FloatField(allow_null=True, help_text="0 to 1.")
    reason = serializers.CharField()
    detail = serializers.CharField(help_text="Why it is unavailable (a refusal's category, a cut-off answer, ...).")
    model = serializers.CharField()
    current = serializers.BooleanField(help_text="Made for the candidate as it is now (its fingerprint).")
    outcome = serializers.ChoiceField(SuggestionOutcome.choices)
    outcome_at = serializers.DateTimeField(allow_null=True)
    outcome_by = UserBriefSerializer(allow_null=True)
    via_accept = serializers.BooleanField(help_text="The decision was taken with Accept.")
    decided_action = serializers.CharField(help_text="What was decided (link, create or dismiss), or empty.")
    note = serializers.CharField(help_text="The Reject note.")
    created_at = serializers.DateTimeField()

    def to_representation(self, instance):
        s: AssistantSuggestion = instance
        obs = self.context.get("observation")
        target = s.target
        return {
            "id": str(s.id),
            "state": s.state,
            "action": s.action,
            "record": {"id": str(target.id), "kind": target.kind, "name": target.name} if target else None,
            "confidence": float(s.confidence) if s.confidence is not None else None,
            "reason": s.reason,
            "detail": s.detail,
            "model": s.model,
            "current": obs is not None and s.fingerprint == obs.fingerprint,
            "outcome": s.outcome,
            "outcome_at": iso(s.outcome_at),
            "outcome_by": represent(UserBriefSerializer, s.outcome_by),
            "via_accept": s.via_accept,
            "decided_action": s.decided_action,
            "note": s.outcome_note,
            "created_at": timestamp(s.created_at),
        }


def suggestion_data(suggestion: AssistantSuggestion | None, obs) -> dict[str, Any] | None:
    if suggestion is None:
        return None
    return AssistantSuggestionSerializer(context={"observation": obs}).to_representation(suggestion)


class AssistantAcceptInputSerializer(serializers.Serializer):
    suggestion = serializers.UUIDField(
        help_text="The suggestion being accepted (the candidate's assistant_suggestion.id). 409 when it is no longer "
        "the candidate's current, undecided suggestion."
    )
    reason = serializers.CharField(required=False, help_text="Dismiss only: defaults to the suggestion's reason.")
    name = serializers.CharField(required=False, help_text="Create only: as on create.")
    kind = serializers.CharField(required=False, help_text="Create only.")
    lifecycle = serializers.CharField(required=False, help_text="Create only.")
    description = serializers.CharField(required=False, help_text="Create only.")
    custom_field_data = serializers.DictField(required=False, help_text="Create only.")


class AssistantRejectInputSerializer(serializers.Serializer):
    suggestion = serializers.UUIDField(
        help_text="The suggestion being rejected. 409 when it is no longer the candidate's current, undecided "
        "suggestion."
    )
    note = serializers.CharField(required=False, allow_blank=True, help_text="Why not (optional, 500 characters).")
