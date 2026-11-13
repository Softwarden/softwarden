"""Every routed view declares its minimum role (CI gate: URL coverage). The
declaration is the enforcement: `min_role`/`public` on a function view or
`role_policy` on a viewset binds core.drf.RolePermission, and a DRF view without one is refused by the default
permission (core.permissions.DenyUndeclared). Public routes must be exactly the hand-kept list in
tests/test_permissions.py PUBLIC, so making a route public is a visible decision."""

from django.conf import settings
from django.test import RequestFactory
from django.urls import URLPattern, URLResolver, get_resolver, resolve
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.drf import LEVELS, PLATFORM, PUBLIC, PUBLIC_POLICY, declared_policy, min_role
from core.permissions import DenyUndeclared
from tests import test_permissions

# Routes without a session role: the public ones, and the operator health view (its own token or an Admin session,
# softwarden/ops.py), which is `public` to the role machinery. tests/test_permissions.py keeps the lists apart.
PUBLIC_LIST = test_permissions.PUBLIC + test_permissions.OPS_TOKEN_ROUTES

IGNORED_METHODS = {"options", "head"}


def walk(patterns, prefix=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from walk(pattern.url_patterns, prefix + str(pattern.pattern))
        elif isinstance(pattern, URLPattern):
            yield prefix + str(pattern.pattern), pattern.callback


def methods_and_actions(callback):
    """[(method, action or None)] a DRF route answers."""
    actions = getattr(callback, "actions", None)
    if actions:  # a viewset route: as_view({"get": "list", ...})
        return [(m, a) for m, a in actions.items() if m not in IGNORED_METHODS]
    cls = callback.cls
    return [(m, None) for m in cls.http_method_names if m not in IGNORED_METHODS and hasattr(cls, m)]


def declarations():
    """{(method or "*", route): role} for every route, and the problems found."""
    found, problems = {}, []
    for route, callback in walk(get_resolver().url_patterns):
        name = f"{callback.__module__}.{callback.__name__} ({route})"
        cls = getattr(callback, "cls", None)
        if cls is None:  # a plain Django view (probes, the SPA): no DRF auth at all, so only `public` is possible
            if getattr(callback, "role_policy", None) != PUBLIC_POLICY:
                problems.append(f"{name} is a plain Django view; mark it @public or make it a DRF view with a role")
            found["*", route] = PUBLIC
            continue
        policy = declared_policy(callback)
        if policy is None:
            problems.append(f"{name} declares no minimum role (min_role, public or role_policy)")
            continue
        for method, action in methods_and_actions(callback):
            role = policy.role_for(method, action)
            if role not in LEVELS:
                problems.append(f"{name} {method.upper()} has no valid role")
            found[method, route] = role
    return found, problems


def test_every_route_declares_its_minimum_role():
    found, problems = declarations()
    assert not problems, "\n".join(problems)
    assert len(found) > 100  # the walk saw the whole API, not an empty resolver


SPA_ROUTE = resolve("/").route


def public_routes_from_the_list():
    pairs = set()
    for method, url in PUBLIC_LIST:
        path = url if url.startswith("/") else "/api/v1/" + url.split("?")[0]
        route = resolve(path).route
        # Any unknown path falls through to the SPA, so a stale entry would pass silently; only "/" may.
        assert route != SPA_ROUTE or url == "/", f"PUBLIC entry {url} only matches the SPA catch-all"
        pairs.add((method, route))
    return pairs


def test_public_routes_are_exactly_the_public_list():
    found, _ = declarations()
    declared = {key for key, role in found.items() if role == PUBLIC}
    listed = public_routes_from_the_list()
    # Plain Django views answer whichever methods they choose; the list names one per route.
    plain = {route for method, route in declared if method == "*"}
    assert {(m, r) for m, r in listed if r not in plain} == {(m, r) for m, r in declared if m != "*"}
    assert {r for _, r in listed if r in plain} == plain


def test_platform_routes_are_exactly_the_platform_list():
    """`platform` is declared by exactly the routes under /api/v1/platform/, every method of them, and they are the
    hand-kept list in tests/test_permissions.py PLATFORM_ROUTES (platform admin console)."""
    found, _ = declarations()
    declared = {key for key, role in found.items() if role == PLATFORM}
    under = {key for key in found if key[1].startswith("api/v1/platform/")}
    assert declared == under and declared
    sample = "00000000-0000-0000-0000-000000000000"
    listed = set()
    for method, url in test_permissions.PLATFORM_ROUTES:
        route = resolve("/api/v1/" + url.format(org=sample, override=sample)).route
        assert route != SPA_ROUTE, url
        listed.add((method, route))
    assert listed == declared


def test_undeclared_views_fail_closed():
    """The default permission refuses a DRF view that declares nothing, even for a signed-in Admin."""
    assert settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == ["core.permissions.DenyUndeclared"]

    @api_view(["GET"])
    def forgotten(request):
        return Response({"ok": True})

    assert declared_policy(forgotten.cls) is None
    assert forgotten.cls.permission_classes == [DenyUndeclared]
    response = forgotten(RequestFactory().get("/x"))
    assert response.status_code == 403


def test_min_role_goes_under_api_view():
    @api_view(["GET"])
    def view(request):
        return Response()

    try:
        min_role("viewer")(view)
    except TypeError as e:
        assert "below" in str(e)
    else:
        raise AssertionError("min_role above @api_view must be refused")


def test_test_only_routes_stay_out_of_the_shipped_urlconf():
    """tests/capability_urls.py (gated capabilities without a real endpoint) is never part of the real routes."""
    assert settings.ROOT_URLCONF == "softwarden.urls"
    routes = [route for route, _ in walk(get_resolver().url_patterns)]
    assert not [r for r in routes if "_test" in r]
    assert all(callback.__module__ != "tests.capability_urls" for _, callback in walk(get_resolver().url_patterns))


def test_playwright_only_routes_declare_their_role_too():
    """api/e2e_views.py is routed only under SOFTWARDEN_E2E with an _e2e database, so the walk above can't see it."""
    from api import e2e_views

    # @api_view functions carry .cls (their APIView); the module's imports (decorators, models) don't.
    views = {name: v for name, v in vars(e2e_views).items() if callable(v) and hasattr(v, "cls")}
    assert set(views) >= {
        "run_digest",
        "run_connectors",
        "multi_org_user",
        "sso_seed",
        "system_states",
        "enterprise_org",
        "custom_fields_org",
        "spec_org",
        "outbox",
        "expire_impersonation",
        "run_export_jobs",
    }
    # Ending an impersonation is asked by the member impersonated, who may be a Viewer; every other route is Admin.
    any_role = {"expire_impersonation"}
    for name, view in views.items():  # every view in the module, so a new one can't skip the check
        policy = declared_policy(view.cls)
        assert policy is not None and policy.role_for("post") == ("viewer" if name in any_role else "admin"), name
