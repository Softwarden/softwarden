"""Hostile-input guards shared by every API request.

Input that no field could ever store or compute with is refused once, centrally, as a 400 problem naming the field or
parameter, instead of reaching PostgreSQL or `int()` and answering 500:
- request bodies (core/parsers.py): a NUL character (PostgreSQL text and jsonb can't hold one), an unpaired UTF-16
  surrogate (not encodable as UTF-8, so not storable either) or nesting deeper than MAX_DEPTH;
- query parameters and the path under /api/ (QueryGuardMiddleware): a NUL character;
- whole numbers from text (`whole_number`): ASCII decimal digits only, at most MAX_DIGITS of them, so "²" (which
  passes str.isdigit()) or 5,000 digits (past Python's int() limit and any database key) are a field error.

The messages never echo the value: a refused field may be a secret.
"""

from __future__ import annotations

import re
from typing import Any

from core.problems import INVALID_REQUEST, problem_response

MAX_DEPTH = 32  # nesting of objects and arrays in a request body; real bodies nest a few levels at most
MAX_DIGITS = 18  # every 18-digit decimal fits a signed 64-bit integer
BIGINT_MAX = 2**63 - 1

NUL_MESSAGE = "Remove the NUL character (\\u0000); text can't contain it."
SURROGATE_MESSAGE = "Remove the unpaired UTF-16 surrogate; text must be valid Unicode."
DEPTH_MESSAGE = f"The request nests objects and arrays more than {MAX_DEPTH} levels deep."
KEY_MESSAGE = "A field name contains a NUL character or an unpaired surrogate."
MAX_ERRORS = 20


def text_problem(value: str) -> str | None:
    """Why `value` can't be stored as text, or None when it can."""
    if "\x00" in value:
        return NUL_MESSAGE
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # after JSON decoding, a surrogate left in the string is an unpaired one
        return SURROGATE_MESSAGE
    return None


def body_errors(data: Any) -> dict[str, list[str]]:
    """{field path: [message]} for every string in a decoded body that can't be stored, and for nesting past
    MAX_DEPTH. Paths are the keys joined with dots, list items as [index] (`lines[2].notes`); a top-level problem
    is `non_field`. Walks iteratively, so the walk itself can't hit the recursion limit."""
    errors: dict[str, list[str]] = {}
    stack: list[tuple[Any, str, int]] = [(data, "", 0)]
    while stack and len(errors) < MAX_ERRORS:
        node, path, depth = stack.pop()
        if isinstance(node, str):
            problem = text_problem(node)
            if problem:
                errors[path or "non_field"] = [problem]
            continue
        if not isinstance(node, dict | list):
            continue
        if depth >= MAX_DEPTH:
            errors[path or "non_field"] = [DEPTH_MESSAGE]
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                key = str(key)
                if text_problem(key):
                    errors[path or "non_field"] = [KEY_MESSAGE]
                    continue
                stack.append((value, f"{path}.{key}" if path else key, depth + 1))
        else:
            for index, value in enumerate(node):
                stack.append((value, f"{path}[{index}]" if path else f"[{index}]", depth + 1))
    return errors


def whole_number(value: object, low: int = 0, high: int = BIGINT_MAX) -> int | None:
    """`value` as an int when it is a whole number from `low` to `high`, else None: a string of ASCII decimal digits
    (at most MAX_DIGITS of them) or a JSON integer (never a boolean). Callers turn None into their own field error."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not (value.isascii() and value.isdecimal()) or len(value) > MAX_DIGITS:
            return None
        value = int(value)
    if not isinstance(value, int) or not low <= value <= high:
        return None
    return value


TEXT_MESSAGE = "Send text."  # as the vendor, AI and custom-field services say


def text_value(value: object, field: str, errors: dict[str, list[str]]) -> str:
    """A text field from a JSON body, stripped: null is empty (it clears an optional field); a number, boolean, list
    or object records TEXT_MESSAGE for `field` and gives "", so a wrong type is a 400 that writes
    nothing instead of a 500 or a silently cleared field."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    errors[field] = [TEXT_MESSAGE]
    return ""


# Names that go into mail to someone else (signup's confirmation, an invitation) must not carry a link:
# a scheme with slashes (`https://`, `ftp://`), `www.`, or a dotted host name ending in two or more letters (`evil.example`, `pat@evil.co`), which
# mail clients turn into links. "Contoso Ltd.", "A.B. Holdings" and "Fabrikam 2.0" pass; "Node.js" or "J.Smith" are
# refused too (written "Node js" they pass): a name has no need for a domain. The one rule for every name field that
# ends up in an email; use it rather than a copy.
WEB_ADDRESS = re.compile(
    r"[a-z][a-z0-9+.-]*://|(?<![\w-])www\.|(?<![\w-])[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,63}(?![\w-])",
    re.IGNORECASE,
)
WEB_ADDRESS_MESSAGE = (
    "Leave out web addresses and domain names (such as example.com). You can rename the organization after signing up."
)


def has_web_address(value: str) -> bool:
    """Whether `value` contains something a mail client would show as a link (WEB_ADDRESS)."""
    return WEB_ADDRESS.search(value) is not None


# Browser sign-in navigations: their handlers sanitize `next` and `state` themselves and must always end in a redirect
# (a NUL in `next` lands on the dashboard), never in a JSON problem the browser would show as a page.
SIGN_IN_NAVIGATION = re.compile(r"^/api/v1/auth/(oidc/(start|callback)|saml/start|saml/[\w-]+/acs)$")


def _shown(name: str) -> str:
    return name.replace("\x00", "\\u0000")


class QueryGuardMiddleware:
    """A NUL character in an /api/ path or query parameter (`?q=a%00b`) is a 400 naming the parameter, before any
    view, filter or hand-written parser sees it. Percent-decoding can't produce a surrogate (Django decodes the query
    string with replacement), so NUL is the one character to refuse here."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/") and not SIGN_IN_NAVIGATION.match(request.path):
            errors: dict[str, list[str]] = {}
            if "\x00" in request.path:
                errors["path"] = [NUL_MESSAGE]
            for name, values in request.GET.lists():
                if "\x00" in name or any("\x00" in v for v in values):
                    errors[_shown(name)] = [NUL_MESSAGE]
                if len(errors) >= MAX_ERRORS:
                    break
            if errors:
                return problem_response(400, "Some parameters need attention.", INVALID_REQUEST, errors=errors)
        return self.get_response(request)
