from django.urls import include, path, re_path

from . import ee, ops

urlpatterns = [
    path("api/v1/", include("api.urls")),
    path("healthz", ops.healthz),
    # The operator health view: the ops token or an Admin session, otherwise 404 (softwarden/ops.py).
    path("healthz/ops", ops.healthz_ops),
    path("readyz", ops.readyz),
    path("readyz/web", ops.readyz_web),
    # SCIM provisioning from the optional `ee` package (capability scim): bearer-token only, outside /api/v1/.
    *([path("", include("ee.scim.urls"))] if ee.INSTALLED else []),
    # Everything else is a client-side route; an unknown /api path is the not-found problem (handler404).
    re_path(r"^(?!api/)(?P<path>.*)$", ops.spa),
]

# Under /api/, what Django answers itself is a problem too (core/problems.py); other paths keep Django's pages.
handler400 = "core.problems.bad_request"
handler403 = "core.problems.permission_denied"
handler404 = "core.problems.page_not_found"
handler500 = "core.problems.server_error"
