"""Browser security headers the app sends itself, so every deployment has them, not only the
Compose stack behind Caddy: the Helm chart, or any other proxy, serves exactly what Django answers.

- X-Frame-Options: DENY (Django's XFrameOptionsMiddleware, settings.X_FRAME_OPTIONS) and `frame-ancestors 'none'` in the
  CSP: no page of the app can be framed, so destructive Admin screens can't be clickjacked.
- Content-Security-Policy: SPA_CSP on every response that set none of its own. Responses with their own policy keep it,
  alone: attachment downloads (the sandbox, catalog/attachment_viewsets.py), and the nonce policies of the optional
  `ee` package's SAML pages.
- Permissions-Policy: camera, microphone and geolocation off, unless the response set one.
- Strict-Transport-Security: Django's SecurityMiddleware, on HTTPS requests only, when SOFTWARDEN_HSTS_SECONDS is set
  (settings.SECURE_HSTS_SECONDS). Compose leaves it unset because Caddy sends HSTS for a real host name and never for
  localhost; the Helm chart sets it when the ingress has TLS.
- nosniff, Referrer-Policy and Cross-Origin-Opener-Policy: Django's SecurityMiddleware, as before.
- Cache-Control: `private, no-store` on every /api/, /scim/ and /healthz/ops response that set none.

docker/Caddyfile sends the same CSP and Permissions-Policy with `?` (only when the response has none), so on Compose
they are a second layer that now covers Caddy's own responses; tests/test_security_headers.py keeps the values equal.
The SPA has no inline script (index.html loads theme-init.js as a file); 'unsafe-inline' styles are for the <style>
elements Radix and Scalar inject at run time.
"""

SPA_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
# API and SCIM answers (catalog data, exports, problems) are never kept by a browser's
# disk cache or a caching intermediary. A view that sets its own Cache-Control keeps it.
NO_STORE = "private, no-store"
NO_STORE_PREFIXES = ("/api/", "/scim/", "/healthz/ops")


class SecurityHeadersMiddleware:
    """Adds SPA_CSP and PERMISSIONS_POLICY to a response that has none. Listed before WhiteNoise, so the files it
    serves (the SPA's index.html at /) get them too."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = SPA_CSP
        if "Permissions-Policy" not in response:
            response["Permissions-Policy"] = PERMISSIONS_POLICY
        if "Cache-Control" not in response and getattr(request, "path", "").startswith(NO_STORE_PREFIXES):
            response["Cache-Control"] = NO_STORE
        return response
