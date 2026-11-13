"""The assistant's API (capability `assistant`; discovery/assistant.py). Routes are in
discovery/urls.py. Every route needs the capability (403 problem naming it, after the role check).

Roles: the settings and the usage log are Admin only (reads included: they show the spend and the key's state); anyone
signed in reads the batches; Editors and Admins ask for a batch, and accept or reject a suggestion (on the candidate,
discovery/viewsets.py). Every write goes through discovery/assistant.py. The API key is write-only: no
response, ETag or audit event carries it."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response

from core.capabilities import ASSISTANT
from core.drf import (
    ADMIN,
    EDITOR,
    IF_MATCH,
    PROBLEMS,
    VIEWER,
    RoleAPIView,
    etag,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
)
from core.problems import FieldErrors

from . import assistant
from .assistant_serializers import (
    AssistantBatchRequestSerializer,
    AssistantBatchSerializer,
    AssistantSettingsInputSerializer,
    AssistantSettingsSerializer,
    AssistantUsageRowSerializer,
    AssistantUsageSerializer,
)
from .models import AssistantBatch, AssistantSettings, AssistantUsage

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
SECRET_FIELDS = ("api_key_ciphertext", "api_key_key_id")  # never an ETag input; api_key_set_at still moves it
BATCHES_SHOWN = 50
USAGE_SHOWN = 1000
MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def settings_revision(row: AssistantSettings | None) -> str:
    return revision(row, skip=SECRET_FIELDS) if row is not None else revision(extra={"assistant_settings": None})


class AssistantSettingsView(RoleAPIView):
    """The organization's assistant settings. GET answers the defaults (off, $10 cap) until an Admin saves."""

    role_policy = policy(ADMIN, ADMIN, capability=ASSISTANT)

    def respond(self, row: AssistantSettings | None) -> Response:
        serializer = AssistantSettingsSerializer(context={"organization": self.organization})
        response = Response(serializer.to_representation(row))
        response["ETag"] = etag(settings_revision(row))
        return response

    @extend_schema(summary="Assistant settings (Admin)", responses={200: AssistantSettingsSerializer, **ERRORS})
    def get(self, request):
        refuse_unknown_params(request)
        return self.respond(assistant.settings_of(self.organization))

    @extend_schema(
        summary="Change the assistant settings (Admin); send api_key only to replace it",
        description="Turn the assistant on or off, replace or remove the organization's Anthropic API key (without "
        "one the installation's ANTHROPIC_API_KEY is used, if set), and set the monthly cost cap in USD. The key is "
        "never returned: api_key_set and api_key_set_on say whether and since when. Data sent to the Anthropic API: "
        "docs/PERMISSIONS.md, Assistant.",
        parameters=[IF_MATCH],
        request=AssistantSettingsInputSerializer,
        responses={200: AssistantSettingsSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    )
    def patch(self, request):
        data = self.payload()
        with transaction.atomic():
            row = AssistantSettings.objects.select_for_update().filter(organization=self.organization).first()
            require_match(request, settings_revision(row))
            assistant.update_settings(self.organization, request.user, data)
            return self.respond(assistant.settings_of(self.organization))


class AssistantUsageView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN, capability=ASSISTANT)

    @extend_schema(
        summary="The assistant's usage log for a month, with its total against the cap (Admin)",
        description="One row per Anthropic API call: model, the four token counts, the cost in USD (the reserved "
        "worst case while a call is in progress), the outcome and how many candidates it carried; newest first, at "
        f"most {USAGE_SHOWN}. `month` is YYYY-MM in the organization's timezone, default the current month.",
        parameters=[OpenApiParameter("month", str, description="YYYY-MM; default this month.")],
        responses={200: AssistantUsageSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        unknown = set(request.query_params) - {"month"}
        errors = {k: ["This list doesn't take this parameter."] for k in sorted(unknown)}
        start = None
        raw = request.query_params.get("month")
        if raw is not None:
            found = MONTH.match(raw)
            if found is None or not 1 <= int(found.group(2)) <= 12 or int(found.group(1)) < 2000:
                errors["month"] = ["Use a month as YYYY-MM, such as 2026-11."]
            else:
                start = datetime(int(found.group(1)), int(found.group(2)), 1, tzinfo=ZoneInfo(org.timezone))
        if errors:
            raise FieldErrors(errors)
        month = assistant.month_summary(org, start)
        begin = start or assistant.month_start(org)
        rows = list(
            AssistantUsage.objects.filter(
                organization=org, created_at__gte=begin, created_at__lt=assistant.next_month(begin)
            ).order_by("-created_at", "-id")[: USAGE_SHOWN + 1]
        )
        return Response(
            {
                "month": month,
                "results": AssistantUsageRowSerializer(rows[:USAGE_SHOWN], many=True).data,
                "truncated": len(rows) > USAGE_SHOWN,
            }
        )


class AssistantBatchesView(RoleAPIView):
    role_policy = policy(VIEWER, EDITOR, capability=ASSISTANT)

    @extend_schema(
        summary="The assistant's recent batches, newest first",
        description=f"The last {BATCHES_SHOWN}: what each looked at and suggested, and why one stopped early "
        "(stop_reason cap_reached is “cap reached”).",
        responses={200: AssistantBatchSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        # A batch whose job is gone is failed by the worker's minute sweep (discovery.assistant), not on this read: the
        # queue polls every 15 s, and a read never writes.
        rows = AssistantBatch.objects.filter(organization=self.organization).select_related(
            "connector", "requested_by"
        )[:BATCHES_SHOWN]
        return Response(AssistantBatchSerializer(rows, many=True).data)

    @extend_schema(
        summary="Ask the assistant for suggestions now (Editor, Admin)",
        description="Queues a batch over the undecided candidates (one connector's, or all) as its own job, whose "
        "payload is the batch's ID only. Candidates whose fingerprint is unchanged since their last suggestion are "
        "not sent again. 202 with the batch and a Location header. 409 when the assistant is off, has no key or no "
        "priced model, or a batch is already queued or running.",
        request=AssistantBatchRequestSerializer,
        responses={202: AssistantBatchSerializer, **WRITE_ERRORS},
    )
    def post(self, request):
        batch = assistant.request_batch(self.organization, request.user, self.payload())
        batch = AssistantBatch.objects.select_related("connector", "requested_by").get(pk=batch.pk)
        response = Response(AssistantBatchSerializer(batch).data, status=status.HTTP_202_ACCEPTED)
        response["Location"] = request.build_absolute_uri(f"batches/{batch.id}")
        return response


class AssistantBatchView(RoleAPIView):
    role_policy = policy(VIEWER, EDITOR, capability=ASSISTANT)

    @extend_schema(
        summary="One assistant batch",
        responses={200: AssistantBatchSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        batch = get_object_or_404(
            AssistantBatch.objects.filter(organization=self.organization).select_related("connector", "requested_by"),
            pk=pk,
        )
        return Response(AssistantBatchSerializer(batch).data)
