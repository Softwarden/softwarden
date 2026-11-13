"""One JSON object per line on stdout, for observability. Messages must never carry secrets or personal data.

Each line has `ts`, `level`, `logger` and `message`, then whichever of these apply:
- `request_id`: the request's id (X-Request-ID), the job's id inside a job, the run's id inside a schedule run;
- `org` and `user`: ids only, never a name or an email address;
- `path` and `method` on request lines; `status` and `duration_ms` on the `softwarden.request` line;
- `exc`: the traceback.
They come from core/request_context.log_fields() and from a record's own attributes (`extra=`).

Every text field is scrubbed before it is written (scrub()): query strings are cut from anything that looks like a
path, since the renewals calendar feed carries its secret in one (`?token=`) and runserver's request lines and some
exception messages would print it; every configured secret (Django's key, the database password, the credential
and backup keys, the ops token, core/jobs._secret_values) is replaced, also percent-encoded, JSON- or repr-escaped
and (for a key) in standard base64; and PATTERNS replace what has a secret's shape whatever its value:
URL userinfo, the path and fragment of an absolute URL (a webhook URL's secret), Bearer/Basic credentials (16+), this
product's `sw_`/`swc_`/`scim_` tokens, `sk-ant-` keys, `name=value` for secret-sounding names and `"password": "..."`.
SOFTWARDEN_LOG_FORMAT=text gives one readable line instead, scrubbed the same way.

Gunicorn's own lines (gunicorn.conf.py) use these formatters too, before Django is set up: then the request fields
are empty and only the query strings are scrubbed."""

import json
import logging
import re
import threading
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

# A path followed by a query string: "/x/y?token=..." keeps "/x/y". No query parameter is worth the risk of the one
# that is a secret. Text with a "?" but no path before it ("Is it ready?") is left alone.
QUERY = re.compile(r"(/[^\s?\"'<>]*)\?[^\s\"'<>]*")
FIELDS = ("request_id", "org", "user", "path", "method")
REDACTED = "[redacted]"
SECRETS_TTL = 60.0  # seconds the configured secret values are kept before they are read again

_secrets: tuple[float, list[str]] | None = None
_reading = threading.local()


def forget_secrets() -> None:
    """Read the configured secrets again on the next line (tests that change them)."""
    global _secrets
    _secrets = None


def _secret_values() -> list[str]:
    """core/jobs._secret_values, kept SECRETS_TTL seconds; [] before Django is ready, or while being read (the
    reading could log)."""
    global _secrets
    now = time.monotonic()
    if _secrets is not None and now - _secrets[0] < SECRETS_TTL:
        return _secrets[1]
    if getattr(_reading, "active", False):
        return []
    try:
        from django.apps import apps

        if not apps.ready:
            return []
        _reading.active = True
        from core import jobs

        values = jobs._secret_values()
    except Exception:  # a log line must never fail because a key file is unreadable
        return []
    finally:
        _reading.active = False
    _secrets = (now, values)
    return values


BARE_SECRET_NAMES = "sig|key|code|state|pwd|assertion|samlresponse"  # noqa: S105  (parameter names, not a secret)
# Pattern rules: secrets the configured-value list can't know, in the shapes a library
# message or a future log line would carry them. Applied in this order, after the query strings are cut.
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL userinfo: scheme://user:password@host (a DSN, an SMTP or proxy URL), whatever is encoded in it.
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/?#@\"'<>]+@", re.IGNORECASE), rf"\1{REDACTED}@"),
    # The path and fragment of an absolute http(s) URL: a stored webhook or Teams URL carries its secret in the path,
    # an invitation link in the fragment, and neither can be told apart by shape. Scheme, host and port stay.
    (re.compile(r"\b(https?://[^\s/?#\"'<>]+)[/#][^\s\"'<>]+", re.IGNORECASE), rf"\1/{REDACTED}"),
    # Authorization schemes as an Authorization header writes them: Bearer or Basic (base64 of user:password),
    # capitalized, followed by a credential of 16 characters or more. "Basic settings saved" and "bearer of bad news"
    # are prose and stay.
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{16,}"), rf"\1 {REDACTED}"),
    # This product's token formats (API, calendar feed, SCIM) and Anthropic API keys, wherever they appear.
    (re.compile(r"\b(?:sw|swc|scim)_[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{4,}"), REDACTED),
    # name=value where the name says secret (a form body, a query without a path, a DSN keyword): any name ending in
    # token, secret, password or signature.
    (
        re.compile(
            r"\b([\w.\-]*(?:token|secret|password|passwd|signature|api_key|apikey))=[^\s&\"'<>]+",
            re.IGNORECASE,
        ),
        rf"\1={REDACTED}",
    ),
    # The OAuth and SAML parameters by their bare names (sig, key, code, state, pwd, ...), which are also ordinary
    # words in log lines ("state=failed", "exit code=1"): only as a query parameter (after ? or &), or elsewhere when
    # the value looks like a credential (8 characters or more, with a digit or a symbol).
    (
        re.compile(rf"(?<=[?&])({BARE_SECRET_NAMES})=[^\s&\"'<>]+", re.IGNORECASE),
        rf"\1={REDACTED}",
    ),
    (
        re.compile(
            rf"(?<![\w.\-])({BARE_SECRET_NAMES})=(?=[^\s&\"'<>]{{8,}})(?=[^\s&\"'<>]*[^A-Za-z\s&\"'<>])[^\s&\"'<>]+",
            re.IGNORECASE,
        ),
        rf"\1={REDACTED}",
    ),
    # "password": "value" and 'secret': 'value' (JSON, a dict's repr).
    (
        re.compile(
            r"""(["'][\w\-]*(?:token|secret|password|passwd|api_key|apikey|private_key)["']\s*:\s*["'])[^"']+""",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
)


def _encoded_forms(value: str) -> set[str]:
    """`value` as it can appear inside other text: itself, percent-encoded (a DSN or a query), JSON- and repr-escaped,
    and a URL-safe base64 key in standard base64."""
    forms = {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        json.dumps(value)[1:-1],
        repr(value)[1:-1],
        value.replace("-", "+").replace("_", "/"),
    }
    return {f for f in forms if len(f) >= 6}


def scrub(text: str) -> str:
    """`text` without query strings, configured secrets (also encoded) or anything a PATTERNS rule recognizes."""
    text = QUERY.sub(r"\1", text)
    for value in _secret_values():
        for form in sorted(_encoded_forms(value), key=len, reverse=True):
            if form in text:
                text = text.replace(form, REDACTED)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _context() -> dict[str, str]:
    try:
        from core.request_context import log_fields
    except Exception:  # gunicorn's master logs before Django is importable
        return {}
    return log_fields()


def fields(record: logging.LogRecord) -> dict[str, Any]:
    """The request fields of a line: the current context, overridden by the record's own attributes."""
    found: dict[str, Any] = {}
    context = _context()
    for name in FIELDS:
        value = getattr(record, name, None)
        if value is None:
            value = context.get(name)
        if value is not None:
            found[name] = scrub(str(value))
    status = getattr(record, "status_code", None)
    if status is not None:
        found["status"] = status
    duration = getattr(record, "duration_ms", None)
    if duration is not None:
        found["duration_ms"] = duration
    return found


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
            **fields(record),
        }
        if record.exc_info:
            entry["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    """SOFTWARDEN_LOG_FORMAT=text: `time level logger message key=value ...`, for reading locally."""

    def format(self, record):
        when = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        extra = " ".join(f"{k}={v}" for k, v in fields(record).items())
        line = f"{when} {record.levelname} {record.name} {scrub(record.getMessage())}"
        if extra:
            line = f"{line} [{extra}]"
        if record.exc_info:
            line = f"{line}\n{scrub(self.formatException(record.exc_info))}"
        return line
