"""Saved views and subscriptions (catalog/saved_filters.py, catalog/saved_views.py, notify/subscriptions.py):
views with an owner,
`shared` and `columns`; who may change them; subscriptions, their roles and tenancy; the weekly job on each
organization's local date; the skip cases; the test send. The acceptance check end to end: a Wednesday subscription sends
exactly one email on the organization's local Wednesday at the send time with the view's rows, and its link's query
is the view's query; a rerun sends nothing, Thursday sends nothing, and the local date decides across time zones.
Email goes to Django's in-memory outbox; nothing contacts Microsoft."""

import urllib.parse
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.db import connection

from catalog import customfields, saved_filters, services
from catalog.models import Application, Resource, SavedFilter
from core import jobs
from core.models import AuditEvent, Membership
from notify import subscriptions
from notify.models import Delivery, NotificationPreference, SavedViewSubscription
from notify.subscription_serializers import SavedViewSubscriptionSerializer

pytestmark = pytest.mark.django_db

WEDNESDAY = date(2026, 11, 11)
THURSDAY = WEDNESDAY + timedelta(days=1)
BASE = "https://softwarden.example.test"


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = BASE


def drain() -> None:
    while (job := jobs.claim()) is not None:
        jobs.run(job)


def at(org, day: date, hh: int, mm: int) -> datetime:
    """A moment on the organization's local date and time."""
    return datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo(org.timezone))


def app(org, editor, name, lifecycle="active"):
    return services.create_resource(org, editor, {"name": name, "lifecycle": lifecycle})


def view(f, name="Active apps", query=None, shared=True, owner="editor", columns=None, kind="application"):
    return saved_filters.create(
        f["org"],
        f["users"][owner],
        {
            "name": name,
            "kind": kind,
            "query": query if query is not None else {"lifecycle": "active"},
            "shared": shared,
            "columns": columns or [],
        },
    )


def subscribe(f, saved, who="viewer", weekday=2, actor=None):
    actor = actor or f["users"][who]
    return subscriptions.create(
        f["org"], actor, {"saved_view": str(saved.id), "user": f["users"][who].id, "weekday": weekday}
    )


def sub_deliveries(f=None):
    rows = Delivery.objects.filter(kind="subscription")
    if f is not None:
        rows = rows.filter(organization=f["org"])
    return rows.order_by("created_at", "id")


# ---- views: owner, sharing, columns -------------------------------------------------------------------------------


def test_a_private_view_is_its_owners_alone_and_a_shared_one_is_everyones(a, api):
    viewer, editor, admin = (api(a["users"][r]) for r in ("viewer", "editor", "admin"))
    private = viewer.post("saved-filters", {"name": "Mine", "kind": "application", "query": {}})
    assert private.status_code == 201 and private.json_body["shared"] is False
    assert private.json_body["owner"]["id"] == a["users"]["viewer"].id and private.json_body["mine"] is True
    pid = private.json_body["id"]
    shared = editor.post("saved-filters", {"name": "Team", "kind": "application", "query": {}, "shared": True})
    sid = shared.json_body["id"]
    for client, expected in ((viewer, {"Mine", "Team"}), (editor, {"Team"}), (admin, {"Team"})):
        names = {f["name"] for f in client.get("saved-filters?kind=application").json_body if not f["builtin"]}
        assert names == expected
    assert admin.get(f"saved-filters/{pid}").status_code == 404  # Admins included
    assert admin.patch(f"saved-filters/{pid}", {"name": "Took"}).status_code == 404
    assert admin.delete(f"saved-filters/{pid}").status_code == 404
    assert viewer.get(f"saved-filters/{sid}").json_body["can_edit"] is False
    assert admin.get(f"saved-filters/{sid}").json_body["can_edit"] is True


@pytest.mark.parametrize(
    "owner,actor,shared,expected",
    [
        ("viewer", "viewer", False, 200),  # anyone changes their own private view
        ("editor", "editor", True, 200),  # an Editor owner changes their shared view
        ("editor", "admin", True, 200),  # any Admin changes a shared view
        ("admin", "editor", True, 403),  # an Editor can't change a colleague's shared view
        ("editor", "viewer", True, 403),
        ("editor", "admin", False, 404),  # someone else's private view doesn't exist
        ("viewer", "editor", False, 404),
    ],
)
def test_who_may_change_or_delete_a_view(a, api, owner, actor, shared, expected):
    saved = view(a, shared=shared, owner=owner)
    client = api(a["users"][actor])
    assert client.patch(f"saved-filters/{saved.id}", {"name": "Renamed"}).status_code == expected
    deleted = client.delete(f"saved-filters/{saved.id}")
    assert deleted.status_code == (204 if expected == 200 else expected)
    assert SavedFilter.objects.filter(id=saved.id).exists() is (expected != 200)


def test_an_owner_demoted_to_viewer_can_no_longer_change_their_shared_view(a, api):
    saved = view(a, shared=True, owner="editor")
    Membership.objects.filter(user=a["users"]["editor"], organization=a["org"]).update(role="viewer")
    client = api(a["users"]["editor"])
    assert client.patch(f"saved-filters/{saved.id}", {"name": "x"}).status_code == 403
    assert client.delete(f"saved-filters/{saved.id}").status_code == 403


def test_sharing_needs_editor_and_unsharing_moves_the_view_back_to_its_owner(a, api):
    viewer, admin = api(a["users"]["viewer"]), api(a["users"]["admin"])
    mine = view(a, shared=False, owner="viewer")
    assert viewer.patch(f"saved-filters/{mine.id}", {"shared": True}).status_code == 403
    theirs = view(a, name="Editor's", shared=True, owner="editor")
    done = admin.patch(f"saved-filters/{theirs.id}", {"shared": False})
    assert done.status_code == 200 and done.json_body["shared"] is False and done.json_body["mine"] is False
    assert admin.get(f"saved-filters/{theirs.id}").status_code == 404  # now the editor's private view
    assert api(a["users"]["editor"]).get(f"saved-filters/{theirs.id}").status_code == 200
    ownerless = SavedFilter.objects.create(organization=a["org"], shared=True, kind="application", name="Old", query={})
    refused = admin.patch(f"saved-filters/{ownerless.id}", {"shared": False})
    assert refused.status_code == 400 and "no owner" in refused.json()["errors"]["shared"][0]
    assert admin.patch(f"saved-filters/{ownerless.id}", {"name": "Older"}).status_code == 200


def test_a_change_is_validated_audited_and_takes_if_match(a, api):
    editor = api(a["users"]["editor"])
    saved = view(a, shared=True)
    url = f"saved-filters/{saved.id}"
    assert editor.patch(url, {"query": {"nonsense": "1"}}).status_code == 400
    assert editor.patch(url, {"columns": ["nope"]}).status_code == 400
    assert editor.patch(url, {"kind": "component"}).status_code == 400  # a view keeps its list
    assert editor.patch("saved-filters/builtin:active", {"name": "x"}).status_code == 400
    current = editor.get(url)
    changed = editor.client.patch(
        "/api/v1/" + url,
        data='{"name": "Live apps", "columns": ["lifecycle", "it_steward"], "query": {"lifecycle": "active,deprecated"}}',
        content_type="application/json",
        HTTP_IF_MATCH=current["ETag"],
    )
    assert changed.status_code == 200
    body = changed.json()
    assert body["name"] == "Live apps" and body["columns"] == ["lifecycle", "it_steward"]
    stale = editor.client.patch(
        "/api/v1/" + url, data='{"name": "Again"}', content_type="application/json", HTTP_IF_MATCH=current["ETag"]
    )
    assert stale.status_code == 412
    event = AuditEvent.objects.filter(action="saved_filter.updated").latest("created_at")
    assert event.changes["name"] == ["Active apps", "Live apps"]
    assert event.changes["columns"] == [[], ["lifecycle", "it_steward"]]
    post = event.postchange or {}
    assert post["columns"] == ["lifecycle", "it_steward"] and post["shared"] is True


def test_columns_are_the_lists_columns_in_order(a, api):
    editor = api(a["users"]["editor"])
    body = {"name": "Cols", "kind": "application", "query": {}}
    assert editor.post("saved-filters", {**body, "columns": ["component_type"]}).status_code == 400  # a component's
    assert editor.post("saved-filters", {**body, "columns": ["lifecycle", "lifecycle"]}).status_code == 400
    assert editor.post("saved-filters", {**body, "columns": "lifecycle"}).status_code == 400
    assert editor.post("saved-filters", {**body, "columns": ["validation_status"]}).status_code == 400  # profile off
    assert editor.post("saved-filters", {**body, "columns": ["cf_tier"]}).status_code == 400  # no such field yet
    customfields.create_field(
        a["org"], a["users"]["admin"], {"name": "tier", "label": "Tier", "type": "text", "applies_to": ["application"]}
    )
    made = editor.post("saved-filters", {**body, "columns": ["cf_tier", "vendor", "lifecycle"]})
    assert made.status_code == 201 and made.json_body["columns"] == ["cf_tier", "vendor", "lifecycle"]


def test_deleting_a_custom_field_removes_its_column_from_views(a):
    admin = a["users"]["admin"]
    field = customfields.create_field(
        a["org"], admin, {"name": "tier", "label": "Tier", "type": "text", "applies_to": ["application"]}
    )
    saved = view(a, columns=["cf_tier", "lifecycle"])
    customfields.delete_field(a["org"], admin, field)
    saved.refresh_from_db()
    assert saved.columns == ["lifecycle"]


def test_listing_names_your_subscription(a, api):
    saved = view(a)
    sub = subscribe(a, saved, "viewer", weekday=4)
    rows = api(a["users"]["viewer"]).get("saved-filters?kind=application").json_body
    mine = next(r for r in rows if r["id"] == str(saved.id))
    assert mine["subscription"] == {"id": str(sub.id), "weekday": 4, "active": True}
    theirs = next(
        r for r in api(a["users"]["admin"]).get("saved-filters?kind=application").json_body if r["id"] == str(saved.id)
    )
    assert theirs["subscription"] is None


def test_response_keys_match_the_documented_shapes(a, api):
    from catalog.serializers import SavedFilterSerializer

    saved = view(a)
    body = api(a["users"]["editor"]).get(f"saved-filters/{saved.id}").json_body
    assert set(body) == set(SavedFilterSerializer().fields)
    sub = subscribe(a, saved, "editor")
    got = api(a["users"]["editor"]).get(f"saved-view-subscriptions/{sub.id}").json_body
    assert set(got) == set(SavedViewSubscriptionSerializer().fields)


# ---- subscriptions: who, what, tenancy -------------------------------------------------------------------------------


def test_a_member_subscribes_themselves_to_a_view_they_can_see(a, api):
    viewer = api(a["users"]["viewer"])
    shared, private_other = view(a), view(a, name="Editor only", shared=False)
    made = viewer.post("saved-view-subscriptions", {"saved_view": str(shared.id), "weekday": 2})
    assert made.status_code == 201
    body = made.json_body
    assert body["user"]["id"] == a["users"]["viewer"].id and body["mine"] and body["weekday_name"] == "Wednesday"
    assert body["send_time"] == "08:00" and body["timezone"] == a["org"].timezone and body["active"] is True
    again = viewer.post("saved-view-subscriptions", {"saved_view": str(shared.id), "weekday": 3})
    assert again.status_code == 400 and "Already subscribed" in again.json()["errors"]["saved_view"][0]
    assert (
        viewer.post("saved-view-subscriptions", {"saved_view": str(private_other.id), "weekday": 2}).status_code == 400
    )
    assert viewer.post("saved-view-subscriptions", {"saved_view": "builtin:active", "weekday": 2}).status_code == 400
    for bad in (7, -1, True, "2", None):
        assert (
            viewer.post("saved-view-subscriptions", {"saved_view": str(shared.id), "weekday": bad}).status_code == 400
        )
    own = view(a, name="Mine", shared=False, owner="viewer")
    assert viewer.post("saved-view-subscriptions", {"saved_view": str(own.id), "weekday": 0}).status_code == 201
    event = AuditEvent.objects.filter(action="saved_view_subscription.created").latest("created_at")
    assert event.changes["view_name"] is None and event.changes["user"] == a["users"]["viewer"].id  # private name


def test_only_admins_subscribe_someone_else_and_only_to_a_view_they_can_see(a, b, api):
    shared, admins_own = view(a), view(a, name="Admin only", shared=False, owner="admin")
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    body = {"saved_view": str(shared.id), "weekday": 1, "user": a["users"]["viewer"].id}
    assert editor.post("saved-view-subscriptions", body).status_code == 403
    made = admin.post("saved-view-subscriptions", body)
    assert made.status_code == 201 and made.json_body["user"]["id"] == a["users"]["viewer"].id
    assert not made.json_body["mine"] and made.json_body["can_edit"]
    refused = admin.post(
        "saved-view-subscriptions",
        {"saved_view": str(admins_own.id), "weekday": 1, "user": a["users"]["editor"].id},
    )
    assert refused.status_code == 400 and "private" in refused.json()["errors"]["saved_view"][0]
    outsider = admin.post(
        "saved-view-subscriptions", {"saved_view": str(shared.id), "weekday": 1, "user": b["users"]["viewer"].id}
    )
    assert outsider.status_code == 400 and "member" in outsider.json()["errors"]["user"][0]


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_every_role_manages_its_own_subscriptions(a, api, role):
    client = api(a["users"][role])
    sub = subscribe(a, view(a), role, weekday=0)
    url = f"saved-view-subscriptions/{sub.id}"
    assert [s["id"] for s in client.get("saved-view-subscriptions").json_body] == [str(sub.id)]
    assert client.patch(url, {"weekday": 5, "active": False}).json_body["weekday"] == 5
    assert client.patch(url, {"saved_view": str(uuid.uuid4())}).status_code == 400  # delete and subscribe again
    assert client.delete(url).status_code == 204
    actions = list(
        AuditEvent.objects.filter(action__startswith="saved_view_subscription.").values_list("action", flat=True)
    )
    assert sorted(actions) == [
        "saved_view_subscription.created",
        "saved_view_subscription.deleted",
        "saved_view_subscription.updated",
    ]


def test_someone_elses_subscription_exists_only_for_admins(a, api):
    sub = subscribe(a, view(a), "editor")
    private = subscribe(a, view(a, name="Viewer's own", shared=False, owner="viewer"), "viewer")
    viewer, admin = api(a["users"]["viewer"]), api(a["users"]["admin"])
    url = f"saved-view-subscriptions/{sub.id}"
    assert viewer.get(url).status_code == 404
    assert viewer.patch(url, {"weekday": 1}).status_code == 404
    assert viewer.delete(url).status_code == 404
    assert viewer.post(f"{url}/test-send").status_code == 404
    assert [s["id"] for s in viewer.get("saved-view-subscriptions").json_body] == [str(private.id)]
    listed = {s["id"]: s for s in admin.get("saved-view-subscriptions").json_body}
    assert set(listed) == {str(sub.id), str(private.id)}
    assert listed[str(private.id)]["saved_view"]["name"] is None  # a private view's name isn't the Admin's to see
    assert listed[str(sub.id)]["saved_view"]["name"] == "Active apps"
    only = admin.get(f"saved-view-subscriptions?user={a['users']['editor'].id}").json_body
    assert [s["id"] for s in only] == [str(sub.id)]
    assert admin.get("saved-view-subscriptions?bogus=1").status_code == 400
    assert admin.patch(url, {"active": False}).status_code == 200
    assert admin.delete(url).status_code == 204


def test_subscriptions_never_cross_organizations(a, b, api):
    theirs_view = view(b)
    theirs = subscribe(b, theirs_view, "viewer")
    admin = api(a["users"]["admin"])
    assert admin.get("saved-view-subscriptions").json_body == []
    assert admin.get(f"saved-view-subscriptions/{theirs.id}").status_code == 404
    assert admin.patch(f"saved-view-subscriptions/{theirs.id}", {"weekday": 1}).status_code == 404
    assert admin.post(f"saved-view-subscriptions/{theirs.id}/test-send").status_code == 404
    assert admin.delete(f"saved-view-subscriptions/{theirs.id}").status_code == 404
    refused = admin.post("saved-view-subscriptions", {"saved_view": str(theirs_view.id), "weekday": 1})
    assert refused.status_code == 400
    assert SavedViewSubscription.objects.filter(id=theirs.id).exists()


def test_removing_a_membership_removes_its_subscriptions(a):
    sub = subscribe(a, view(a), "viewer")
    Membership.objects.filter(user=a["users"]["viewer"], organization=a["org"]).delete()
    assert not SavedViewSubscription.objects.filter(id=sub.id).exists()


def test_unauthenticated(api):
    assert api().get("saved-view-subscriptions").status_code in (401, 403)
    assert api().post("saved-view-subscriptions", {}).status_code in (401, 403)


def test_the_preference_turns_saved_view_emails_off(a, api):
    client = api(a["users"]["viewer"])
    assert client.get("auth/me/preferences").json_body["subscriptions_enabled"] is True
    done = client.patch("auth/me/preferences", {"subscriptions_enabled": False})
    assert done.status_code == 200 and done.json_body["subscriptions_enabled"] is False


# ---- the weekly email: the acceptance check ------------------------------------------------------------------------------


@pytest.fixture
def wednesday(a):
    """An organization in New York with active and retired applications, a shared view of the active ones showing two
    columns, and the viewer subscribed for Wednesdays."""
    org, editor = a["org"], a["users"]["editor"]
    org.timezone = "America/New_York"
    org.save()
    app(org, editor, "Payroll")
    app(org, editor, "Legacy", lifecycle="retired")
    saved = view(a, columns=["lifecycle", "it_steward"])
    sub = subscribe(a, saved, "viewer", weekday=2)
    return {**a, "view": saved, "sub": sub}


def test_a_wednesday_subscription_sends_once_on_the_local_wednesday_with_the_rows_and_the_link(wednesday):
    f = wednesday
    org = f["org"]
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 7, 59)) == {}  # before the send time (08:00)
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 8, 26))["sent"] == 1
    drain()
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == [f["users"]["viewer"].email]
    assert message.subject == "Saved view: Active apps (2 applications)"
    body = message.body
    assert "Payroll" in body and "orga App" in body and "Legacy" not in body
    assert "Lifecycle: Active" in body and "IT steward: " in body
    html = message.alternatives[0][0]  # type: ignore[attr-defined]  # an EmailMultiAlternatives
    assert '<th scope="col"' in html and "IT steward" in html and "Legacy" not in html
    link = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("Open the view in Softwarden"))
    parts = urllib.parse.urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE}/applications"
    query = dict(urllib.parse.parse_qsl(parts.query))
    assert query.pop("preset") == "all" and query.pop("view") == str(f["view"].id)
    assert query == f["view"].query  # the link reproduces the filter
    delivery = sub_deliveries().get()
    assert delivery.status == "sent" and delivery.dedupe_key == f"sv:{f['sub'].id.hex}:{WEDNESDAY.isoformat()}"
    f["sub"].refresh_from_db()
    assert f["sub"].last_sent == WEDNESDAY

    # A rerun the same day, later that day, and Thursday send nothing.
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 8, 26)) == {}
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 23, 26)) == {}
    assert subscriptions.run_for_org(org, at(org, THURSDAY, 8, 26)) == {}
    drain()
    assert len(mail.outbox) == 1 and sub_deliveries().count() == 1
    # The next Wednesday is a new week.
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY + timedelta(days=7), 9, 26))["sent"] == 1


def test_the_schedule_runs_every_organization_and_is_registered_after_the_rules(wednesday):
    org = wednesday["org"]
    moment = at(org, WEDNESDAY, 8, 26).astimezone(UTC)
    assert subscriptions.scheduled_run(moment) == {
        "subscriptions_sent": 1,
        "subscriptions_skipped": 0,
        "subscriptions_failed": 0,
    }
    assert subscriptions.scheduled_run(moment)["subscriptions_sent"] == 0
    assert jobs.SCHEDULES["saved_views.subscriptions"].cron == "26 * * * *"
    names = list(jobs.SCHEDULES)
    assert names.index("notify.rules") < names.index("saved_views.subscriptions")


@pytest.mark.parametrize(
    "zone,utc_moment,sends",
    [
        # Auckland is UTC+13 in November: Tuesday 19:26 UTC is Wednesday 08:26 there.
        ("Pacific/Auckland", datetime(2026, 11, 10, 19, 26, tzinfo=UTC), True),
        # ...and Wednesday 19:26 UTC is already Thursday there.
        ("Pacific/Auckland", datetime(2026, 11, 11, 19, 26, tzinfo=UTC), False),
        # Los Angeles is UTC-8: Wednesday 15:26 UTC is 07:26 there, before the send time...
        ("America/Los_Angeles", datetime(2026, 11, 11, 15, 26, tzinfo=UTC), False),
        # ...and Thursday 03:26 UTC is still Wednesday evening there.
        ("America/Los_Angeles", datetime(2026, 11, 12, 3, 26, tzinfo=UTC), True),
    ],
)
def test_the_organizations_local_date_decides(wednesday, zone, utc_moment, sends):
    org = wednesday["org"]
    org.timezone = zone
    org.save()
    got = subscriptions.run_for_org(org, utc_moment)
    assert got.get("sent", 0) == (1 if sends else 0)
    if sends:
        assert sub_deliveries().get().dedupe_key.endswith(":2026-11-11")


def test_a_late_digest_time_still_sends_that_day(wednesday):
    org = wednesday["org"]
    org.digest_time = time(23, 50)  # later than every org's last hourly run: the send time is 23:00
    org.save()
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 22, 26)) == {}
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 23, 26))["sent"] == 1


def test_rows_are_capped_with_and_n_more(a):
    org = a["org"]
    for i in range(55):
        r = Resource.objects.create(organization=org, name=f"Bulk {i:02d}", slug=f"bulk-{i}", lifecycle="active")
        Application.objects.create(resource=r, organization=org)
    sub = subscribe(a, view(a, query={"q": "Bulk"}), "viewer", weekday=WEDNESDAY.weekday())
    composed = subscriptions.compose(org, sub, WEDNESDAY)
    assert isinstance(composed, subscriptions.Composed) and composed.total == 55
    text = composed.content["text"]
    assert text.count("  • Bulk") == 50 and "and 5 more" in text
    assert "Bulk 00" in text and "Bulk 54" not in text  # the list's own order (by name)


def test_the_rows_are_the_lists_rows_for_the_member(a, b, api):
    """The same rows `GET /applications?<query>` gives the subscriber: other organizations' records never, and the
    list's defaults (archived records hidden) apply."""
    org, editor = a["org"], a["users"]["editor"]
    hidden = app(org, editor, "Archived one")
    services.archive_resource(org, editor, hidden)
    app(b["org"], b["users"]["editor"], "Other org app")
    sub = subscribe(a, view(a, query={}), "viewer")
    composed = subscriptions.compose(org, sub, WEDNESDAY)
    assert isinstance(composed, subscriptions.Composed)
    listed = api(a["users"]["viewer"]).get("applications").json_body["results"]
    names = [line[4:].split(" — ")[0] for line in composed.content["text"].splitlines() if line.startswith("  • ")]
    assert names == [r["name"] for r in listed]
    assert "Archived one" not in names and "Other org app" not in names


def test_a_secret_looking_value_never_reaches_the_email(a):
    org, admin = a["org"], a["users"]["admin"]
    customfields.create_field(
        org, admin, {"name": "cfg", "label": "Config", "type": "object", "applies_to": ["application"]}
    )
    Resource.objects.filter(id=a["app"].id).update(custom_field_data={"cfg": {"client_secret": "S3CRET-VALUE-123"}})
    sub = subscribe(a, view(a, columns=["cf_cfg", "lifecycle"]), "viewer")
    composed = subscriptions.compose(org, sub, WEDNESDAY)
    assert isinstance(composed, subscriptions.Composed)
    assert "S3CRET" not in composed.content["text"] and "S3CRET" not in composed.content["html"]
    assert "Config: —" in composed.content["text"]


def test_the_job_payload_names_the_delivery_only(wednesday):
    org = wednesday["org"]
    from core.models import Job

    subscriptions.run_for_org(org, at(org, WEDNESDAY, 8, 26))
    job = Job.objects.get(kind="notify.deliver")
    assert set(job.payload) == {"delivery_id"}


# ---- skip cases ----------------------------------------------------------------------------------------------------


def _skipped(f) -> Delivery:
    found = sub_deliveries(f).get()
    assert found.status == "skipped" and found.content == {}
    return found


def _run_wednesday(f):
    return subscriptions.run_for_org(f["org"], at(f["org"], WEDNESDAY, 8, 26))


@pytest.mark.parametrize(
    "breakage,reason",
    [
        ("deleted", "the saved view was deleted"),
        ("deactivated", "account is deactivated"),
        ("no_email", "has no email address"),
        ("opted_out", "turned off saved view emails"),
        ("made_private", "can no longer see this view"),
        ("bad_filter", "the view's filter has problems"),
    ],
)
def test_nothing_is_sent_and_the_reason_is_recorded(wednesday, breakage, reason):
    f = wednesday
    saved, viewer = f["view"], f["users"]["viewer"]
    if breakage == "deleted":
        saved_filters.delete(f["org"], f["users"]["editor"], saved.id)
    elif breakage == "deactivated":
        viewer.is_active = False
        viewer.save()
    elif breakage == "no_email":
        viewer.email = ""
        viewer.save()
    elif breakage == "opted_out":
        NotificationPreference.objects.create(
            organization=f["org"],
            membership=Membership.objects.get(user=viewer, organization=f["org"]),
            subscriptions_enabled=False,
        )
    elif breakage == "made_private":
        saved_filters.update(f["org"], f["users"]["editor"], saved, {"shared": False})
    elif breakage == "bad_filter":
        SavedFilter.objects.filter(id=saved.id).update(query={"cf_gone": "x"})  # written behind the service's back
    assert _run_wednesday(f)["skipped"] == 1
    drain()
    assert mail.outbox == []
    skipped = _skipped(f)
    assert reason in skipped.last_error and skipped.dedupe_key.startswith(f"sv:{f['sub'].id.hex}:")
    assert _run_wednesday(f) == {}  # recorded once
    assert sub_deliveries(f).count() == 1


def test_a_list_the_members_organization_no_longer_has_is_skipped(a, monkeypatch):
    from core import capabilities as caps

    services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Helper", "kind": "ai_agent", "lifecycle": "active"}
    )
    agents = view(a, name="Agents", kind="ai_agent", query={})
    sub = subscribe(a, agents, "viewer", weekday=WEDNESDAY.weekday())
    assert isinstance(subscriptions.compose(a["org"], sub, WEDNESDAY), subscriptions.Composed)
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    skipped = subscriptions.compose(a["org"], sub, WEDNESDAY)
    assert isinstance(skipped, subscriptions.Skip) and "can no longer open this list" in skipped.reason


def test_a_turned_off_subscription_records_nothing(wednesday):
    subscriptions.update(wednesday["org"], wednesday["users"]["viewer"], wednesday["sub"], {"active": False})
    assert _run_wednesday(wednesday) == {}
    assert not sub_deliveries().exists()


def test_a_skipped_subscription_email_cant_be_resent(wednesday, api):
    saved_filters.delete(wednesday["org"], wednesday["users"]["editor"], wednesday["view"].id)
    _run_wednesday(wednesday)
    skipped = _skipped(wednesday)
    refused = api(wednesday["users"]["admin"]).post(f"notification-log/{skipped.id}/resend")
    assert refused.status_code == 409


# ---- test send -----------------------------------------------------------------------------------------------------


def backdate_tests():
    """Move every test delivery two minutes back, past the test-send limit."""
    from django.utils import timezone

    Delivery.objects.filter(kind="test").update(created_at=timezone.now() - timedelta(minutes=2))


def test_the_subscriber_or_an_admin_sends_this_weeks_email_now(wednesday, api):
    f = wednesday
    url = f"saved-view-subscriptions/{f['sub'].id}/test-send"
    done = api(f["users"]["viewer"]).post(url)
    assert done.status_code == 200 and done.json_body["ok"] is True and done.json_body["status"] == "sent"
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [f["users"]["viewer"].email]
    assert Delivery.objects.get(id=done.json_body["delivery"]).kind == "test"
    throttled = api(f["users"]["admin"]).post(url)  # once a minute per subscription, whoever asks
    assert throttled.status_code == 429 and "just sent" in throttled.json()["detail"]
    assert int(throttled["Retry-After"]) <= 61
    backdate_tests()
    assert api(f["users"]["admin"]).post(url).json_body["ok"] is True
    assert mail.outbox[1].to == [f["users"]["viewer"].email]  # always the subscriber, whoever asks
    assert api(f["users"]["editor"]).post(url).status_code == 404
    assert AuditEvent.objects.filter(action="saved_view_subscription.tested").count() == 2
    assert not sub_deliveries().exists()  # a test isn't this week's email
    saved_filters.delete(f["org"], f["users"]["editor"], f["view"].id)
    backdate_tests()
    refused = api(f["users"]["viewer"]).post(url)
    assert refused.status_code == 409 and "deleted" in refused.json()["detail"]


# ---- review follow-ups ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("zone", ["Asia/Kathmandu", "Australia/Eucla", "Pacific/Chatham", "Asia/Kolkata"])
def test_a_late_digest_time_sends_on_the_day_in_every_offset(wednesday, zone):
    """The run at :26 UTC lands at local :11 in +5:45 / +8:45 / +13:45 zones; with the send time capped at 23:00 a
    digest time of 23:50 still sends on the local Wednesday."""
    from tests.test_notification_rules import last_run_of_the_day

    org = wednesday["org"]
    org.timezone = zone
    org.digest_time = time(23, 50)
    org.save()
    last = last_run_of_the_day(zone, WEDNESDAY, 26)
    assert last.astimezone(ZoneInfo(zone)).hour == 23
    assert subscriptions.run_for_org(org, last - timedelta(hours=1)) == {}
    assert subscriptions.run_for_org(org, last)["sent"] == 1
    assert sub_deliveries().get().dedupe_key.endswith(f":{WEDNESDAY.isoformat()}")


def test_a_private_views_name_query_and_link_never_reach_an_admin_in_the_log(a, api):
    org = a["org"]
    secret_name = "Viewer secret shortlist"
    mine = view(a, name=secret_name, shared=False, owner="viewer", query={"q": "orga"})
    sub = subscribe(a, mine, "viewer", weekday=WEDNESDAY.weekday())
    assert subscriptions.run_for_org(org, at(org, WEDNESDAY, 8, 26))["sent"] == 1
    drain()
    assert secret_name in mail.outbox[0].body  # the owner's own email names it
    delivery = sub_deliveries().get()
    assert delivery.subject == "Saved view subscription (1 application)"
    admin = api(a["users"]["admin"])
    listed = admin.get("notification-log?kind=subscription").json_body
    detail = admin.get(f"notification-log/{delivery.id}").json_body
    for body in (listed, detail):
        dumped = str(body)
        assert secret_name not in dumped and str(mine.id) not in dumped and "q=orga" not in dumped
    assert detail["content"]["redacted"] and detail["content"]["text"] == ""
    # A skipped email of a private view doesn't name it either, nor say what its filter is.
    SavedFilter.objects.filter(id=mine.id).update(query={"cf_gone": "x"})
    subscriptions.run_for_org(org, at(org, WEDNESDAY + timedelta(days=7), 8, 26))
    skipped = sub_deliveries().filter(status="skipped").get()
    assert secret_name not in skipped.subject and "cf_gone" not in skipped.last_error
    assert sub.id  # the subscription itself is untouched


def test_an_admins_own_private_view_reads_whole_in_the_log(a, api):
    org = a["org"]
    mine = view(a, name="Admin shortlist", shared=False, owner="admin")
    subscribe(a, mine, "admin", weekday=WEDNESDAY.weekday())
    subscriptions.run_for_org(org, at(org, WEDNESDAY, 8, 26))
    delivery = sub_deliveries().get()
    detail = api(a["users"]["admin"]).get(f"notification-log/{delivery.id}").json_body
    assert "Admin shortlist" in detail["content"]["text"]


# The keys the SPA list applies from its URL: apps/web/src/features/catalog/catalog-list.tsx RAW_KEYS (SHARED_KEYS,
# KIND_KEYS, COMPLIANCE_KEYS, QUALITY_KEYS) plus q and tag. Change both together.
SPA_SHARED = {"lifecycle", "archived", "gap", "review_state", "imported", "criticality", "department"}
SPA_SHARED |= {"data_classification", "is_gxp", "validation_status", "risk_tier"}
SPA_SHARED |= {"owner", "support_team", "it_steward", "periodic_review_due"}  # chips
SPA_SHARED |= {"completeness__lt", "completeness__gte", "quality_rule", "q", "tag"}
SPA_KIND = {
    "application": {"category", "deployment_model", "vendor", "authentication_method", "sso_enabled"}
    | {"scim_provisioning"},
    "component": {"component_type", "hosting", "environment"},
    "ai_agent": {"platform", "channel", "risk_classification", "human_in_loop", "actions_can_write", "model"}
    | {"provider", "auth_mode", "environment"},
}


@pytest.mark.parametrize("kind", ["application", "component", "ai_agent"])
def test_saved_view_query_keys_are_exactly_what_the_spa_list_applies(kind):
    from catalog import saved_views

    assert saved_views.query_keys(kind) == SPA_SHARED | SPA_KIND[kind]


@pytest.mark.parametrize("key", ["owner", "support_team", "it_steward"])
def test_owner_team_and_steward_filters_can_be_saved_and_are_applied_by_the_email(wednesday, api, key):
    """The list's chips filter by these, so a view holds them and its email applies them."""
    f = wednesday
    payroll = Resource.objects.get(organization=f["org"], name="Payroll")
    value = {
        "owner": str(f["people"]["dana"].id),
        "support_team": str(f["team"].id),
        "it_steward": str(f["users"]["editor"].id),
    }[key]
    column = {"owner": "business_owner_id", "support_team": "support_team_id", "it_steward": "it_steward_id"}[key]
    Resource.objects.filter(id=payroll.id).update(**{column: int(value) if key == "it_steward" else value})
    saved = api(f["users"]["editor"]).post("saved-filters", {"name": "X", "kind": "application", "query": {key: value}})
    assert saved.status_code == 201, saved.json_body
    SavedFilter.objects.filter(id=f["view"].id).update(query={key: value})
    listed = api(f["users"]["editor"]).get(f"saved-filters/{f['view'].id}").json_body
    assert listed["problems"] == []
    assert _run_wednesday(f)["sent"] == 1


def test_a_filter_the_list_page_ignores_cant_be_saved_and_a_stored_one_is_a_problem(wednesday, api):
    f = wednesday
    key = "ordering_hint"  # not a list parameter the SPA applies
    refused = api(f["users"]["editor"]).post("saved-filters", {"name": "X", "kind": "application", "query": {key: "x"}})
    assert refused.status_code == 400 and refused.json()["errors"]["query"][0].startswith(f"{key}:")
    SavedFilter.objects.filter(id=f["view"].id).update(query={key: "x"})
    listed = api(f["users"]["editor"]).get(f"saved-filters/{f['view'].id}").json_body
    assert any(p.startswith(f"{key}:") for p in listed["problems"])
    assert _run_wednesday(f)["skipped"] == 1 and "filter has problems" in _skipped(f).last_error


def test_periodic_review_due_is_held_like_the_other_compliance_keys(wednesday, api):
    from catalog import saved_views

    f = wednesday
    assert "periodic_review_due" in saved_views.query_keys("application")
    editor = api(f["users"]["editor"])
    due = editor.post("saved-filters", {"name": "X", "kind": "application", "query": {"periodic_review_due": "true"}})
    gxp = editor.post("saved-filters", {"name": "Y", "kind": "application", "query": {"is_gxp": "true"}})
    assert due.status_code == gxp.status_code, (due.json(), gxp.json())


def test_run_list_applies_every_permission_class_of_the_list(a, monkeypatch):
    from rest_framework.permissions import BasePermission

    from catalog import saved_views
    from catalog.viewsets import ApplicationViewSet

    class Closed(BasePermission):
        message = "Closed for maintenance."

        def has_permission(self, request, view):
            return False

    membership = Membership.objects.get(user=a["users"]["viewer"], organization=a["org"])
    assert saved_views.run_list(membership, "application", {}).total == 1
    monkeypatch.setattr(ApplicationViewSet, "permission_classes", [*ApplicationViewSet.permission_classes, Closed])
    with pytest.raises(saved_views.NotAllowed, match="Closed for maintenance"):
        saved_views.run_list(membership, "application", {})


def test_a_view_whose_owner_left_cant_be_made_private(a, api):
    saved = view(a, shared=True, owner="editor")
    Membership.objects.filter(user=a["users"]["editor"], organization=a["org"]).delete()
    refused = api(a["users"]["admin"]).patch(f"saved-filters/{saved.id}", {"shared": False})
    assert refused.status_code == 400 and "no longer a member" in refused.json()["errors"]["shared"][0]
    saved.refresh_from_db()
    assert saved.shared


def test_deleting_a_user_keeps_their_shared_views_and_removes_their_private_ones(a):
    shared, private = view(a, shared=True, owner="editor"), view(a, name="Mine", shared=False, owner="editor")
    a["org"].coordinator = None
    a["org"].save()
    Resource.objects.filter(it_steward=a["users"]["editor"]).update(it_steward=None)
    a["users"]["editor"].delete()
    shared.refresh_from_db()
    assert shared.user_id is None and shared.shared
    assert not SavedFilter.objects.filter(id=private.id).exists()


def test_audit_labels_say_saved_view():
    from catalog.audit_log import LABELS as ACTION_LABELS
    from catalog.audit_log import TARGET_TYPES

    assert ACTION_LABELS["saved_filter.created"] == "Saved view added"
    assert ACTION_LABELS["saved_filter.updated"] == "Saved view changed"
    assert ACTION_LABELS["saved_filter.deleted"] == "Saved view deleted"
    assert TARGET_TYPES["SavedFilter"] == "Saved view"


@pytest.mark.parametrize(
    "kind,query,path,extra",
    [
        ("component", {"component_type": "scheduled_job"}, "/applications", {"kind": "components"}),
        ("ai_agent", {"platform": "copilot_studio"}, "/ai-agents", {}),
        ("application", {"cf_tier": "Gold", "cf_seats__gte": "3", "tag": "Finance"}, "/applications", {}),
    ],
)
def test_the_link_opens_the_right_list_with_the_views_query(a, kind, query, path, extra):
    from catalog import saved_views

    if kind == "application":
        for name, type_ in (("tier", "select"), ("seats", "integer")):
            body = {"name": name, "label": name.title(), "type": type_, "applies_to": ["application"]}
            if type_ == "select":
                body["choices"] = ["Gold", "Silver"]
            customfields.create_field(a["org"], a["users"]["admin"], body)
    saved = view(a, name=f"Link {kind}", kind=kind, query=query)
    parts = urllib.parse.urlsplit(saved_views.list_link(saved))
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE}{path}"
    got = dict(urllib.parse.parse_qsl(parts.query))
    assert got.pop("preset") == "all" and got.pop("view") == str(saved.id)
    assert got == {**extra, **query}


# ---- migration ---------------------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_shared_filters_become_shared_views_owned_by_their_creator():
    """catalog 0039: a filter with no user (shared) becomes `shared` and owned by its creator; personal ones stay."""
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("catalog", "0038_snapshotlink_type_names")])
    old = executor.loader.project_state([("catalog", "0038_snapshotlink_type_names")]).apps
    Org, User, Old = (
        old.get_model("core", "Organization"),
        old.get_model("auth", "User"),
        old.get_model("catalog", "SavedFilter"),
    )
    org = Org.objects.create(name="Upgraded views (synthetic)", slug="upgraded-views-synthetic")
    user = User.objects.create(username="sv@upgraded.test", email="sv@upgraded.test")
    shared = Old.objects.create(organization=org, user=None, kind="application", name="Team", query={}, created_by=user)
    orphan = Old.objects.create(organization=org, user=None, kind="application", name="Old", query={})
    personal = Old.objects.create(
        organization=org, user=user, kind="application", name="Mine", query={}, created_by=user
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        New = new.get_model("catalog", "SavedFilter")
        rows = {r.id: r for r in New.objects.filter(organization_id=org.id)}
        assert rows[shared.id].shared and rows[shared.id].user_id == user.id and rows[shared.id].columns == []
        assert rows[orphan.id].shared and rows[orphan.id].user_id is None
        assert not rows[personal.id].shared and rows[personal.id].user_id == user.id
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_savedfilter WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM notify_notificationrule WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_relationshiptype WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_softwarecategory WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])
            cursor.execute("DELETE FROM auth_user WHERE id = %s", [user.id])
