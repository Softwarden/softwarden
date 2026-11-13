# Softwarden MCP server image (compose profile `mcp`, docs/MCP.md). It talks to Softwarden only through its REST API,
# with each client's own API token; it holds no secret and no database connection.
# compose builds it with the repository's ca-certs/ as the `cacerts` context (ca-certs/README.md). Standalone:
#   docker build --build-context cacerts=../ca-certs -t softwarden-mcp:local mcp
FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NATIVE_TLS=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    PATH="/app/.venv/bin:$PATH" \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8001
# Only the network's root certificates (ca-certs/*.crt; ca-certs/README.md) are trusted; having none is fine.
RUN --mount=type=bind,from=cacerts,target=/tmp/ca-certs \
    mkdir -p /usr/local/share/ca-certificates/extra \
 && find /tmp/ca-certs -maxdepth 1 -name '*.crt' -exec cp {} /usr/local/share/ca-certificates/extra/ ';' \
 && update-ca-certificates
COPY --from=ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# uv's download cache lives in a BuildKit cache mount, so it never ends up in an image layer.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY softwarden_mcp softwarden_mcp
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev \
 && useradd --system --uid 10002 softwarden-mcp
USER softwarden-mcp
EXPOSE 8001
# /healthz answers without a token or an allowed Host (TokenGate checks it first), so the probe needs neither.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=4).status == 200 else 1)"]
CMD ["softwarden-mcp", "--transport", "http"]
