"""Offboarding and access review routes, included from api/urls.py under /api/v1/.

GET  people/{id}/footprint                 Viewer: where the Person appears (footprint.py)
GET  people/{id}/footprint.csv             Viewer: the same as CSV; audited (`export`), refused while impersonating
POST people/{id}/reassign                  Editor (notification rules: Admin): hand roles to another person
GET  reports/access-review                 Viewer: records with their owners, flagged (access_review.py)
GET  reports/access-review/export.csv      Viewer: the same as CSV; audited (`export`), refused while impersonating

Viewers read the footprint and the report because every owner, team and account manager in them is already on pages
a Viewer reads; the parts a Viewer doesn't read anywhere else (the account, notification rules) are Admin-only
sections. Reassigning is an Editor's, as changing an owner is."""

from typing import Any

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from catalog.models import Person
from catalog.services import audit
from catalog.viewsets import csv_response
from core.dates import org_today
from core.drf import CSV_FILE, EDITOR, PROBLEMS, VIEWER, ProblemSerializer, RoleAPIView, policy, refuse_unknown_params
from imports import csvio

from . import access_review, footprint, reassign
from .serializers import (
    AccessReviewSerializer,
    FootprintSerializer,
    ReassignInputSerializer,
    ReassignResultSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
CSV = {**CSV_FILE, **ERRORS}


def _person(org, pk) -> Person:
    person = Person.objects.select_related("user").filter(organization=org, pk=pk).first()
    if person is None:
        raise NotFound("This person doesn't exist or you don't have access.")
    return person


def _file(org, what: str) -> str:
    return f"softwarden-{what}-{org_today(org).isoformat()}.csv"


# ---- footprint ------------------------------------------------------------------------------------------------------


FOOTPRINT_HELP = (
    "Where the person appears, in sections: records they own (applications and components, AI agents, "
    "integrations), records they are IT steward of, teams, vendors they manage the account of, contracts and renewals "
    "they own or follow up, reviews they confirmed, change assessments, data-quality exceptions, notification rules "
    "(Admins), discovery sources they review, discovered apps whose Entra owners match them (by Entra object ID or "
    "email), licenses assigned to them in Entra (linked by a license usage connector), and their account (Admins: membership, IT coordinator, API tokens, calendar feed, saved views "
    "and subscriptions). AI agents include those whose run-as identity is their email or Entra object ID. A section "
    "the organization lacks the capability for, that is Admin-only, that needs a user account they don't have, or "
    "whose data isn't stored is `available: false` with the `reason`; licenses are until a Microsoft Entra license "
    "usage connector has read the tenant ('Licenses: not read; needs User.Read.All'). Archived rows are included and marked. Another organization's person is a 404."
)


class FootprintView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="A person's footprint", description=FOOTPRINT_HELP, responses={200: FootprintSerializer, **ERRORS}
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        org = self.organization
        return Response(footprint.build(org, _person(org, pk), self.membership.role))


FOOTPRINT_COLUMNS = ["section", "section_label", "type", "id", "name", "roles", "archived", "detail", "path"]


class FootprintExportView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="A person's footprint as CSV",
        description="One row per item, with its section; an unavailable section is one row of type `not_available` "
        "with the reason. Cells are escaped against spreadsheet formulas. Audited as an export; refused while platform "
        "staff impersonate a member.",
        responses=CSV,
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        org = self.organization
        person = _person(org, pk)
        found = footprint.build(org, person, self.membership.role)
        rows: list[dict[str, Any]] = []
        for section in found["sections"]:
            common = {"section": section["key"], "section_label": section["label"]}
            if not section["available"]:
                rows.append({**common, "type": "not_available", "detail": section["reason"]})
                continue
            for i in section["items"]:
                rows.append(
                    {
                        **common,
                        **{k: i[k] or "" for k in ("type", "id", "name", "detail", "path")},
                        "roles": ", ".join(i["roles"]),
                        "archived": "yes" if i["archived"] else "no",
                    }
                )
        data = csvio.write_csv(FOOTPRINT_COLUMNS, rows)
        audit(
            org,
            request.user,
            "export",
            None,
            {"kind": "footprint_row", "rows": len(rows), "filters": {"person": str(person.id)}},  # rows written
        )
        return csv_response(data, _file(org, "footprint"))


# ---- reassign -------------------------------------------------------------------------------------------------------


class ReassignView(RoleAPIView):
    role_policy = policy(EDITOR)

    @extend_schema(
        summary="Reassign a person's roles to someone else (Editors and Admins)",
        description="Moves the chosen `roles` from this person to `to`, each row through its own service (its "
        "single-edit validation and its own audit event, marked `via: offboarding`), in one transaction, and writes "
        "one `person.reassigned` event. Roles: "
        + "; ".join(f"`{k}`: {v.label}" for k, v in reassign.ROLES.items())
        + ". `notification_rules` is Admin-only. `to` must be an active person of this organization, not this one; "
        "IT steward and renewal follow-up also need their account to be an active Admin or Editor here. Archived "
        "records, renewals and contracts are skipped and listed; team memberships aren't moved, and nothing is "
        "deleted. `dry_run: true` runs the same service calls in a savepoint that is rolled back: rows are `planned`, "
        "or `refused` with the errors the real run would give, and nothing is written. If any row's service refuses a "
        "real run, nothing is saved and the 400 problem lists them in `rows`. No `person.reassigned` event when "
        "nothing changed. Every answer carries `plan_hash`; a real run that sends a dry run's `plan_hash` is refused "
        "with 409 /problems/reassign-stale (nothing saved) when the rows it would change differ from that dry run's.",
        request=ReassignInputSerializer,
        responses={
            200: ReassignResultSerializer,
            **ERRORS,
            400: OpenApiResponse(
                ProblemSerializer,
                description="Field errors, or refused rows in `rows` (each `{type, id, name, path, roles, status: "
                "refused, errors, field_errors}`)",
            ),
            409: OpenApiResponse(ProblemSerializer, description="The plan changed since the dry run (reassign-stale)"),
        },
    )
    def post(self, request, pk):
        org = self.organization
        person = _person(org, pk)
        return Response(reassign.reassign(org, request.user, self.membership.role, person, self.payload()))


# ---- access review --------------------------------------------------------------------------------------------------

ACCESS_PARAMS = [
    OpenApiParameter(
        "kind", str, description="Comma-separated: application, component, ai_agent, integration. Default: all."
    ),
    OpenApiParameter("flag", str, description=f"Comma-separated: {', '.join(access_review.FLAGS)}. Rows with any."),
    OpenApiParameter("owner", str, description="A person's ID: records they are business or technical owner of."),
    OpenApiParameter("include_retired", bool, description="Include retired records. Default false."),
]
ACCESS_HELP = (
    "Every record that isn't archived (retired ones with include_retired) with its business and technical owner, by "
    "name. Flags: `inactive_owner` (an owner is a Person marked inactive: the 'Inactive owner' gap of the Ownership "
    "gaps queue), `owner_left` (an owner is still active but their user account was deactivated or removed from the "
    "organization), `no_owner` (neither owner; an integration: no technical owner) and, informational, "
    "`entra_owner_mismatch` (a linked Entra app lists a user owner who isn't one of the record's owners, matched by "
    "Entra object ID or email as discovery does). `counts` are over the records in scope before the flag filter. "
    "From stored data only, as of discovery's last run."
)


class AccessReviewView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Access review",
        description=ACCESS_HELP,
        parameters=ACCESS_PARAMS,
        responses={200: AccessReviewSerializer, **{c: PROBLEMS[c] for c in (400, 403)}},
    )
    def get(self, request):
        org = self.organization
        filters = access_review.parse_filters(request.query_params)
        found = access_review.rows(org, filters)
        return Response({"counts": access_review.counts(found), "results": access_review.flagged(found, filters)})


ACCESS_COLUMNS = [
    "record",
    "record_kind",
    "record_id",
    "lifecycle",
    "business_owner",
    "business_owner_id",
    "business_owner_status",
    "technical_owner",
    "technical_owner_id",
    "technical_owner_status",
    "flags",
    "entra_owners",
    "why",
]


def _owner_cells(prefix: str, owner: dict[str, Any] | None) -> dict[str, str]:
    if owner is None:
        return {prefix: "", f"{prefix}_id": "", f"{prefix}_status": ""}
    return {prefix: owner["name"], f"{prefix}_id": owner["id"], f"{prefix}_status": owner["status"]}


def _entra_cell(owners: list[dict[str, Any]]) -> str:
    return "; ".join(o["name"] or o["email"] or "owner not resolved" for o in owners)


class AccessReviewExportView(RoleAPIView):
    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Access review as CSV",
        description="The report's rows with the same filters. Cells are escaped against spreadsheet formulas. Audited "
        "as an export; refused while platform staff impersonate a member.",
        parameters=ACCESS_PARAMS,
        responses={**CSV_FILE, **{c: PROBLEMS[c] for c in (400, 403)}},
    )
    def get(self, request):
        org = self.organization
        filters = access_review.parse_filters(request.query_params)
        found = access_review.flagged(access_review.rows(org, filters), filters)
        data = csvio.write_csv(
            ACCESS_COLUMNS,
            [
                {
                    "record": r["record"]["name"],
                    "record_kind": r["record"]["kind"],
                    "record_id": r["record"]["id"],
                    "lifecycle": r["record"]["lifecycle"],
                    **_owner_cells("business_owner", r["business_owner"]),
                    **_owner_cells("technical_owner", r["technical_owner"]),
                    "flags": ", ".join(r["flags"]),
                    "entra_owners": _entra_cell(r["entra_owners"]),
                    "why": r["why"],
                }
                for r in found
            ],
        )
        audit(
            org, request.user, "export", None, {"kind": "access_review_record", "rows": len(found), "filters": filters}
        )
        return csv_response(data, _file(org, "access-review"))
