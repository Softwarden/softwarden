"""Renewals calendar feed (money/calendar_feeds.py, money/ics.py, money/report_views.calendar_feed_ics): a
per-person secret URL, stored hashed, that a calendar app subscribes to. The feed is checked with an independent
iCalendar parser (the `icalendar` package, tests only) and line by line against RFC 5545's framing rules: CRLF, 75-octet
folding, escaped text, all-day events with stable UIDs. Acceptance check: "the renewals calendar ICS subscribes in Outlook"
(the real Outlook subscription is an owner check)."""

import json
import logging
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import icalendar
import pytest
from django.core.cache import cache
from django.test import Client

from api import tokens
from catalog import services
from core.models import AuditEvent, Membership
from money import calendar_feeds, contracts, ics, renewals
from money.models import CalendarFeed, Contract
from tests.test_tokens import mint  # noqa: F401  (mint is a fixture)

pytestmark = pytest.mark.django_db
FEED = "/api/v1/calendar/renewals.ics"


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()  # the feed throttles count in the process cache
    yield
    cache.clear()


def v(component, name) -> Any:
    """A property of a parsed calendar component (icalendar's own types are a wide union)."""
    return component[name]


def iso(a, days):
    return (services.org_today(a["org"]) + timedelta(days=days)).isoformat()


def secret_of(body):
    return parse_qs(urlsplit(body["url"]).query)["token"][0]


def fetch(secret, **params):
    query = "&".join(f"{k}={v}" for k, v in {"token": secret, **params}.items())
    return Client().get(f"{FEED}?{query}", HTTP_ACCEPT="text/calendar")


@pytest.fixture
def subscribed(a, api):
    org, editor = a["org"], a["users"]["editor"]
    contracts.create_contract(org, editor, {"name": "Suite, Pro; edition", "end_date": iso(a, 120), "auto_renew": "yes",
                                            "notice_value": 30, "notice_unit": "days",
                                            "create_renewal_obligation": True})  # fmt: skip
    renewals.create_renewal(org, editor, {"name": "Domain", "renewal_date": iso(a, 15), "auto_renew": "no"})
    client = api(a["users"]["viewer"])
    created = client.post("calendar-feed")
    assert created.status_code == 201, created.json_body
    return {"client": client, "body": created.json_body, "secret": secret_of(created.json_body)}


# ---- the feed ---------------------------------------------------------------------------------------------------------


def test_feed_parses_and_lists_the_deadlines(a, subscribed):
    response = fetch(subscribed["secret"])
    assert response.status_code == 200
    assert response["Content-Type"] == "text/calendar; charset=utf-8"
    cal = icalendar.Calendar.from_ical(response.content)
    assert str(v(cal, "VERSION")) == "2.0" and str(v(cal, "METHOD")) == "PUBLISH" and v(cal, "PRODID")
    assert str(v(cal, "X-WR-CALNAME")) == "Softwarden renewals (orga)"
    events = {str(v(e, "SUMMARY")): e for e in cal.walk("VEVENT")}
    assert set(events) == {
        "Cancellation deadline: Suite, Pro; edition",  # escaped on the wire, read back intact
        "Renewal action due: Domain",
    }
    suite = events["Cancellation deadline: Suite, Pro; edition"]
    start = v(suite, "DTSTART").dt
    assert isinstance(start, date) and start.isoformat() == iso(a, 90)  # all-day: a date, not a date-time
    assert v(suite, "DTSTART").params["VALUE"] == "DATE"
    assert v(suite, "DTEND").dt == start + timedelta(days=1)
    assert v(suite, "DTSTAMP").dt.tzinfo is not None and str(v(suite, "TRANSP")) == "TRANSPARENT"
    assert "Renewal date" in str(v(suite, "DESCRIPTION")) and "\n" in str(v(suite, "DESCRIPTION"))
    assert str(v(suite, "URL")).startswith("http://testserver/renewals/")
    assert v(events["Renewal action due: Domain"], "DTSTART").dt.isoformat() == iso(a, 15 - a["org"].renewal_lead_days)


def test_uids_are_stable_across_fetches_and_edits(a, subscribed):
    def uids():
        cal = icalendar.Calendar.from_ical(fetch(subscribed["secret"]).content)
        return {str(v(e, "SUMMARY")): (str(v(e, "UID")), v(e, "DTSTART").dt) for e in cal.walk("VEVENT")}

    first = uids()
    assert first == uids()
    suite = Contract.objects.get(name="Suite, Pro; edition")
    contracts.update_contract(a["org"], a["users"]["editor"], suite, {"end_date": iso(a, 150)})
    moved = uids()
    key = "Cancellation deadline: Suite, Pro; edition"
    assert moved[key][0] == first[key][0] and moved[key][1] != first[key][1]  # same event, new date


def physical_lines(body: bytes) -> list[bytes]:
    assert body.endswith(b"\r\n")
    lines = body[:-2].split(b"\r\n")
    assert all(b"\n" not in line and b"\r" not in line for line in lines)  # CRLF only, never a bare LF
    return lines


def test_crlf_folding_and_escaping_on_the_wire(a, subscribed):
    long_name = "Ünïcödé ✓ " * 12 + "end"  # multi-octet characters across the fold
    renewals.create_renewal(a["org"], a["users"]["editor"], {"name": long_name, "explicit_deadline": iso(a, 5)})
    renewals.create_renewal(a["org"], a["users"]["editor"], {"name": "Back\\slash", "explicit_deadline": iso(a, 6)})
    body = fetch(subscribed["secret"]).content
    lines = physical_lines(body)
    assert all(len(line) <= 75 for line in lines), max(len(line) for line in lines)
    assert any(line.startswith(b" ") for line in lines)  # something was folded
    for line in lines:
        line.decode("utf-8")  # no character split across a fold
    unfolded = body.replace(b"\r\n ", b"").decode("utf-8")
    assert "SUMMARY:Cancellation deadline: Suite\\, Pro\\; edition\r\n" in unfolded
    assert "SUMMARY:Cancellation deadline: Back\\\\slash\r\n" in unfolded
    assert f"SUMMARY:Cancellation deadline: {long_name}\r\n" in unfolded
    names = {str(v(e, "SUMMARY")) for e in icalendar.Calendar.from_ical(body).walk("VEVENT")}
    assert f"Cancellation deadline: {long_name}" in names and "Cancellation deadline: Back\\slash" in names


def test_the_writer_folds_at_75_octets_and_escapes():
    line = "DESCRIPTION:" + "é" * 80
    folded = ics.fold(line)
    parts = folded.encode("utf-8").split(b"\r\n")
    assert all(len(p) <= 75 for p in parts) and parts[1].startswith(b" ")
    assert folded.replace("\r\n ", "") == line
    assert ics.fold("x" * 75) == "x" * 75
    assert ics.fold("x" * 76) == "x" * 75 + "\r\n x"
    assert ics.escape_text("a,b;c\\d\ne\r\nf\x07g") == "a\\,b\\;c\\\\d\\ne\\nfg"


def test_months_parameter(a, subscribed):
    renewals.create_renewal(a["org"], a["users"]["editor"], {"name": "Far", "explicit_deadline": iso(a, 600)})
    default = icalendar.Calendar.from_ical(fetch(subscribed["secret"]).content)
    assert "Cancellation deadline: Far" not in {str(v(e, "SUMMARY")) for e in default.walk("VEVENT")}
    wide = icalendar.Calendar.from_ical(fetch(subscribed["secret"], months=24).content)
    assert "Cancellation deadline: Far" in {str(v(e, "SUMMARY")) for e in wide.walk("VEVENT")}
    assert fetch(subscribed["secret"], months="nonsense").status_code == 200  # falls back to the default


def test_an_empty_calendar_is_still_valid(a, api):
    secret = secret_of(api(a["users"]["admin"]).post("calendar-feed").json_body)
    body = fetch(secret).content
    cal = icalendar.Calendar.from_ical(body)
    assert list(cal.walk("VEVENT")) == [] and physical_lines(body)[-1] == b"END:VCALENDAR"
    # RFC 5545 §3.6: still at least one component (a UTC time zone definition)
    assert [c.name for c in cal.subcomponents] == ["VTIMEZONE"] and str(v(cal.subcomponents[0], "TZID")) == "UTC"


# ---- managing your feed -------------------------------------------------------------------------------------------------


def test_create_shows_the_address_once_and_stores_only_a_hash(a, subscribed):
    body, secret, client = subscribed["body"], subscribed["secret"], subscribed["client"]
    assert secret.startswith("swc_") and len(secret) == 44
    assert body["url"] == f"http://testserver{FEED}?token={secret}"
    assert "webcal_url" not in body  # webcal:// would fetch over plain http first
    assert body["active"] and body["prefix"] == secret[:12]
    feed = CalendarFeed.objects.get(id=body["id"])
    assert feed.token_hash == calendar_feeds.hash_secret(secret) and secret not in feed.token_hash
    shown = client.get("calendar-feed")
    assert shown.json_body == {**{k: body[k] for k in ("active", "id", "prefix", "created_at")}, "last_used_at": None}
    assert secret not in shown.content.decode()
    again = client.post("calendar-feed")
    assert again.status_code == 409 and "Rotate" in again.json_body["detail"]
    fetch(secret)
    assert client.get("calendar-feed").json_body["last_used_at"] is not None


def test_rotate_and_revoke(a, subscribed):
    client, old = subscribed["client"], subscribed["secret"]
    rotated = client.post("calendar-feed/rotate")
    assert rotated.status_code == 200
    new = secret_of(rotated.json_body)
    assert new != old and rotated.json_body["id"] != subscribed["body"]["id"]
    assert fetch(old).status_code == 404 and fetch(new).status_code == 200
    revoked = client.delete("calendar-feed")
    assert revoked.status_code == 200 and revoked.json_body["active"] is False
    assert fetch(new).status_code == 404
    assert client.delete("calendar-feed").status_code == 200  # nothing to revoke: unchanged
    assert client.post("calendar-feed/rotate").status_code == 404
    assert client.post("calendar-feed").status_code == 201  # a new one after revoking
    actions = list(
        AuditEvent.objects.filter(organization=a["org"], target_type="CalendarFeed")
        .order_by("created_at")
        .values_list("action", "actor_id")
    )
    viewer = a["users"]["viewer"].id
    assert actions == [
        ("calendar_feed.created", viewer),
        ("calendar_feed.rotated", viewer),
        ("calendar_feed.revoked", viewer),
        ("calendar_feed.created", viewer),
    ]


def problem(response):
    return response.status_code, response["Content-Type"], json.loads(response.content)


@pytest.mark.parametrize("bad", ["", "nonsense", "swc_" + "x" * 40, "sw_" + "y" * 40, "swc_" + "z" * 200])
def test_a_bad_token_is_404_like_a_revoked_one(a, subscribed, bad):
    client = subscribed["client"]
    rotated = secret_of(client.post("calendar-feed/rotate").json_body)
    revoked = fetch(subscribed["secret"])  # the old address
    assert problem(fetch(bad)) == problem(revoked)
    assert problem(revoked)[0] == 404 and problem(revoked)[1] == "application/problem+json"
    assert Client().get(FEED).status_code == 404  # no token at all
    assert fetch(rotated).status_code == 200


def test_the_feed_stops_when_the_owner_leaves_or_is_deactivated(a, subscribed):
    secret, viewer = subscribed["secret"], a["users"]["viewer"]
    viewer.is_active = False
    viewer.save()
    assert fetch(secret).status_code == 404
    viewer.is_active = True
    viewer.save()
    assert fetch(secret).status_code == 200
    Membership.objects.filter(user=viewer, organization=a["org"]).delete()
    assert fetch(secret).status_code == 404


def test_the_feed_is_its_organizations_own(a, b, api, subscribed):
    """A person in two organizations has one feed in each, each listing only its own deadlines."""
    renewals.create_renewal(b["org"], b["users"]["editor"], {"name": "Theirs", "explicit_deadline": iso(a, 7)})
    Membership.objects.create(user=a["users"]["viewer"], organization=b["org"], role="viewer")
    mine = {
        str(v(e, "SUMMARY")) for e in icalendar.Calendar.from_ical(fetch(subscribed["secret"]).content).walk("VEVENT")
    }
    assert not any("Theirs" in s for s in mine)
    theirs = calendar_feeds.create_feed(b["org"], a["users"]["viewer"])[1]
    listed = {str(v(e, "SUMMARY")) for e in icalendar.Calendar.from_ical(fetch(theirs).content).walk("VEVENT")}
    assert listed == {"Cancellation deadline: Theirs"}
    # Each organization's management routes see only that organization's feed.
    assert subscribed["client"].get("calendar-feed").json_body["id"] == subscribed["body"]["id"]


def test_feed_management_is_session_only_and_any_role(a, api, mint):  # noqa: F811
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).post("calendar-feed").status_code == 201
    assert api().get("calendar-feed").status_code in (401, 403)
    bearer = Client(HTTP_AUTHORIZATION=f"Bearer {mint('admin', read_only=False)['secret']}")
    refused = bearer.get("/api/v1/calendar-feed")
    assert refused.status_code == 403 and "signed-in session" in json.loads(refused.content)["detail"]
    assert bearer.post("/api/v1/calendar-feed/rotate", content_type="application/json").status_code == 403


def test_the_feed_is_rate_limited(a, subscribed, settings):
    settings.CALENDAR_FEED_RATE = "2/min"
    assert fetch(subscribed["secret"]).status_code == 200
    assert fetch(subscribed["secret"]).status_code == 200
    limited = fetch(subscribed["secret"])
    assert limited.status_code == 429 and limited["Retry-After"]
    assert fetch("swc_" + "q" * 40).status_code == 404  # another key: its own budget
    settings.CALENDAR_FEED_RATE = ""
    settings.CALENDAR_FEED_ADDRESS_RATE = "1/min"
    cache.clear()
    assert fetch("swc_" + "q" * 40).status_code == 404
    assert fetch(subscribed["secret"]).status_code == 429  # one address over every feed


def test_the_secret_never_reaches_logs_audit_or_responses(a, subscribed, caplog):
    caplog.set_level(logging.DEBUG)
    secret, client = subscribed["secret"], subscribed["client"]
    fetch(secret)
    fetch("swc_" + "w" * 40)
    rotated = client.post("calendar-feed/rotate").json_body
    new = secret_of(rotated)
    fetch(secret)  # 404, logged by Django as "Not Found: <path>"
    client.delete("calendar-feed")
    for value in (secret, new, secret[12:], new[12:]):
        assert value not in caplog.text
        for event in AuditEvent.objects.filter(organization=a["org"]):
            dumped = json.dumps([event.changes, event.prechange, event.postchange])
            assert value not in dumped
            assert calendar_feeds.hash_secret(value) not in dumped
    assert client.get("calendar-feed").content.decode().count("swc_") == 0
    events = AuditEvent.objects.filter(organization=a["org"], target_type="CalendarFeed")
    for event in events:
        assert set(event.changes) <= {"owner", "prefix", "replaces"}
        for snap in (event.prechange, event.postchange):
            assert snap is None or set(snap) <= {"user", "prefix", "revoked_at"}


def test_create_rotate_revoke_services_refuse_twice(a):
    org, viewer = a["org"], a["users"]["viewer"]
    feed, _ = calendar_feeds.create_feed(org, viewer)
    from core.problems import Conflict

    with pytest.raises(Conflict):
        calendar_feeds.create_feed(org, viewer)
    calendar_feeds.revoke_feed(org, viewer, feed)
    with pytest.raises(Conflict):
        calendar_feeds.rotate_feed(org, viewer, feed)


def test_api_tokens_are_not_feed_secrets(a, mint):  # noqa: F811
    """An API token in the query string is not a feed secret, and a feed secret is not an API token."""
    api_secret = mint("admin")["secret"]
    assert fetch(api_secret).status_code == 404
    feed_secret = calendar_feeds.create_feed(a["org"], a["users"]["admin"])[1]
    response = Client(HTTP_AUTHORIZATION=f"Bearer {feed_secret}").get("/api/v1/applications")
    assert response.status_code in (401, 403)
    assert tokens.SECRET_PREFIX != calendar_feeds.SECRET_PREFIX[: len(tokens.SECRET_PREFIX)]


def test_removing_a_member_revokes_their_feed_here_for_good(a, b, api):
    """Re-adding them later doesn't bring the old address back; their feed in another organization is untouched."""
    shared = b["users"]["editor"]
    Membership.objects.create(user=shared, organization=a["org"], role="editor")
    here = calendar_feeds.create_feed(a["org"], shared)[1]
    there = calendar_feeds.create_feed(b["org"], shared)[1]
    assert api(a["users"]["admin"]).delete(f"users/{shared.id}").status_code == 204
    assert fetch(here).status_code == 404 and fetch(there).status_code == 200
    event = AuditEvent.objects.get(organization=a["org"], action="calendar_feed.revoked")
    assert event.actor_id == a["users"]["admin"].id and event.changes["owner"] == shared.id
    Membership.objects.create(user=shared, organization=a["org"], role="editor")
    assert fetch(here).status_code == 404
