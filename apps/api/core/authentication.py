"""Session authentication for the SPA (settings.REST_FRAMEWORK). DRF's own, except that a CSRF failure is the
`/problems/csrf-failed` problem instead of a generic 403, so a client can tell a missing X-CSRFToken header from a
role refusal. Kept apart from core/drf.py for the same reason as core/permissions.py: DRF imports it while loading
APIView, so nothing here may import DRF's views at module level."""

from rest_framework import authentication, exceptions


class SessionAuthentication(authentication.SessionAuthentication):
    def enforce_csrf(self, request):
        try:
            super().enforce_csrf(request)
        except exceptions.PermissionDenied as e:
            from .problems import CsrfFailed

            reason = str(e.detail).removeprefix("CSRF Failed: ")
            raise CsrfFailed(reason) from None
