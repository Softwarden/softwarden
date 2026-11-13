"""The dashboard, the reports and the renewals calendar feed (money/reporting.py, money/calendar_feeds.py).

Roles: every member (Viewer and up) reads the dashboard and the reports and exports them (exports are audited as
`export`, counts and filters only); every member manages their own calendar feed, from a signed-in session only (an
API token is refused, as for API tokens: a script shouldn't mint subscription secrets). The feed itself is public:
the secret in its query string is its only authentication, every refusal is the same 404, and it is rate limited per
feed and per client address."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import exceptions, renderers, status
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import ApiToken
from catalog import services
from catalog.viewsets import csv_response
from core import capabilities as caps
from core.dates import org_today
from core.drf import (
    CSV_FILE,
    PROBLEMS,
    VIEWER,
    ProblemSerializer,
    RoleAPIView,
    policy,
    public,
    refuse_unknown_params,
    set_location,
    timestamp,
)
from core.input import whole_number
from core.problems import FieldErrors, OrganizationSuspended
from core.throttling import SharedRateThrottle
from imports import csvio

from . import calendar_feeds, ics, licences, reporting
from .models import CalendarFeed
from .report_serializers import (
    CalendarFeedCreatedSerializer,
    CalendarFeedSerializer,
    CalendarReportSerializer,
    DashboardSerializer,
    SpendReportSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403)}
CSV = {**CSV_FILE, **ERRORS}
FEED_PATH = "/api/v1/calendar/renewals.ics"
SESSION_ONLY = "Manage your calendar feed from a signed-in session, not with a token."
MONTHS_HELP = (
    f"1 to {reporting.CALENDAR_MONTHS_MAX}; default {reporting.CALENDAR_MONTHS}: this month and the next ones."
)


def months_param(raw: str | None) -> int:
    if raw in (None, ""):
        return reporting.CALENDAR_MONTHS
    months = whole_number(raw, 1, reporting.CALENDAR_MONTHS_MAX)
    if months is None:
        raise FieldErrors({"months": [f"Choose a number of months from 1 to {reporting.CALENDAR_MONTHS_MAX}."]})
    return months


def by_param(raw: str | None) -> str:
    by = raw or "vendor"
    if by not in reporting.DIMENSIONS:
        raise FieldErrors({"by": ["Choose vendor, department, category or cost_center."]})
    return by


def audit_export(org, user, kind: str, rows: int, filters: dict[str, Any]) -> None:
    services.audit(org, user, "export", None, {"kind": kind, "rows": rows, "filters": filters})


def _name(org, what: str) -> str:
    return f"softwarden-{what}-{org_today(org).isoformat()}.csv"


# ---- dashboard ------------------------------------------------------------------------------------------------------


class DashboardView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Dashboard",
        description="Every block of the dashboard in one response: the Needs attention summary (badge counts and "
        "each queue's size), action deadlines due within 30, 60 and 90 days (the dates reminders use), expiring "
        "credentials (API tokens: an Admin sees the organization's, others their own), spend by vendor, department "
        "and category per currency (the largest 8 groups, every named gap, and one 'Everything else' row), license utilization and the "
        "data-quality summary. A block the edition lacks is null (contracts off: no spend or license block, and the "
        "deadlines leave contracts out). Any role.",
        responses={200: DashboardSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        org = self.organization
        return Response(reporting.dashboard(org, self.membership, org_today(org)))


# ---- spend ----------------------------------------------------------------------------------------------------------

BY = OpenApiParameter(
    "by", str, enum=list(reporting.DIMENSIONS), description="Group by; default vendor. See the description."
)
SPEND_HELP = (
    "Annualized cost of live contracts (Active, Pending renewal; not archived) split over their lines to the cent, "
    "grouped per currency (never converted). Every dimension's groups add up to `totals[].annualized` for that "
    "currency. vendor: the contract's; department and category: the line's record's (category for applications only); "
    "cost_center: the line's record's cost center, else the contract's. Cost not on a record is the `unallocated` gap "
    "(by cost center it takes the contract's). `allocations` is the detail: one row per contract line, plus a "
    "contract's unallocated remainder."
)


class SpendReportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=caps.CONTRACTS)

    @extend_schema(
        summary="Spend report",
        description=SPEND_HELP,
        parameters=[BY],
        responses={200: SpendReportSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request, ("by",))
        return Response(reporting.spend_report(self.organization, by_param(request.query_params.get("by"))))


SPEND_COLUMNS = [
    "group",
    "gap",
    "currency",
    "amount",
    "percent_of_contract",
    "contract",
    "contract_id",
    "vendor",
    "record",
    "record_kind",
    "record_id",
    "cost_center",
]


class SpendExportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=caps.CONTRACTS)

    @extend_schema(
        summary="Spend report as CSV",
        description="The allocation detail, one row per contract line (and unallocated remainder) with its group. "
        "Cells are escaped against spreadsheet formulas. Audited as an export.",
        parameters=[BY],
        responses=CSV,
    )
    def get(self, request):
        refuse_unknown_params(request, ("by",))
        org = self.organization
        by = by_param(request.query_params.get("by"))
        rows = reporting.spend_report(org, by)["allocations"]
        data = csvio.write_csv(
            SPEND_COLUMNS,
            (
                {
                    "group": r["label"],
                    "gap": r["gap"] or "",
                    "currency": r["currency"],
                    "amount": r["amount"],
                    "percent_of_contract": r["percent"],
                    "contract": r["contract"]["name"],
                    "contract_id": r["contract"]["id"],
                    "vendor": r["vendor"]["name"] if r["vendor"] else "",
                    "record": r["record"]["name"] if r["record"] else "",
                    "record_kind": r["record"]["kind"] if r["record"] else "",
                    "record_id": r["record"]["id"] if r["record"] else "",
                    "cost_center": r["cost_center"],
                }
                for r in rows
            ),
        )
        audit_export(org, request.user, "spend_allocation", len(rows), {"by": by})
        return csv_response(data, _name(org, f"spend-by-{by.replace('_', '-')}"))


# ---- renewals calendar ----------------------------------------------------------------------------------------------

MONTHS = OpenApiParameter("months", int, description=MONTHS_HELP)
CALENDAR_HELP = (
    "Every open action deadline to the end of the window, overdue ones included, soonest first, from the same "
    "dates reminders use: a renewal's open cycle (with the contract that drives it, if any), and contracts that "
    "drive no renewal (their cancellation deadline). Renewals need the renewals capability, contracts the contracts "
    "capability."
)


class CalendarReportView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Renewals calendar",
        description=CALENDAR_HELP,
        parameters=[MONTHS],
        responses={200: CalendarReportSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request, ("months",))
        org = self.organization
        months = months_param(request.query_params.get("months"))
        return Response(reporting.calendar(org, org_today(org), months))


CALENDAR_COLUMNS = [
    "date",
    "days",
    "overdue",
    "action",
    "label",
    "name",
    "source",
    "vendor",
    "term_ends",
    "auto_renew",
    "decision",
    "contract",
    "annualized_cost",
    "currency",
    "owner",
    "id",
]


class CalendarExportView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Renewals calendar as CSV",
        description=CALENDAR_HELP + " Cells are escaped against spreadsheet formulas. Audited as an export.",
        parameters=[MONTHS],
        responses=CSV,
    )
    def get(self, request):
        refuse_unknown_params(request, ("months",))
        org = self.organization
        months = months_param(request.query_params.get("months"))
        rows = reporting.calendar(org, org_today(org), months)["rows"]
        data = csvio.write_csv(
            CALENDAR_COLUMNS,
            (
                {
                    "date": r["date"],
                    "days": r["timing"]["days"],
                    "overdue": "yes" if r["timing"]["overdue"] else "no",
                    "action": r["action"],
                    "label": r["label_text"] or "",
                    "name": r["name"],
                    "source": r["source"],
                    "vendor": r["vendor"] or "",
                    "term_ends": r["term_ends"] or "",
                    "auto_renew": r["auto_renew"],
                    "decision": r["decision"] or "",
                    "contract": r["contract"]["name"] if r["contract"] else "",
                    "annualized_cost": r["annualized_cost"] or "",
                    "currency": r["currency"] or "",
                    "owner": r["owner"]["name"] if r["owner"] else "",
                    "id": r["id"],
                }
                for r in rows
            ),
        )
        audit_export(org, request.user, "renewal_deadline", len(rows), {"months": months})
        return csv_response(data, _name(org, "renewals-calendar"))


# ---- licence utilisation --------------------------------------------------------------------------------------------

LICENSE_COLUMNS = [
    "application",
    "application_id",
    "archived",
    "sku_name",
    "sku_id",
    "pools",
    "seats_purchased",
    "seats_assigned",
    "seats_available",
    "utilization",
    "overused",
]


class LicenseExportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=caps.CONTRACTS)

    @extend_schema(
        summary="License utilization as CSV",
        description="GET /license-pools/totals as rows, with a final row for the organization. Cells are escaped "
        "against spreadsheet formulas. Audited as an export.",
        responses=CSV,
    )
    def get(self, request):
        refuse_unknown_params(request)
        org = self.organization
        found = licences.totals(org)
        rows = [
            {
                "application": r["application"]["name"],
                "application_id": r["application"]["id"],
                "archived": "yes" if r["application"]["archived"] else "no",
                **{k: r[k] for k in ("sku_name", "sku_id", "pools", "seats_purchased", "seats_assigned")},
                "seats_available": r["seats_available"],
                "utilization": "" if r["utilization"] is None else r["utilization"],
                "overused": "yes" if r["overused"] else "no",
            }
            for r in found["rows"]
        ]
        t = found["total"]
        rows.append(
            {
                "application": "All applications",
                **{k: t[k] for k in ("pools", "seats_purchased", "seats_assigned", "seats_available")},
                "utilization": "" if t["utilization"] is None else t["utilization"],
                "overused": "yes" if t["overused"] else "no",
            }
        )
        data = csvio.write_csv(LICENSE_COLUMNS, rows)
        audit_export(org, request.user, "license_total", len(found["rows"]), {})
        return csv_response(data, _name(org, "licence-utilisation"))


# ---- calendar feed: your own subscription ---------------------------------------------------------------------------


def feed_json(feed: CalendarFeed | None) -> dict[str, Any]:
    if feed is None:
        return {"active": False, "id": None, "prefix": None, "created_at": None, "last_used_at": None}
    return {
        "active": feed.revoked_at is None,
        "id": str(feed.id),
        "prefix": feed.prefix,
        "created_at": timestamp(feed.created_at),
        "last_used_at": timestamp(feed.last_used_at),
    }


def feed_url(request, secret: str) -> dict[str, str]:
    # https only (behind Caddy): no webcal:// link, which calendar apps fetch over plain http first, sending the secret
    # in the clear before any redirect.
    return {"url": request.build_absolute_uri(FEED_PATH) + "?token=" + secret}


class SessionOnly(RoleAPIView):
    def check_permissions(self, request):
        super().check_permissions(request)
        # A method the route doesn't have answers 405 first, as on every route (core.drf.RolePermission).
        if isinstance(request.auth, ApiToken) and getattr(self, request.method.lower(), None) is not None:
            raise exceptions.PermissionDenied(SESSION_ONLY)


FEED_DESCRIPTION = (
    "Your own subscription to the renewals calendar (an ICS feed of the deadlines in GET /reports/renewals-calendar, "
    "for Outlook, Google or Apple Calendar). The address holds a secret and is shown once, when it is created or "
    "rotated; only a hash is kept. It reads as you, in this organization, and stops working when you revoke or rotate "
    "it or leave the organization. Any role, signed-in session only."
)


class CalendarFeedView(SessionOnly):
    role_policy = policy(VIEWER, VIEWER)

    @extend_schema(
        summary="Your calendar feed",
        description=FEED_DESCRIPTION + " active is false when you have none.",
        responses={200: CalendarFeedSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        org = self.organization
        return Response(feed_json(calendar_feeds.live_feed(org, request.user)))

    @extend_schema(
        summary="Create your calendar feed",
        description=FEED_DESCRIPTION + " Refused (409) while you have one: rotate it instead.",
        request=None,
        responses={201: CalendarFeedCreatedSerializer, **ERRORS, 409: PROBLEMS[409]},
    )
    def post(self, request):
        org = self.organization
        feed, secret = calendar_feeds.create_feed(org, request.user)
        response = Response({**feed_json(feed), **feed_url(request, secret)}, status=status.HTTP_201_CREATED)
        set_location(request, response, request.path_info)  # the feed is read and deleted here
        return response

    @extend_schema(
        summary="Revoke your calendar feed",
        description="The address stops working at once. Revoking when you have none changes nothing.",
        request=None,
        responses={200: CalendarFeedSerializer, **ERRORS},
    )
    def delete(self, request):
        org = self.organization
        feed = calendar_feeds.live_feed(org, request.user)
        if feed is not None:
            calendar_feeds.revoke_feed(org, request.user, feed)
        return Response(feed_json(None))


class CalendarFeedRotateView(SessionOnly):
    role_policy = policy(VIEWER, VIEWER)

    @extend_schema(
        summary="Rotate your calendar feed",
        description="A new address; the old one stops working at once. Calendars subscribed to the old address must "
        "subscribe again. 404 when you have no feed.",
        request=None,
        responses={200: CalendarFeedCreatedSerializer, **ERRORS, 404: PROBLEMS[404], 409: PROBLEMS[409]},
    )
    def post(self, request):
        org = self.organization
        with transaction.atomic():
            feed = calendar_feeds.live_feed(org, request.user)
            if feed is None:
                raise exceptions.NotFound("You have no calendar feed. Create one first.")
            new, secret = calendar_feeds.rotate_feed(org, request.user, feed)
            return Response({**feed_json(new), **feed_url(request, secret)})


# ---- calendar feed: the public ICS ----------------------------------------------------------------------------------


class FeedThrottle(SharedRateThrottle):
    """Per feed (keyed by the presented secret's hash, whether or not it is a feed): calendar apps poll every few
    hours, so the default leaves room for several subscribed calendars and manual refreshes."""

    scope = "calendar_feed"

    def get_rate(self):
        return settings.CALENDAR_FEED_RATE or None

    def allow_request(self, request, view):
        return True if self.rate is None else super().allow_request(request, view)

    def get_cache_key(self, request, view):
        secret = str(request.query_params.get("token", ""))
        return self.cache_format % {"scope": self.scope, "ident": calendar_feeds.hash_secret(secret)[:32]}


class FeedAddressThrottle(FeedThrottle):
    """Per client address, over every feed: Outlook.com and Google fetch from shared addresses, so this is generous;
    it caps guessing and abuse from one address."""

    scope = "calendar_feed_address"

    def get_rate(self):
        return settings.CALENDAR_FEED_ADDRESS_RATE or None

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class JsonProblems(BaseContentNegotiation):
    """The feed's errors (404, 429) are problem JSON whatever the client accepts: calendar apps send
    `Accept: text/calendar`, which no renderer has (the 200 is the ICS bytes, returned as they are)."""

    def select_parser(self, request, parsers):
        return parsers[0] if parsers else None

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


SUMMARY = reporting.NEUTRAL_LABELS  # an event's title: the action's words, as a stale row's label


def feed_events(request, rows: list[dict[str, Any]]) -> list[ics.Event]:
    now = datetime.now(UTC).replace(microsecond=0)
    events = []
    for r in rows:
        link = request.build_absolute_uri(r["path"])
        parts = [r["explanation"]] if r["explanation"] else []
        if r["vendor"]:
            parts.append(f"Vendor: {r['vendor']}")
        if r["term_ends"]:
            parts.append(f"{'Term ends' if r['source'] == 'contract' else 'Renewal date'}: {r['term_ends']}")
        if r["contract"] and r["source"] == "renewal":
            parts.append(f"Contract: {r['contract']['name']}")
        if r["decision"]:
            parts.append(f"Decision recorded: {r['decision'].capitalize()}, not completed")
        parts.append(f"Open in Softwarden: {link}")
        label = f" ({r['label_text'].lower()})" if r["label"] == "assumed" else ""
        events.append(
            ics.Event(
                uid=r["uid"],
                day=datetime.fromisoformat(r["date"]).date(),
                summary=f"{SUMMARY[r['action']]}{label}: {r['name']}",
                description="\n".join(parts),
                stamp=now,
                url=link,
            )
        )
    return events


NOT_FOUND = "This calendar feed doesn't exist."


@public
class CalendarFeedIcsView(APIView):
    authentication_classes: list[Any] = []
    renderer_classes = [renderers.JSONRenderer]
    content_negotiation_class = JsonProblems
    throttle_classes = [FeedAddressThrottle, FeedThrottle]

    @extend_schema(
        summary="Renewals calendar feed (ICS, public)",
        description="An iCalendar (RFC 5545) feed of the renewals calendar for its owner's organization, one all-day "
        "event per action deadline with a stable UID. Authenticated only by the secret in `token` (from POST "
        "/calendar-feed); anything else, including a revoked feed, is 404. Rate limited per feed and per client "
        "address (429 with Retry-After).",
        auth=[],
        parameters=[OpenApiParameter("token", str, required=True, description="The feed's secret."), MONTHS],
        responses={
            (200, "text/calendar"): OpenApiResponse(OpenApiTypes.BINARY, description="text/calendar; charset=utf-8"),
            404: PROBLEMS[404],
            429: OpenApiResponse(ProblemSerializer, description="Too many requests"),
        },
    )
    def get(self, request):
        found = calendar_feeds.authenticate(request.query_params.get("token"))
        if found is None:
            raise exceptions.NotFound(NOT_FOUND)
        _feed, membership = found
        org = membership.organization
        if org.suspended_at is not None:  # suspended by the service operator
            raise OrganizationSuspended()
        raw = request.query_params.get("months")
        months = reporting.CALENDAR_MONTHS
        max_months = reporting.CALENDAR_MONTHS_MAX
        if raw not in (None, "") and (wanted := whole_number(raw, 1, max_months)) is not None:
            months = wanted  # a bad value falls back to the default: a calendar app can't show a field error
        rows = reporting.calendar(org, org_today(org), months)["rows"]
        body = ics.calendar(f"Softwarden renewals ({org.name})", feed_events(request, rows))
        response = HttpResponse(body, content_type=ics.CONTENT_TYPE)
        response["Content-Disposition"] = 'inline; filename="softwarden-renewals.ics"'
        response["Cache-Control"] = "private, no-cache"
        response["X-Robots-Tag"] = "noindex"
        return response
