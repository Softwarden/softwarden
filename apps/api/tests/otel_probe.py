"""OpenTelemetry switched on, end to end (softwarden/otel.py). Needs the optional `otel`
extra, so it runs in its own process: tests/test_observability.py starts it in an environment with the extra (uv sync
--extra otel), and CI's WITH_OTEL=1 image leg runs the same file inside the built image, with the image's own packages:

  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 OTEL_BSP_SCHEDULE_DELAY=60000 SOFTWARDEN_OPS_TOKEN=<token> \\
    PYTHONPATH=<apps/api> python otel_probe.py web|worker

`web` drives softwarden.wsgi.application, as gunicorn does; `worker` traces a job, as the worker does. Spans go to an
in-memory exporter behind the scrubbing exporter (nothing is sent: port 9 is the discard port and the batch exporter
never flushes in time). It prints `RESULT <json>` and exits 0 only if check() passes. No database needed."""

import json
import os
import sys
import uuid
import wsgiref.util
from typing import Any


def run(service: str) -> dict:
    if service == "web":
        from softwarden import otel
        from softwarden.wsgi import application  # sets Django and tracing up, as gunicorn does

        assert otel._tracer is not None, "the web process didn't set tracing up"
    else:
        import django

        django.setup()
        from softwarden import otel

        assert otel.configure("worker") is True, "the worker didn't set tracing up"
    from opentelemetry import trace
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider: Any = trace.get_tracer_provider()  # the SDK provider otel.configure set up
    provider.add_span_processor(SimpleSpanProcessor(otel.scrubbing_exporter(exporter)))
    token = os.environ["SOFTWARDEN_OPS_TOKEN"]
    if service == "web":
        environ: dict = {}
        wsgiref.util.setup_testing_defaults(environ)
        environ.update(PATH_INFO="/healthz", QUERY_STRING="token=topsecret")
        headers: dict = {}

        def start(status, response_headers):
            headers.update(response_headers)

        wsgi: Any = application
        b"".join(wsgi(environ, start))
        request_id = headers["X-Request-ID"]
    else:

        class Job:
            id, kind = uuid.uuid4(), "test.kind"

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("orphan query", kind=trace.SpanKind.CLIENT):
            pass  # the worker's polling outside a job: dropped
        try:
            with otel.job_span(Job()):
                with tracer.start_as_current_span("query in job", kind=trace.SpanKind.CLIENT):
                    pass
                raise RuntimeError("failed with " + token + " at /f.ics?token=swc_secret")
        except RuntimeError:
            pass
        request_id = str(Job.id)
    spans = [
        {
            "name": s.name,
            "attributes": dict(s.attributes or {}),
            "status": s.status.description,
            "events": [dict(e.attributes or {}) for e in s.events],
        }
        for s in exporter.get_finished_spans()
    ]
    return {"request_id": request_id, "spans": spans}


def check(service: str, out: dict, token: str) -> None:
    """What tracing must do: the span carries the request id, and no query string or secret reaches it."""
    from softwarden import otel

    raw = json.dumps(out["spans"])
    if service == "web":
        (span,) = [s for s in out["spans"] if s["attributes"].get(otel.REQUEST_ID)]
        assert span["attributes"][otel.REQUEST_ID] == out["request_id"]
        assert "topsecret" not in raw
        return
    names = sorted(s["name"] for s in out["spans"])
    assert names == ["job test.kind", "query in job"], names  # the orphan query was dropped
    job = next(s for s in out["spans"] if s["name"] == "job test.kind")
    assert job["attributes"][otel.REQUEST_ID] == out["request_id"]
    assert job["attributes"]["softwarden.job.kind"] == "test.kind"
    assert job["events"] and "exception.message" in job["events"][0]
    assert token not in raw and "token=" not in raw and "swc_secret" not in raw
    assert "[redacted]" in job["events"][0]["exception.message"]


if __name__ == "__main__":
    service = sys.argv[1] if len(sys.argv) > 1 else ""
    if service not in ("web", "worker"):
        sys.exit("usage: otel_probe.py web|worker")
    out = run(service)
    print("RESULT " + json.dumps(out))  # the log lines are on stdout too
    check(service, out, os.environ["SOFTWARDEN_OPS_TOKEN"])
    print(f"OK: tracing on in the {service} process, scrubbed")
