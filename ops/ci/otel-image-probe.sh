#!/usr/bin/env bash
# OpenTelemetry switched on inside an image built with WITH_OTEL=1. CI's image job runs
# it in its WITH_OTEL=1 leg; run it locally the same way from the repository root:
#
#   docker build --build-arg WITH_OTEL=1 --tag softwarden:otel . && ops/ci/otel-image-probe.sh softwarden:otel
#
# Runs apps/api/tests/otel_probe.py (the file tests/test_observability.py's OTel tests run) in a throwaway container of
# the image, once as the web process and once as the worker, with the image's own packages: tracing must switch on,
# the span must carry the request id, and no query string or secret may reach it. No database, no network: spans go to
# an in-memory exporter. Exits non-zero if either run fails.
set -euo pipefail

image=${1:?usage: ops/ci/otel-image-probe.sh <image built with WITH_OTEL=1>}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
probe="$root/apps/api/tests/otel_probe.py"
[ -f "$probe" ] || { echo "no $probe" >&2; exit 2; }
# A path docker.exe understands (C:/... under Git Bash; unchanged on Linux).
if command -v cygpath >/dev/null 2>&1; then probe=$(cygpath -m "$probe"); fi
token="ops-token-otel-probe-$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"

for service in web worker; do
  echo "== $service"
  # MSYS_NO_PATHCONV: Git Bash would otherwise rewrite the container paths below into Windows ones.
  MSYS_NO_PATHCONV=1 docker run --rm --network none \
    -v "$probe:/tmp/otel_probe.py:ro" \
    -e PYTHONPATH=/app/api -e SOFTWARDEN_DEBUG=1 -e DJANGO_SETTINGS_MODULE=softwarden.settings \
    -e OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:9 -e OTEL_BSP_SCHEDULE_DELAY=60000 \
    -e SOFTWARDEN_OPS_TOKEN="$token" \
    "$image" python /tmp/otel_probe.py "$service" | grep -v '^RESULT '
done
echo "OpenTelemetry works in $image"
