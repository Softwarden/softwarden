"""The endoflife.date v1 API, read by the worker only (eol/cache.py). Checked against the published description
(https://endoflife.date/docs/api/v1/, OpenAPI 1.2.1) on 2026-11-09:

- `GET /api/v1/products`: every product as a summary (name, label, aliases, category, tags, uri), about 90 KB for 489
  products;
- `GET /api/v1/products/{product}`: one product with all its release cycles (`result.releases`) and `last_modified`;
- `GET /api/v1/products/full`: every product with its releases in one document, the snapshot `eol_import` loads.

Answers carry an ETag and `Cache-Control: public, max-age=0, must-revalidate`; a request with If-None-Match gets
304 when nothing changed, so a daily run that finds nothing new costs headers only. There is no Last-Modified header
(checked 2026-11-09), so only the ETag is sent back. The documented codes are 200, 301 (a renamed product: follow the
Location), 304, 404 (an HTML page, not JSON) and 429 (with Retry-After); Netlify, which hosts it, may add 502 and 503.
No rate limit is published beyond 429. The data is MIT-licensed.

Every request goes through core/outbound.request with the public-only (webhook) rule: https, a public host name,
every address it resolves to public, the connection pinned to the address checked, no redirect followed by the
transport, a wall-clock deadline and a body cap. Redirects are followed here, at most MAX_REDIRECTS, and only to the
same host. Retries: up to ATTEMPTS per URL for an unreachable host, a 429 or a 5xx, waiting BACKOFF seconds doubled each
time, or Retry-After when it is shorter than RETRY_AFTER_MAX. Tests replace `network` (tests/conftest.py refuses it)."""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from core import outbound
from core.graph import Reply, Unreachable

BASE_URL = "https://endoflife.date/api/v1"
HOST = "endoflife.date"
TIMEOUT = 15  # seconds per socket operation
DEADLINE = 45.0  # seconds for one whole request
LIST_MAX = 4 * 1024 * 1024  # the product list (about 90 KB in November 2026)
PRODUCT_MAX = 2 * 1024 * 1024  # one product (PostgreSQL about 10 KB, Ubuntu about 20 KB)
ATTEMPTS = 3
BACKOFF = 2.0  # seconds before the second attempt; doubled for the third
RETRY_AFTER_MAX = 60
MAX_REDIRECTS = 3
REDIRECTS = (301, 302, 307, 308)


class FetchError(Exception):
    """No usable answer. The message is safe to store and show: a status or the failure's type, never a body."""


class NotFound(FetchError):
    """404: endoflife.date has no such product."""


@dataclass(frozen=True)
class Fetched:
    """A 200 (`data` the parsed JSON, `etag` the ETag to send back) or a 304 (`not_modified`, no data)."""

    not_modified: bool
    data: Any = None
    etag: str = ""


def user_agent() -> str:
    return f"Softwarden/{settings.APP_VERSION} (+{settings.PROJECT_URL})"


def products_url() -> str:
    return f"{BASE_URL}/products"


def product_url(name: str) -> str:
    return f"{BASE_URL}/products/{urllib.parse.quote(name, safe='')}"


def network(url: str, headers: dict[str, str], body_max: int) -> Reply:
    """One GET through the SSRF guard (public hosts only). Replaced in tests."""
    return outbound.request("GET", url, None, headers, TIMEOUT, DEADLINE, private_ok=False, body_max=body_max)


def sleep(seconds: float) -> None:  # replaced in tests
    time.sleep(seconds)


def _retry_after(reply: Reply) -> float | None:
    raw = (reply.headers.get("retry-after") or "").strip()
    if raw.isdigit():
        return float(int(raw))
    return None


def _follow(url: str, reply: Reply) -> str:
    location = (reply.headers.get("location") or "").strip()
    if not location:
        raise FetchError(f"HTTP {reply.status} without a Location")
    target = urllib.parse.urljoin(url, location)
    parts = urllib.parse.urlsplit(target)
    if parts.scheme != "https" or (parts.hostname or "").lower() != HOST:
        raise FetchError(f"HTTP {reply.status} to another host, not followed")
    return target


def fetch(url: str, *, etag: str = "", body_max: int = PRODUCT_MAX) -> Fetched:
    """GET one document. Fetched on 200 and 304; NotFound on 404; FetchError for anything else once the retries are
    spent (an unreachable host, 429, 5xx) or straight away (another status, a body that isn't JSON or was cut at
    `body_max`)."""
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    redirects = 0
    attempt = 0
    while True:
        attempt += 1
        retry_in: float | None = None
        try:
            reply = network(url, headers, body_max)
        except outbound.UnsafeDestination as error:
            raise FetchError(f"refused: {error}") from None
        except Unreachable as error:
            if attempt >= ATTEMPTS:
                raise FetchError(f"unreachable ({error})") from None
            retry_in = BACKOFF * 2 ** (attempt - 1)
        else:
            if reply.status in REDIRECTS:
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise FetchError("too many redirects")
                url = _follow(url, reply)
                attempt -= 1  # a redirect isn't a failed attempt
                continue
            if reply.status == 304:
                return Fetched(not_modified=True, etag=etag)
            if reply.status == 200:
                if len(reply.body) >= body_max:
                    raise FetchError("the answer is larger than expected")
                try:
                    data = json.loads(reply.body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise FetchError("the answer isn't JSON") from None
                return Fetched(not_modified=False, data=data, etag=(reply.headers.get("etag") or "")[:200])
            if reply.status == 404:
                raise NotFound("not on endoflife.date (404)")
            if reply.status != 429 and reply.status < 500:
                raise FetchError(f"HTTP {reply.status}")
            if attempt >= ATTEMPTS:
                raise FetchError(f"HTTP {reply.status}")
            retry_in = BACKOFF * 2 ** (attempt - 1)
            after = _retry_after(reply)
            if after is not None:
                if after > RETRY_AFTER_MAX:
                    raise FetchError(f"HTTP {reply.status}, retry after {int(after)} s")
                retry_in = max(retry_in, after)
        sleep(retry_in)
