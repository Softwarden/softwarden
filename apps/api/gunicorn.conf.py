"""Gunicorn settings for the container. Gunicorn's own lines use the same format as Django's (softwarden/logs.py)."""

import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("SOFTWARDEN_WORKERS", "3"))
# Threaded workers: with sync workers one slow request held a whole process, so three slow
# clients stopped the app. Each thread has its own database connection (Django opens one per thread), so the default
# 3 x 8 can use 24 of PostgreSQL's 100. The front proxy must still buffer request bodies (Caddy's request_buffers;
# on Helm the ingress, deploy/helm/softwarden/README.md), or enough slow uploads would hold every thread.
worker_class = "gthread"
threads = int(os.environ.get("SOFTWARDEN_THREADS", "8"))
# With gthread workers this bounds the worker process, not a request: a process whose main loop stops answering
# gunicorn's heartbeat for this long is restarted. A single long request (a 5,000-row CSV import commit runs in-request,
# about 40 s on the dev machine) is no longer cut off at 120 s; Caddy's `write` timeout (10 min) is the outer
# bound for a request through the proxy.
timeout = 120
# No gunicorn access log: the app writes one `softwarden.request` line per request instead (core/request_context.py),
# with the request id, organization and user ids, status and duration, and the path without its query string (the
# calendar feed's secret travels in one). `accesslog = None` alone isn't enough: gunicorn writes access lines to the
# `gunicorn.access` logger whenever logconfig_dict is set, so that logger goes to a NullHandler below, at CRITICAL so no
# line is even formatted (otherwise every request was logged twice, the second time with its Referer and
# User-Agent, plus the health probe every 10 s; tests/test_gunicorn_conf.py).
accesslog = None
_format = "text" if (os.environ.get("SOFTWARDEN_LOG_FORMAT") or "").strip().lower() == "text" else "json"
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "softwarden.logs.JsonFormatter"},
        "text": {"()": "softwarden.logs.TextFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": _format},
        "discard": {"class": "logging.NullHandler"},
    },
    "loggers": {
        "gunicorn.error": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "gunicorn.access": {"handlers": ["discard"], "level": "CRITICAL", "propagate": False},
    },
}

# gunicorn 26 opens a control socket under $HOME by default; nothing uses gunicornc, and the Helm pods run with a
# read-only root filesystem, so it would only log an error at start.
control_socket_disable = True
