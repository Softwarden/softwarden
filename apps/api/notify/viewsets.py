"""Needs attention and notifications: the queues page, your digest preference, snoozing and
acknowledging items, and the Admin delivery log with Retry now. Routes are in notify/urls.py with the same URLs and
response shapes as the function views they replaced.

Roles: anyone signed in reads Needs attention and sets their own preference; Editors and Admins snooze (anyone may
undo their own); the delivery log and retry are Admin-only. Writes go through notify/services.py and
notify/digest.py."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django import forms
from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import exceptions
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.models import RECORD_KINDS, Resource
from core.dates import org_today
from core.drf import (
    ADMIN,
    EDITOR,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    policy,
    refuse_unknown_params,
)

from . import digest
from . import services as notify_services
from .attention import SCOPES, attention_queues
from .models import Digest, DigestStatus, NotificationPreference, NotificationSnooze
from .serializers import (
    AttentionSerializer,
    DigestSerializer,
    PreferenceInputSerializer,
    PreferenceSerializer,
    SnoozeInputSerializer,
    SnoozeSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}


# ---- Needs attention ---------------------------------------------------------------------------------------------


class NeedsAttentionView(RoleAPIView):
    """Every queue is computed for the whole organization and then cut by `scope`: mine (my own items, plus the
    unrouted ones when I am the coordinator), all, or unassigned (items with no steward or owner of their own,
    whether or not the coordinator catches them). `counts` carries the item total for each scope so the screen and
    the navigation badge never disagree."""

    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Needs attention queues",
        parameters=[OpenApiParameter("scope", str, enum=list(SCOPES), description="Default mine.")],
        responses={200: AttentionSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request, ("scope",))
        org = self.organization
        scope = request.query_params.get("scope", "mine")
        if scope not in SCOPES:
            raise exceptions.ValidationError({"scope": ["Use mine, all or unassigned."]})
        today = org_today(org)
        queues, counts = attention_queues(org, request.user, today)
        # This user's snoozes and acknowledgments: shown on the row; they never hide it.
        mine = digest.active_snoozes(org, today, request.user).get(request.user.id, {})
        for name, rows in queues.items():
            for row in rows:
                s = mine.get(digest.subject_of(name, row))
                quiet = s is not None and digest.is_quiet(s, row, org, today)
                row["snooze"] = (
                    {"id": str(s.id), "kind": s.kind, "until": s.until and s.until.isoformat()} if s and quiet else None
                )

        def cut(rows):
            return [{k: v for k, v in r.items() if k != "_flags"} for r in rows if scope == "all" or r["_flags"][scope]]

        # records = 0 means a new install: the screen invites adding the first record instead of showing all-clear.
        records = Resource.objects.filter(organization=org, kind__in=RECORD_KINDS, archived_at__isnull=True).count()
        return Response(
            {"scope": scope, "records": records, "counts": counts, **{name: cut(rows) for name, rows in queues.items()}}
        )


# ---- your digest preference ----------------------------------------------------------------------------------------


def next_digest_at(org, now=None):
    """When the next digest run starts, in the organization's timezone."""
    zone = ZoneInfo(org.timezone)
    local = (now or datetime.now(zone)).astimezone(zone)
    at = datetime.combine(local.date(), org.digest_time, tzinfo=zone)
    return at if at > local else at + timedelta(days=1)


def preference_json(membership):
    org = membership.organization
    pref = NotificationPreference.objects.filter(membership=membership).first()
    return {
        "digest_enabled": pref.digest_enabled if pref else True,
        "reminders_enabled": pref.reminders_enabled if pref else True,
        "subscriptions_enabled": pref.subscriptions_enabled if pref else True,
        "digest_time": org.digest_time.strftime("%H:%M"),
        "timezone": org.timezone,
        "next_digest_at": next_digest_at(org).isoformat(),
    }


class PreferencesView(RoleAPIView):
    """The signed-in user's own notification preferences in the current organization (the daily digest; reminder
    emails from notification rules); nobody changes another user's, so every member may write them."""

    role_policy = policy(VIEWER, VIEWER)

    @extend_schema(summary="Your digest preference", responses={200: PreferenceSerializer, **ERRORS})
    def get(self, request):
        refuse_unknown_params(request)
        return Response(preference_json(self.membership))

    @extend_schema(
        summary="Turn your daily digest or reminder emails on or off",
        request=PreferenceInputSerializer,
        responses={200: PreferenceSerializer, **ERRORS},
    )
    def patch(self, request):
        notify_services.set_preferences(self.membership, request.user, self.payload())
        return Response(preference_json(self.membership))


# ---- snoozes ---------------------------------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(summary="Your snoozes and acknowledgments"),
    retrieve=extend_schema(summary="Get one of your snoozes or acknowledgments"),
    create=extend_schema(
        summary="Snooze or acknowledge a Needs attention item for your notifications (Editor, Admin)",
        description="Affects your digest only. The item, its deadline and the queue stay as they are.",
    ),
    destroy=extend_schema(summary="Undo your snooze or acknowledgment"),
)
class SnoozeViewSet(TenantViewSet):
    """Your own snoozes: someone else's doesn't exist for you. Undo is open to any role, since a snooze only ever
    changes its owner's notifications."""

    role_policy = policy(VIEWER, EDITOR, destroy=VIEWER)
    pagination_class = None
    serializer_class = SnoozeSerializer
    input_serializer_class = SnoozeInputSerializer
    ordering_fields = ("created_at", "until")
    default_ordering = ("-created_at",)

    def base_queryset(self):
        return NotificationSnooze.objects.all()

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(user=self.request.user)

    def create_object(self, data):
        return notify_services.snooze(self.organization, self.request.user, data)

    def destroy_object(self, obj):
        notify_services.unsnooze(self.organization, self.request.user, obj)


# ---- the digest delivery log (Admin) -------------------------------------------------------------------------------


def _for_date(value: str):
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


def _status(value: str):
    if value == "":
        return None
    if value not in DigestStatus.values:
        raise forms.ValidationError("Choose pending, sent, failed or skipped.")
    return value


class DigestFilterSet(TenantFilterSet):
    status = ParamFilter(
        parse=_status,
        method="filter_status",
        choices=DigestStatus.choices,
        help_text="From the delivery: pending is queued or sending.",
    )
    for_date = ParamFilter(parse=_for_date, field_name="for_date", help_text="YYYY-MM-DD")

    class Meta:
        model = Digest
        fields: list[str] = []

    def filter_status(self, qs, name, value):
        if not value:
            return qs
        statuses = digest.DELIVERY_STATUSES[value]
        if value == DigestStatus.FAILED:  # a digest whose delivery is gone reads failed (digest.status_of)
            return qs.filter(Q(delivery__status__in=statuses) | Q(delivery__isnull=True))
        return qs.filter(delivery__status__in=statuses)


DIGEST_LOG_MAX = 200


@extend_schema_view(
    list=extend_schema(
        summary="Digest delivery log (Admin): newest first, at most 200 rows",
        description="Each digest's status, attempts and error come from its delivery (kind digest), also listed in "
        "the notification log.",
    ),
    retrieve=extend_schema(summary="Get one digest's delivery (Admin)"),
    retry=extend_schema(
        summary="Send a digest again now (Admin)",
        description="A pending digest is attempted now; a failed or skipped one is resent as a new delivery "
        "(resend_of the old), which becomes the digest's delivery. 409 while it is being sent or when email isn't "
        "configured.",
        request=None,
        responses={200: DigestSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
    ),
)
class DigestViewSet(TenantViewSet):
    role_policy = policy(ADMIN)
    pagination_class = None
    serializer_class = DigestSerializer
    filterset_class = DigestFilterSet
    ordering_fields = ("for_date",)
    default_ordering = ("-for_date", "user__email")

    list_limit = DIGEST_LOG_MAX

    def base_queryset(self):
        return Digest.objects.select_related("user", "delivery")

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        sent = digest.retry(self.organization, request.user, self.get_object())
        return self.detail_response(sent)
