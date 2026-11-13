"""Serializers for the import viewsets (imports/viewsets.py; E29). Output serializers
render import sources, runs and rows, read-only (core/drf.ReadOnlySerializer); input serializers only describe
request bodies, because imports/importer.py validates every write and owns its messages.

A run's uploaded bytes never leave the server: no serializer has a field for them, and they aren't an ETag input."""

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog import compliance, customfields
from core.drf import BriefSerializer, ReadOnlySerializer, UserBriefSerializer

from . import csvio, importer
from .models import KINDS, MONEY_KINDS, ImportStatus, RowOutcome

KIND_CHOICES = list(KINDS)  # imports.models.KINDS, as plain values (ImportKindEnum)
EMPTY_PANELS: dict[str, Any] = {"people": [], "teams": [], "vendors": [], "matches": [], "matched": {}, "notices": []}


class ImportSourceSerializer(ReadOnlySerializer):
    """Context: `identifiers` {source id: external keys stored}, computed once per page."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    kind = serializers.ChoiceField(KIND_CHOICES, read_only=True)
    identifiers = serializers.SerializerMethodField(
        help_text="External keys stored for this source; deleting needs none."
    )
    column_mapping = serializers.DictField(read_only=True, help_text="{heading: column | external_key | ignore}")
    created_at = serializers.DateTimeField(read_only=True)

    def get_identifiers(self, s) -> int:
        return self.context["identifiers"].get(s.id, 0)


class ImportSourceInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    kind = serializers.ChoiceField(KIND_CHOICES, required=False, help_text="Required when creating; never changes.")


class ImportUploadSerializer(serializers.Serializer):
    file = serializers.FileField(help_text="UTF-8 CSV with the template's headings; up to 5 MB and 5,000 rows.")
    kind = serializers.ChoiceField(KIND_CHOICES)
    source = serializers.UUIDField(  # type: ignore[assignment]  # the field, not Field.source
        required=False, help_text="An import source of the same kind."
    )


class ImportRunSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    kind = serializers.ChoiceField(KIND_CHOICES, read_only=True)
    source = BriefSerializer(read_only=True, allow_null=True)  # type: ignore[assignment]  # the field, not Field.source
    filename = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(ImportStatus.choices, read_only=True)
    uploaded_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    previewed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    committed_at = serializers.DateTimeField(read_only=True, allow_null=True)
    refused_reason = serializers.CharField(read_only=True)
    purge_after = serializers.DateField(read_only=True)
    purged_at = serializers.DateTimeField(read_only=True, allow_null=True)
    purged = serializers.SerializerMethodField()
    counts = serializers.DictField(
        read_only=True,
        help_text="total, create, update, unchanged, error, excluded, needs_confirmation, new_keys, blocking_errors, "
        "pending_decisions, people_to_create, teams_to_create, vendors_to_create; after a commit also created, "
        "updated, keys_stored, people_created, teams_created, vendors_created. Contracts and license pools also "
        "rehearsed: 1 when the preview tried each row's writes (500 rows or fewer), 0 when the rules are checked at commit.",
    )

    def get_purged(self, r) -> bool:
        return r.purged_at is not None


class ImportRunDetailSerializer(ImportRunSerializer):
    decisions = serializers.SerializerMethodField(
        help_text="people, teams, vendors: {key: create|gap}; matches: {row: create|id}"
    )
    panels = serializers.SerializerMethodField(
        help_text="people, teams, vendors: [{key, email, name, rows, fields, decision}] needing a choice; "
        "matches: [{row_number, name, candidate {id, name, known_to_other_source}, decision}]; "
        "matched: {people, teams, vendors}; notices: [text] to show above the preview (contracts and license pools: "
        "a file over 500 rows isn't rehearsed, so the contract rules are checked at commit)."
    )
    can_commit = serializers.SerializerMethodField()
    needs_mapping = serializers.SerializerMethodField(
        help_text="True while the run is uploaded and its columns need mapping."
    )
    headers = serializers.ListField(child=serializers.CharField(), read_only=True, help_text="The file's headings.")
    mapping = serializers.SerializerMethodField(
        help_text="{heading: column | external_key | ignore} once mapped; {} for template files."
    )
    suggested_mapping = serializers.SerializerMethodField(
        help_text="While needs_mapping: the same shape, pre-filled; else {}."
    )
    # The API field `fields`; its method can't be get_fields, which is Serializer's own.
    fields = serializers.SerializerMethodField(  # type: ignore[assignment]  # the API field, not Serializer.fields
        method_name="get_mapping_fields",
        help_text="While needs_mapping: [{value, label, required}], the columns to map to.",
    )

    @extend_schema_field(serializers.DictField())
    def get_decisions(self, r) -> dict[str, Any]:
        return {k: v for k, v in (r.decisions or {}).items() if k != "derived"}

    @extend_schema_field(serializers.DictField())
    def get_panels(self, r) -> dict[str, Any]:
        return (r.decisions or {}).get("derived", {}).get("panels") or EMPTY_PANELS

    def get_can_commit(self, r) -> bool:
        c = r.counts or {}
        work = c.get("create", 0) + c.get("update", 0) + c.get("new_keys", 0)
        return bool(
            r.purged_at is None
            and r.status == ImportStatus.PREVIEWED
            and not c.get("blocking_errors")
            and not c.get("pending_decisions")
            and work > 0
        )

    def get_needs_mapping(self, r) -> bool:
        return importer.needs_mapping(r)

    @extend_schema_field(serializers.DictField(child=serializers.CharField()))
    def get_mapping(self, r) -> dict[str, str]:
        return r.mapping or {}

    @extend_schema_field(serializers.DictField(child=serializers.CharField()))
    def get_suggested_mapping(self, r) -> dict[str, str]:
        if not importer.needs_mapping(r):
            return {}
        org = r.organization
        return importer.suggest_mapping(
            r.kind, r.headers, r.source, compliance.enabled(org), customfields.for_kind(org, r.kind)
        )

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_mapping_fields(self, r) -> list[dict[str, Any]]:
        if not importer.needs_mapping(r):
            return []
        org = r.organization
        return importer.mapping_fields(r.kind, compliance.enabled(org), customfields.for_kind(org, r.kind))


class ImportUpdateSerializer(serializers.Serializer):
    decisions = serializers.DictField(required=False)
    exclude = serializers.ListField(child=serializers.IntegerField(), required=False)
    include = serializers.ListField(child=serializers.IntegerField(), required=False)
    mapping = serializers.DictField(
        required=False, help_text="{heading: column | external_key | ignore}, every heading."
    )
    save_mapping = serializers.BooleanField(required=False, help_text="Remember the mapping on the run's source.")


# ---- rows -----------------------------------------------------------------------------------------------------------


class RowMessageSerializer(serializers.Serializer):
    column = serializers.CharField()
    message = serializers.CharField()


def _messages(row, level) -> list[dict[str, str]]:
    return [
        {"column": e.get("column", ""), "message": e["message"]} for e in (row.errors or []) if e.get("level") == level
    ]


class ImportResultSerializer(BriefSerializer):
    """What a row's commit wrote: a record (its kind), a contract or a license pool, so a client can link to it."""

    kind = serializers.ChoiceField(KIND_CHOICES, read_only=True)


class ImportRowSerializer(ReadOnlySerializer):
    """One CSV row of a run, addressed by its row number (row 1 is the headings)."""

    row_number = serializers.IntegerField(read_only=True)
    name = serializers.SerializerMethodField()
    outcome = serializers.ChoiceField(RowOutcome.choices, read_only=True)
    excluded = serializers.BooleanField(read_only=True)
    errors = serializers.SerializerMethodField(help_text="[{column, message}]; block the commit.")  # type: ignore[assignment]
    warnings = serializers.SerializerMethodField()
    gaps = serializers.SerializerMethodField()
    needs_decision = serializers.SerializerMethodField()
    changes = serializers.DictField(read_only=True, help_text="{field: [before, after]}")
    needs_confirmation = serializers.BooleanField(read_only=True)
    matched = BriefSerializer(
        source="matched_object", read_only=True, allow_null=True, help_text="The record, contract or pool it updates."
    )
    possible_match = BriefSerializer(source="possible_object", read_only=True, allow_null=True)
    result = serializers.SerializerMethodField(help_text="What the commit created or updated, with its kind.")
    computed = serializers.SerializerMethodField(
        help_text="Contracts: annualized_cost, currency, annualization_note, cancellation_deadline, deadline_label, "
        "deadline_explanation, renewal (id), allocation [{record {id, name, kind} | null, share, amount}]. License "
        "pools: seats_purchased, seats_assigned, utilization. What the preview worked out; {} for records."
    )

    @extend_schema_field(ImportResultSerializer(allow_null=True))
    def get_result(self, row) -> dict[str, Any] | None:
        found = row.result_object
        if found is None:
            return None
        kind = found.kind if row.run.kind not in MONEY_KINDS else row.run.kind
        return {"id": str(found.id), "name": str(found.name), "kind": kind}

    @extend_schema_field(serializers.DictField())
    def get_computed(self, row) -> dict[str, Any]:
        return row.computed or {}

    def get_name(self, row) -> str:
        return (row.raw or {}).get(csvio.name_column(row.run.kind), "")

    @extend_schema_field(RowMessageSerializer(many=True))
    def get_errors(self, row) -> list[dict[str, str]]:
        return _messages(row, "error")

    @extend_schema_field(RowMessageSerializer(many=True))
    def get_warnings(self, row) -> list[dict[str, str]]:
        return _messages(row, "warning")

    def get_gaps(self, row) -> list[str]:
        return [e["message"] for e in (row.errors or []) if e.get("level") == "gap"]

    @extend_schema_field(RowMessageSerializer(many=True))
    def get_needs_decision(self, row) -> list[dict[str, str]]:
        return _messages(row, "decision")


class ImportRowPageSerializer(serializers.Serializer):
    count = serializers.IntegerField(help_text="Rows matching the filters.")
    results = ImportRowSerializer(many=True, help_text="By row number.")
