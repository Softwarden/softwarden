"""The largest multipart request the app reads, on any path (attachments review).

A multipart body is parsed (to memory or temporary files) by whoever first touches `request.POST` or `request.FILES`;
for a session client that is CsrfViewMiddleware, before authentication, the role check, the upload throttle or any
view code, and on any path (the SPA catch-all included). So this middleware, placed before WhiteNoise and CSRF, answers
every multipart request, whatever its path or method, before anything reads it:

- 411 when it has no valid Content-Length (Django reads at most Content-Length bytes, so the header is the cap);
- 413 when Content-Length is over settings.MAX_REQUEST_MB (SOFTWARDEN_MAX_REQUEST_MB, default the attachment limit
  plus 1 MB for the multipart framing). Caddy's `request_body max_size` uses the same variable in Compose.

Any other body (JSON) is refused with 413 when its Content-Length is over settings.DATA_UPLOAD_MAX_MEMORY_SIZE (Django's
own limit, 2.5 MB unless set): DRF's JSON parser reads the stream directly, so Django's check never runs for it.

The organization's own, possibly smaller, attachment limit is checked by the upload view and while the file is read
(catalog/attachments.py). Both refusals are problem details (core/problems.py)."""

from django.conf import settings
from django.http import JsonResponse

from .problems import CONTENT_TOO_LARGE, LENGTH_REQUIRED, problem_response

MB = 1024 * 1024


def request_limit_bytes() -> int:
    return settings.MAX_REQUEST_MB * MB


def body_limit_bytes() -> int | None:
    return settings.DATA_UPLOAD_MAX_MEMORY_SIZE


def _problem(status: int, problem_type: str, detail: str) -> JsonResponse:
    # This middleware answers before SecurityMiddleware runs; problem_response sets the nosniff header it would add.
    return problem_response(status, detail, problem_type)


class RequestSizeLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.META.get("CONTENT_LENGTH") or ""
        if request.META.get("CONTENT_TYPE", "").lower().startswith("multipart/"):
            if not raw.isdigit():
                return _problem(411, LENGTH_REQUIRED, "Send the upload with a Content-Length header.")
            if int(raw) > request_limit_bytes():
                return _problem(
                    413,
                    CONTENT_TOO_LARGE,
                    f"The upload is larger than this installation accepts ({settings.MAX_REQUEST_MB} MB).",
                )
        elif raw.strip().isdigit() and (limit := body_limit_bytes()) is not None and int(raw.strip()) > limit:
            return _problem(
                413,
                CONTENT_TOO_LARGE,
                f"The request body is larger than this installation accepts ({limit // 1024} KB). Uploads go as "
                "multipart/form-data, and long lists in smaller batches.",
            )
        return self.get_response(request)
