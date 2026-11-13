"""`GET /api/v1/search`: global search behind the command palette and the results page (core/search.py).

Any member (Viewer and up). Read-only (nothing is written, not even a search history). Results are scoped to the
request organization by every searcher's SQL and limited to the kinds its edition has (core/capabilities.py).
Rate-limited per user (SOFTWARDEN_SEARCH_RATE, per process like the other throttles): 429 with Retry-After."""

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from core.input import whole_number

from . import search
from .drf import PROBLEMS, VIEWER, ReadOnlySerializer, RoleAPIView, filter_errors, policy
from .problems import FieldErrors
from .throttling import UserThrottle, with_api_limits


class SearchThrottle(UserThrottle):
    scope = "search"

    def get_rate(self):
        return settings.SEARCH_RATE or None  # empty: no limit

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        return super().allow_request(request, view)


# ---- response (comments, not docstrings: docstrings become OpenAPI text) -------------------------------------------


class SearchSegmentSerializer(ReadOnlySerializer):
    text = serializers.CharField()
    match = serializers.BooleanField(help_text="Part of the text that matched the query. Render as text, never HTML.")


class SearchSnippetSerializer(ReadOnlySerializer):
    field = serializers.CharField(
        help_text="Where the snippet comes from: description, purpose, vendor, tags, ai_models, custom_field, "
        "details, notes or filename."
    )
    label = serializers.CharField(  # type: ignore[assignment]  # the response member, not Field.label
        help_text="The field's label, a custom field's own label included."
    )
    segments = SearchSegmentSerializer(many=True)


class SearchParentSerializer(ReadOnlySerializer):
    kind = serializers.CharField()
    id = serializers.UUIDField()
    name = serializers.CharField()
    path = serializers.CharField()


class SearchResultSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.CharField()
    name = serializers.CharField(help_text="Name, or an attachment's title.")
    path = serializers.CharField(
        help_text="The result's route under /api/v1/, e.g. applications/<id>, ai-agents/<id>, vendors/<id>, "
        "people/<id>, attachments/<id>; record kinds use the same path in the SPA."
    )
    score = serializers.FloatField(help_text="Relevance within its kind; higher first.")
    archived = serializers.BooleanField(
        help_text="An archived record, an inactive person, or an attachment on an archived record."
    )
    lifecycle = serializers.CharField(allow_null=True, help_text="Records only.")
    snippet = SearchSnippetSerializer(
        allow_null=True,
        help_text="Where the query matched, when it wasn't (only) the name. Null for name matches and typo matches.",
    )
    parent = SearchParentSerializer(allow_null=True, help_text="Attachments only: the record or vendor it is on.")


class SearchGroupSerializer(ReadOnlySerializer):
    kind = serializers.CharField()
    count = serializers.IntegerField(help_text="Every match of this kind; `results` holds at most `limit`.")
    results = SearchResultSerializer(many=True)


class SearchResponseSerializer(ReadOnlySerializer):
    query = serializers.CharField(help_text="`q` as searched: trimmed and normalized to Unicode NFC.")
    ignored = serializers.ListField(
        child=serializers.CharField(),
        help_text="Words of `q` the text search parser finds nothing to match in (a lone symbol such as ²); the "
        "search ran without them.",
    )
    fuzzy = serializers.BooleanField(
        help_text="Nothing matched every word, so these are names similar to the query (typo tolerance)."
    )
    groups = SearchGroupSerializer(many=True)


def flag(params, name: str, errors: dict) -> bool:
    value = params.get(name)
    if value is None or value == "":
        return False
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    errors[name] = ["Use true or false."]
    return False


class SearchView(RoleAPIView):
    role_policy = policy(VIEWER)
    throttle_classes = with_api_limits(SearchThrottle)

    @extend_schema(
        summary="Search records, vendors, people and attachments",
        description="Full-text search (PostgreSQL, `simple` configuration: no stemming). Every word must match; the "
        "last word is a prefix. Records (applications, components, integrations, AI agents) match on name, "
        "description, purpose, their vendor's name, tags, AI models and providers, text-like custom fields and other "
        "text; vendors on name, notes and custom fields; people on name only (never an anonymized person); "
        "attachments on title and file name. Results come grouped by kind, best first. When nothing matches, names "
        "similar to the query are returned with `fuzzy: true`. Archived records, inactive people and attachments on "
        "archived records are left out unless `include_archived=true`. AI agents need the ai_agents capability and "
        "attachments the attachments capability: left out by default, 403 when asked for in `kinds`. Rate-limited "
        "per user (429 with Retry-After). Contracts are searched too.",
        parameters=[
            OpenApiParameter("q", str, required=True, description=f"At most {search.QUERY_MAX} characters."),
            OpenApiParameter(
                "kinds",
                str,
                description="Comma-separated: application, component, integration, ai_agent, vendor, person, "
                "attachment. Default: every kind the organization has.",
            ),
            OpenApiParameter(
                "limit",
                int,
                description=f"Results per kind, 1 to {search.LIMIT_MAX} (default {search.LIMIT_DEFAULT}).",
            ),
            OpenApiParameter("include_archived", bool, description="Default false."),
        ],
        responses={
            200: SearchResponseSerializer,
            400: PROBLEMS[400],
            403: PROBLEMS[403],
            429: OpenApiResponse(description="Too many searches; Retry-After says when to try again"),
        },
    )
    def get(self, request):
        return Response(search.run(self.organization, *search_options(self.organization, request.query_params)))


SEARCH_PARAMS = ("q", "kinds", "limit", "include_archived")


def search_options(org, params) -> tuple[search.Query, list[str], int, bool]:
    """`q`, `kinds`, `limit` and `include_archived` validated, as search.run's arguments after the organization; every
    problem in one 400. Shared with GET /composite/search (api/composite_views.py), so both take the same values."""
    errors = filter_errors(None, params, SEARCH_PARAMS)
    query: search.Query | None = None
    kinds: list[str] = []
    try:
        query = search.searchable(search.parse(params.get("q")))
    except FieldErrors as exc:
        errors.update(exc.errors)
    limit = search.LIMIT_DEFAULT
    raw_limit = params.get("limit")
    if raw_limit not in (None, ""):
        wanted = whole_number(raw_limit, 1, search.LIMIT_MAX)
        if wanted is not None:
            limit = wanted
        else:
            errors["limit"] = [f"Enter a whole number from 1 to {search.LIMIT_MAX}."]
    include_archived = flag(params, "include_archived", errors)
    try:
        kinds = search.requested_kinds(org, params.get("kinds"))
    except FieldErrors as exc:
        errors.update(exc.errors)
    if errors or query is None:
        raise FieldErrors(errors)
    return query, kinds, limit, include_archived
