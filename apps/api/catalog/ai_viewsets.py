"""AI providers and models: reference entities with their filters, and the
"models in use" report. Lists are plain arrays ordered by name, like the other reference data (vendors, categories).

Roles: Viewer reads (the report too); Editor creates, edits and deletes. Every route needs the `ai_agents`
capability (community, so on in every edition today). A provider with models, or a model an agent
uses, can't be deleted (409 naming what uses it). Every write goes through catalog/ai.py."""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import AI_AGENTS
from core.drf import EDITOR, PROBLEMS, VIEWER, TenantViewSet, filter_errors, policy
from core.problems import FieldErrors

from . import ai
from .filters import AIModelFilterSet
from .models import AIModel, AIProvider
from .serializers import (
    AIModelInputSerializer,
    AIModelSerializer,
    AIProviderInputSerializer,
    AIProviderSerializer,
    ModelInUseSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}


@extend_schema_view(
    list=extend_schema(summary="List AI providers"),
    create=extend_schema(summary="Add an AI provider (Editor, Admin)"),
    retrieve=extend_schema(summary="Get an AI provider"),
    partial_update=extend_schema(summary="Update an AI provider (Editor, Admin)"),
    destroy=extend_schema(summary="Delete an AI provider (Editor, Admin; refused while it has models)"),
)
class AIProviderViewSet(TenantViewSet):
    """Who makes or hosts AI models. `usage` is the number of the provider's models."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = AI_AGENTS
    pagination_class = None
    serializer_class = AIProviderSerializer
    input_serializer_class = AIProviderInputSerializer
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return AIProvider.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"usage": ai.provider_usage(self.organization)}

    def create_object(self, data):
        return ai.create_provider(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return ai.update_provider(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        ai.delete_provider(self.organization, self.request.user, obj)


@extend_schema_view(
    list=extend_schema(summary="List AI models"),
    create=extend_schema(summary="Add an AI model (Editor, Admin)"),
    retrieve=extend_schema(summary="Get an AI model"),
    partial_update=extend_schema(summary="Update an AI model (Editor, Admin)"),
    destroy=extend_schema(summary="Delete an AI model (Editor, Admin; refused while an agent uses it)"),
    in_use=extend_schema(
        summary="Models in use: how many agents use each model",
        description="Any role. Every model at least one unarchived AI agent uses, most used first, then by name. "
        "`live_agents` counts the Active and Deprecated ones. Models no agent uses are left out.",
        responses={200: ModelInUseSerializer(many=True), **ERRORS},
    ),
)
class AIModelViewSet(TenantViewSet):
    """A provider's models, filtered by provider, modality, hosting region or use. `usage` is the number of agents
    using the model, archived agents included (what blocks a delete)."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = AI_AGENTS  # the in-use report too
    pagination_class = None
    serializer_class = AIModelSerializer
    input_serializer_class = AIModelInputSerializer
    filterset_class = AIModelFilterSet
    ordering_fields = ("name", "created_at")
    default_ordering = ("name", "version")

    def base_queryset(self):
        return AIModel.objects.select_related("provider")

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"usage": ai.model_usage(self.organization)}

    def create_object(self, data):
        return ai.create_model(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return ai.update_model(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        ai.delete_model(self.organization, self.request.user, obj)

    @action(detail=False, methods=["get"], url_path="in-use", filter_backends=[], pagination_class=None)
    def in_use(self, request):
        errors = filter_errors(None, request.query_params)
        if errors:
            raise FieldErrors(errors)
        return Response(ai.models_in_use(self.organization))
