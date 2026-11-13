"""The OpenAPI schema describes what the API really answers (docs/API.md "Generated client").

Every GET operation in the served schema is called as the seeded demo organization's Admin (tests/api_walk.py fills
path parameters from real rows) and each response is checked against the operation's declared responses, strictly:
the status is declared, a body comes with a declared content type, and a JSON body validates against the declared
schema with `additionalProperties: false` on every object that lists its properties (so a double-wrapped page, an
array declared as an object, or a null in a field not declared nullable all fail). A client generated from the schema
with strict deserialization reads every one of these responses."""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import jsonschema  # type: ignore[import-untyped]  # a drf-spectacular dependency; its stubs are not installed
import pytest

from tests.api_walk import UNREACHABLE, Walker, body_of, seeded_walker

pytestmark = pytest.mark.django_db

DROPPED = {"nullable", "readOnly", "writeOnly", "example", "xml", "externalDocs", "discriminator", "deprecated"}
UNCHECKED_FORMATS = {"double", "float", "int64", "int32", "binary", "byte", "password", "decimal"}


def convert(node: Any) -> Any:
    """An OpenAPI 3.0 schema as JSON Schema draft 7: `nullable` becomes a null type, refs stay (resolved from the
    components), and an object that lists properties without saying otherwise allows no others."""
    if isinstance(node, list):
        return [convert(n) for n in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in DROPPED:
            continue
        if key == "properties":
            out[key] = {name: convert(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties", "not", "allOf", "oneOf", "anyOf"):
            out[key] = convert(value)
        else:
            out[key] = value
    if "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = False
    if out.get("format") in UNCHECKED_FORMATS:
        out.pop("format")
    if node.get("nullable"):
        if "enum" in out and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
        if "type" in out:
            kind = out["type"]
            out["type"] = [*(kind if isinstance(kind, list) else [kind]), "null"]
        else:
            out = {"anyOf": [out, {"type": "null"}]}
    return out


def schema_errors(components: dict[str, Any], declared: dict[str, Any], body: Any) -> list[str]:
    document = copy.deepcopy(components)
    document.update(convert(declared))
    validator = jsonschema.Draft7Validator(document, format_checker=jsonschema.FormatChecker())
    found = []
    for error in validator.iter_errors(body):
        leaves = (error.context or [error]) if error.validator in ("anyOf", "oneOf") else [error]
        for leaf in leaves:
            where = "/".join("*" if isinstance(p, int) else str(p) for p in leaf.absolute_path) or "<root>"
            found.append(f"[{leaf.validator}] at {where}: {leaf.message[:200]}")
    return found


def contract_problems(walker: Walker) -> tuple[list[str], Counter]:
    components = {
        "components": {"schemas": {name: convert(s) for name, s in walker.schema["components"]["schemas"].items()}}
    }
    problems: list[str] = []
    seen: Counter = Counter()
    for template, operation, paths in walker.operations():
        declared = operation.get("responses", {})
        for path in paths:
            response = walker.fetch(path)
            status = response.status_code
            seen[status] += 1
            ctype = response.get("Content-Type", "").split(";")[0]
            if str(status) not in declared:
                problems.append(f"GET {template}: status {status} isn't declared ({path})")
                continue
            content = declared[str(status)].get("content") or {}
            has_body = bool(response.streaming or response.content)
            if not content:
                if has_body:
                    problems.append(f"GET {template}: {status} has a {ctype} body but declares none")
                continue
            if ctype not in content and "*/*" not in content and f"{ctype.split('/')[0]}/*" not in content:
                problems.append(f"GET {template}: {status} answers {ctype}, declared {', '.join(content)}")
                continue
            if ctype != "application/json" and not ctype.endswith("+json"):  # a file (CSV, JSON lines, zip)
                continue
            seen["validated"] += 1
            for error in dict.fromkeys(schema_errors(components, content[ctype].get("schema", {}), body_of(response))):
                problems.append(f"GET {template}: {error}")
    return problems, seen


def test_every_get_response_matches_the_declared_schema(preview_on):
    walker = seeded_walker()
    assert walker.unreached() == set(UNREACHABLE), "update tests/api_walk.py UNREACHABLE (seed the row, or say why)"
    problems, seen = contract_problems(walker)
    assert not problems, "\n".join(dict.fromkeys(problems))
    assert seen["validated"] > 250  # the walk reached the API, not an empty schema
