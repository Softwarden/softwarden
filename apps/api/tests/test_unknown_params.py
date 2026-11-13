"""Every read refuses a query parameter it doesn't take (docs/API.md "Every list and every other read ... refuses
a parameter it doesn't take with a 400 naming it").

Every GET operation in the served schema is called as the seeded demo organization's Admin (tests/api_walk.py), once
as it is and once with `?zz_typo=1` added. A route that answers the plain call must answer the typo with a 400 problem
whose `errors` name the parameter, and declare that 400, unless it is in ACCEPTS_ANY below with the reason. A route the
seeded data can't reach (no row to fill its path) isn't called; its view calls core.drf.refuse_unknown_params like
the rest."""

from __future__ import annotations

import json

import pytest

from core.openapi import ACCEPTS_ANY_PARAMS
from tests.api_walk import UNREACHABLE, seeded_walker

pytestmark = pytest.mark.django_db

TYPO = "zz_typo"

# The routes that answer whatever comes with them, each with its reason (core/openapi.py, which also leaves them
# without the 400).
ACCEPTS_ANY = ACCEPTS_ANY_PARAMS


def with_typo(url: str) -> str:
    return url + ("&" if "?" in url else "?") + f"{TYPO}=1"


def test_every_read_refuses_a_parameter_it_does_not_take(preview_on):
    walker = seeded_walker()
    assert set(ACCEPTS_ANY) <= set(walker.gets), "ACCEPTS_ANY names a route the schema doesn't have"
    assert walker.unreached() == set(UNREACHABLE), "update tests/api_walk.py UNREACHABLE (seed the row, or say why)"
    problems = []
    checked = 0
    for template, operation, urls in walker.operations(limit=1):
        if not urls or template in ACCEPTS_ANY:
            continue
        plain = walker.fetch(urls[0])
        if plain.status_code >= 300:  # in UNREACHABLE: refused or redirected before reading parameters
            continue
        typo = walker.client_for(urls[0]).get(with_typo(urls[0]))
        checked += 1
        if typo.status_code != 400:
            problems.append(f"GET {template}: {typo.status_code} with ?{TYPO}=1 (plain {plain.status_code})")
            continue
        body = json.loads(typo.content)
        if TYPO not in body.get("errors", {}):
            problems.append(f"GET {template}: the 400 doesn't name {TYPO}: {body}")
        if "400" not in operation["responses"]:
            problems.append(f"GET {template}: the schema doesn't declare the 400")
    assert not problems, "\n".join(problems)
    assert checked > 200  # the walk reached the API
