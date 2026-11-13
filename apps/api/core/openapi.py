"""OpenAPI post-processing (SPECTACULAR_SETTINGS["POSTPROCESSING_HOOKS"]).

Views describe their own success bodies and the problems they raise for their own reasons (core/drf.PROBLEMS). This
hook adds what every operation shares, so a generated client sees one error shape everywhere:

- every error response (4xx, 5xx) is served as `application/problem+json` (core/problems.py), so its content is
  declared under that media type rather than application/json;
- every operation that authenticates can answer 401 (an API token that is unknown, revoked or expired), and every
  signed-in one 429 (the per-token or per-user rate limit, core/throttling.py, with Retry-After);
- every operation has a `default` response: the shared Problem schema for anything else (405, 413, 415, 500);
- every operation that documents 412 (it takes If-Match) also documents 428: no If-Match from a signed-in session while
  SOFTWARDEN_REQUIRE_IF_MATCH is on (core/drf.if_match_required).

The Problem schema takes extension members a type documents beyond those it lists (`additionalProperties`). The
session scheme for core/authentication.py is registered here too (core/drf.py imports this module).

A create refused by an organization's limit (core/limits.check_room) answers 409 /problems/limit-reached whose `limit`
is the limit's name, while Problem's `limit` is the too-many-events maximum (a number). So that problem has a schema of
its own, LimitReachedProblem, and the operations in LIMIT_REACHED_OPERATIONS (every route whose service checks a limit)
declare their 409 as ProblemOrLimitReached: one of Problem or LimitReachedProblem. Other operations keep Problem.
tests/test_openapi.py checks every entry is a real operation."""

from __future__ import annotations

from typing import Any

from drf_spectacular.authentication import SessionScheme

from .problems import PROBLEM_CONTENT_TYPE, PROBLEM_TYPES


class CsrfSessionScheme(SessionScheme):
    """core/authentication.SessionAuthentication is DRF's session authentication: the same cookie scheme."""

    target_class = "core.authentication.SessionAuthentication"


PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}
LIMIT_REACHED = "LimitReachedProblem"
PROBLEM_OR_LIMIT = "ProblemOrLimitReached"
# (method, OpenAPI path) of every operation whose service can refuse with /problems/limit-reached (core/limits.py
# check_room callers): record creates, creating a record from a discovery candidate (also by accepting the assistant's
# suggestion), connectors, import commits, catalog-as-code applies, attachment uploads, invitations and their acceptance, approving pending
# access, a new member, reactivating one, a signup's first Admin, an invitation's (re)sent email, and creates
# in the tables the `other_records` limit counts. SCIM answers its own 409 (outside this schema).
LIMIT_REACHED_OPERATIONS = frozenset(
    {
        ("post", "/api/v1/applications"),
        ("post", "/api/v1/components"),
        ("post", "/api/v1/integrations"),
        ("post", "/api/v1/ai-agents"),
        ("post", "/api/v1/discovery/observations/{id}/create"),
        ("post", "/api/v1/discovery/observations/{id}/assistant/accept"),
        ("post", "/api/v1/connectors"),
        ("post", "/api/v1/imports/{id}/commit"),
        ("post", "/api/v1/catalog-code/apply"),
        ("post", "/api/v1/applications/{id}/attachments"),
        ("post", "/api/v1/components/{id}/attachments"),
        ("post", "/api/v1/integrations/{id}/attachments"),
        ("post", "/api/v1/ai-agents/{id}/attachments"),
        ("post", "/api/v1/vendors/{id}/attachments"),
        ("post", "/api/v1/contracts/{id}/attachments"),
        ("post", "/api/v1/users"),
        ("patch", "/api/v1/users/{id}"),
        ("post", "/api/v1/invitations"),
        ("post", "/api/v1/invitations/{id}/resend"),  # the invitation email send budget
        ("post", "/api/v1/pending-access/{id}/approve"),
        ("post", "/api/v1/auth/invitations/accept"),
        ("post", "/api/v1/auth/signup/verify"),
        # The `other_records` limit: creates in the secondary tables it counts.
        ("post", "/api/v1/people"),
        ("post", "/api/v1/teams"),
        ("post", "/api/v1/departments"),
        ("post", "/api/v1/vendors"),
        ("post", "/api/v1/categories"),
        ("post", "/api/v1/relationships"),
        ("post", "/api/v1/relationship-types"),
        ("post", "/api/v1/custom-fields"),
        ("post", "/api/v1/saved-filters"),
        ("post", "/api/v1/ai-providers"),
        ("post", "/api/v1/ai-models"),
        ("post", "/api/v1/contracts"),
        ("patch", "/api/v1/contracts/{id}"),
        ("post", "/api/v1/contracts/{id}/lines"),
        ("post", "/api/v1/contracts/{id}/renew"),
        ("post", "/api/v1/license-pools"),
        ("post", "/api/v1/license-pools/unbound/{observation_pk}/create"),
        ("post", "/api/v1/renewals"),
        ("post", "/api/v1/notification-rules"),
        ("post", "/api/v1/notification-settings/teams-destinations"),
        ("post", "/api/v1/notification-settings/webhook-destinations"),
        ("post", "/api/v1/webhooks"),
        ("post", "/api/v1/api-tokens"),
        ("post", "/api/v1/import-sources"),
    }
)


def _limit_reached_schema(schemas: dict[str, Any]) -> dict[str, Any]:
    # The limit's name: records, connectors, members or attachment_bytes (the shared LimitNameEnum lists every limit).
    name = {"$ref": "#/components/schemas/LimitNameEnum"} if "LimitNameEnum" in schemas else {"type": "string"}
    return {
        "type": "object",
        "description": "409 /problems/limit-reached: a create would take the organization past one of its limits "
        "(core/limits.py). Nothing was written; updates and deletes are never refused this way.",
        "properties": {
            "type": {"type": "string", "description": "/problems/limit-reached"},
            "title": {"type": "string"},
            "status": {"type": "integer", "description": "409"},
            "detail": {"type": "string", "description": "What to do, in words."},
            "limit": name,
            "maximum": {"type": "integer", "description": "The organization's limit now."},
            "usage": {"type": "integer", "description": "The usage counted for this request."},
            "adding": {
                "type": "integer",
                "description": "How many the request would add (bytes for attachment_bytes).",
            },
        },
        "required": ["type", "title", "status", "detail", "limit", "maximum", "usage", "adding"],
    }


def _limit_responses(result: dict[str, Any]) -> None:
    """LimitReachedProblem and ProblemOrLimitReached, and the 409 of each LIMIT_REACHED_OPERATIONS operation."""
    schemas = result.setdefault("components", {}).setdefault("schemas", {})
    schemas[LIMIT_REACHED] = _limit_reached_schema(schemas)
    schemas[PROBLEM_OR_LIMIT] = {
        "oneOf": [PROBLEM_REF, {"$ref": f"#/components/schemas/{LIMIT_REACHED}"}],
        "description": "Problem details, or LimitReachedProblem when `type` is /problems/limit-reached.",
    }
    paths = result.get("paths", {})
    for method, path in sorted(LIMIT_REACHED_OPERATIONS):
        operation = paths.get(path, {}).get(method)
        if operation is None:
            continue  # tests/test_openapi.py fails on a stale entry
        responses = operation.setdefault("responses", {})
        before = (responses.get("409") or {}).get("description") or "Conflict"
        responses["409"] = {
            "description": f"{before}; or a limit reached (type /problems/limit-reached, LimitReachedProblem)"
            if "limit-reached" not in before
            else before,
            "content": {PROBLEM_CONTENT_TYPE: {"schema": {"$ref": f"#/components/schemas/{PROBLEM_OR_LIMIT}"}}},
        }


METHODS = ("get", "put", "post", "patch", "delete", "head", "options", "trace")
RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before the bucket has room again.",
        "schema": {"type": "integer", "minimum": 1},
    }
}


def _problem(description: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"description": description, "content": {PROBLEM_CONTENT_TYPE: {"schema": PROBLEM_REF}}}
    if headers:
        response["headers"] = headers
    return response


def _signed_in(operation: dict[str, Any]) -> bool:
    """drf-spectacular lists the security schemes of an operation that needs an account; public ones have none."""
    return any(requirement for requirement in operation.get("security") or [])


def _unauthenticated_paths() -> set[str]:
    """OpenAPI paths of the DRF views that authenticate nobody (`authentication_classes = []`)."""
    import re

    from django.urls import URLPattern, URLResolver, get_resolver

    found: set[str] = set()

    def walk(patterns, prefix=""):
        for pattern in patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                cls = getattr(pattern.callback, "cls", None)
                if cls is not None and list(getattr(cls, "authentication_classes", None) or []) == []:
                    found.add("/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", prefix + str(pattern.pattern)))

    walk(get_resolver().url_patterns)
    return found


# The reads that answer whatever query parameters come with them, and why. Every other GET refuses a parameter
# it doesn't take with a 400 naming it; tests/test_unknown_params.py walks every GET route to hold both.
ACCEPTS_ANY_PARAMS = {
    # The identity provider's redirect back carries whatever the provider adds (state, session_state, iss, ...).
    "/api/v1/auth/oidc/callback": "the identity provider decides the parameters of its redirect",
    # Sign-in links are pasted into mail and chat, which add tracking parameters; a refusal would be a dead link.
    "/api/v1/auth/oidc/start": "a browser redirect from a sign-in link; takes org, email and next",
    # Calendar apps poll the subscription address and can't show a 400 (a bad months falls back to the default too).
    "/api/v1/calendar/renewals.ics": "a calendar subscription; a calendar app can't show a field error",
    # Public reachability check called by external uptime monitors, which add cache-busting parameters.
    "/api/v1/health": "an uptime check; takes no input and answers the same whatever is sent",
}


def problem_responses(result, generator, request, public):
    unauthenticated = _unauthenticated_paths()
    for path, operations in result.get("paths", {}).items():
        for method, operation in operations.items():
            if method not in METHODS:
                continue
            responses = operation.setdefault("responses", {})
            for code, response in responses.items():
                content = response.get("content") or {}
                if code[:1] in ("4", "5") and not content:  # described in words only: it is a problem too
                    response["content"] = {PROBLEM_CONTENT_TYPE: {"schema": PROBLEM_REF}}
                elif code[:1] in ("4", "5") and "application/json" in content:
                    response["content"] = {
                        (PROBLEM_CONTENT_TYPE if media == "application/json" else media): body
                        for media, body in content.items()
                    }
            # A Bearer header with an unknown, revoked or expired token is refused on every route that authenticates,
            # public ones too (authentication runs before the role check); setup and the calendar feed don't.
            if path not in unauthenticated:
                responses.setdefault(
                    "401", _problem("The API token is unknown, revoked or expired (type /problems/invalid-token)")
                )
            if _signed_in(operation):
                responses.setdefault(
                    "429",
                    _problem("Rate limited per API token or per user (type /problems/rate-limited)", RETRY_AFTER),
                )
            if "429" in responses:
                responses["429"].setdefault("headers", RETRY_AFTER)
            # Every write that takes If-Match (it documents 412) can answer 428 when the installation requires it.
            if "412" in responses:
                responses.setdefault(
                    "428",
                    _problem(
                        "No If-Match from a signed-in session while SOFTWARDEN_REQUIRE_IF_MATCH is on (type "
                        "/problems/precondition-required). Nothing was changed."
                    ),
                )
            # Every read refuses a parameter it doesn't take (core.drf.refuse_unknown_params), except those below.
            if method == "get" and path not in ACCEPTS_ANY_PARAMS:
                responses.setdefault("400", _problem("A query parameter this read doesn't take, or a bad value"))
            responses.setdefault("default", _problem("Any other error, as problem details"))
    _limit_responses(result)
    problem = result.get("components", {}).get("schemas", {}).get("Problem")
    if problem is not None:
        problem["additionalProperties"] = True  # extension members (RFC 9457 §3.2) a type documents
        problem["required"] = ["type", "title", "status", "detail"]
        problem["properties"]["type"]["description"] = "What went wrong, one of: " + ", ".join(
            f"{t} ({status})" for t, (status, _) in PROBLEM_TYPES.items()
        )
    return result
