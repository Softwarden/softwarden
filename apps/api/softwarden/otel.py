"""Optional OpenTelemetry trace export.

Off unless both hold:
- OTEL_EXPORTER_OTLP_ENDPOINT is set (for example http://otel-collector:4318), and
- the `otel` extra is installed (`uv sync --extra otel`; the image with `--build-arg WITH_OTEL=1`).

Otherwise nothing from opentelemetry is imported and every function here is a no-op; the default image doesn't carry
the packages. When on, the web processes (softwarden/wsgi.py, one provider per gunicorn worker, after the fork, and
before the middleware chain is built) trace each request through the Django instrumentation and each query through
the psycopg one, and the worker (run_worker) traces each job as a `job <kind>` span with its queries under it. Every
request and job span carries `softwarden.request_id`, the id in the X-Request-ID header and on the log lines and audit
events, so a trace and its log lines meet. In the worker a query outside any job (the tick's own polling) is not
traced, so the polling doesn't become a root trace every few seconds.

The exporter is OTLP over HTTP (protobuf), so the standard OTEL_EXPORTER_OTLP_* variables (headers, timeout,
certificate, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) and OTEL_TRACES_SAMPLER apply as the SDK documents them. The service
name is OTEL_SERVICE_NAME, else `softwarden-web` or `softwarden-worker`.

Nothing that could hold a secret leaves the process. Every exported span goes through ScrubbingExporter, which applies
softwarden.logs.scrub (query strings cut, configured secrets replaced) to the span's name, every string attribute,
every event's attributes (an exception's message and stack trace) and the status description, and empties `url.query`.
The request hooks also cut the query from the URL attributes as soon as the span starts. The psycopg spans carry the
statement with its `%s` placeholders, never the parameters (the instrumentation's SQL commenter stays off)."""

import logging
import os
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any

log = logging.getLogger(__name__)

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
REQUEST_ID = "softwarden.request_id"
# Request span attributes that can carry the query string, by semantic-conventions generation.
URL_ATTRIBUTES = ("http.target", "http.url", "url.full")
QUERY_ATTRIBUTES = ("url.query",)

_tracer: Any = None
_configured = False


def enabled() -> bool:
    return bool((os.environ.get(ENDPOINT) or "").strip())


def _scrub(text: str) -> str:
    from softwarden.logs import scrub

    return scrub(text)


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v) if isinstance(v, str) else v for v in value)
    return value


def scrub_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """A span's or event's attributes as they may leave the process: softwarden.logs.scrub on every string, and the
    query attributes emptied."""
    cleaned = {}
    for name, value in (attributes or {}).items():
        cleaned[name] = "" if name in QUERY_ATTRIBUTES and value else scrub_value(value)
    return cleaned


def scrubbing_exporter(inner: Any) -> Any:
    """`inner` wrapped so every span is scrubbed (scrub_attributes) before it is exported."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Status

    def clean(span: Any) -> Any:
        status = span.status
        if status is not None and status.description:
            status = Status(status.status_code, _scrub(status.description))
        return ReadableSpan(
            name=_scrub(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=scrub_attributes(span.attributes),
            events=[Event(e.name, scrub_attributes(e.attributes), e.timestamp) for e in span.events],
            links=span.links,
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    class ScrubbingExporter(SpanExporter):
        def export(self, spans):
            return inner.export([clean(span) for span in spans])

        def shutdown(self):
            return inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return inner.force_flush(timeout_millis)

    return ScrubbingExporter()


def _worker_sampler() -> Any:
    """The configured sampler (OTEL_TRACES_SAMPLER), except that a client span with no parent (a query outside any
    job: the worker's polling) is dropped."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import sampling

    try:
        inner = sampling._get_from_env_or_default()  # the SDK's own reading of OTEL_TRACES_SAMPLER
    except Exception as error:  # a private SDK helper: a newer SDK may move it
        log.warning(
            "OTEL_TRACES_SAMPLER not read (%s); the worker samples every trace (always_on)", type(error).__name__
        )
        inner = sampling.DEFAULT_ON

    class DropOrphanQueries(sampling.Sampler):
        def should_sample(
            self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None
        ):
            parent = trace.get_current_span(parent_context).get_span_context()
            if kind == trace.SpanKind.CLIENT and not parent.is_valid:
                return sampling.SamplingResult(sampling.Decision.DROP)
            return inner.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

        def get_description(self):
            return f"DropOrphanQueries({inner.get_description()})"

    return DropOrphanQueries()


def configure(service: str) -> bool:
    """Set up export for this process (`web` or `worker`) when enabled. True when tracing is on. Never raises: a
    missing extra or a bad setting is one warning, and the app runs untraced. The web process must call it before the
    WSGI handler loads its middleware (softwarden/wsgi.py)."""
    global _tracer, _configured
    if _configured:
        return _tracer is not None
    _configured = True
    if not enabled():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning(
            "%s is set but the otel extra isn't installed; tracing is off (build the image with WITH_OTEL=1).", ENDPOINT
        )
        return False
    try:
        attributes = {} if os.environ.get("OTEL_SERVICE_NAME") else {"service.name": f"softwarden-{service}"}
        options: dict[str, Any] = {"resource": Resource.create(attributes)}
        if service == "worker":
            options["sampler"] = _worker_sampler()
        provider = TracerProvider(**options)
        provider.add_span_processor(BatchSpanProcessor(scrubbing_exporter(OTLPSpanExporter())))
        trace.set_tracer_provider(provider)
        if service == "web":
            from opentelemetry.instrumentation.django import DjangoInstrumentor

            DjangoInstrumentor().instrument(request_hook=_request_hook, response_hook=_response_hook)
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor().instrument(enable_commenter=False)
        _tracer = trace.get_tracer("softwarden")
    except Exception as error:  # a bad endpoint or option must not stop the app
        log.warning("OpenTelemetry setup failed (%s); tracing is off.", type(error).__name__)
        return False
    log.info("OpenTelemetry trace export is on (%s).", service)
    return True


def strip_query(value: str) -> str:
    return value.split("?", 1)[0]


def scrub_span(span: Any) -> None:
    """Remove the query string from a request span's URL attributes (set when the span started)."""
    attributes = getattr(span, "attributes", None) or {}
    for name in URL_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(name, strip_query(value))
    for name in QUERY_ATTRIBUTES:
        if attributes.get(name):
            span.set_attribute(name, "")


def _request_hook(span: Any, request: Any) -> None:
    if span is not None and span.is_recording():
        scrub_span(span)


def _response_hook(span: Any, request: Any, response: Any) -> None:
    """RequestIdMiddleware has run by now, so the request has its id."""
    if span is None or not span.is_recording():
        return
    scrub_span(span)
    request_id = getattr(request, "request_id", None)
    if request_id is not None:
        span.set_attribute(REQUEST_ID, str(request_id))


def job_span(job: Any) -> AbstractContextManager[Any]:
    """A span around one job (its id as softwarden.request_id), or nothing when tracing is off."""
    if _tracer is None:
        return nullcontext()
    return _tracer.start_as_current_span(
        f"job {job.kind}", attributes={REQUEST_ID: str(job.id), "softwarden.job.kind": job.kind}
    )
