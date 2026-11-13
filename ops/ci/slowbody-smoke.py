#!/usr/bin/env python3
"""Slow-body smoke test against a running Compose stack. CI's compose-first-run job runs it.

    python3 ops/ci/slowbody-smoke.py [--connections 3] [--hold 40] [--port 443] [--limit 1.0]

Opens N TLS connections to Caddy that each declare a 5,000-byte JSON body for `POST /api/v1/auth/login`, send a few
bytes of it and then one byte every 2 s, without authenticating, for --hold seconds. Meanwhile it requests `/healthz`
and `/` every two seconds. Every probe must answer 200 within --limit seconds: Caddy buffers request bodies before
they reach gunicorn (docker/Caddyfile, request_buffers) and gunicorn runs threaded workers (apps/api/gunicorn.conf.py),
so a handful of slow clients can't occupy every worker. Exit status 1 on any slow or failed probe, or when the
server closed every slow connection before the hold ended (then nothing was tested).

Standard library only; certificate checks are off (Caddy's local CA). Talks to localhost (the slow sockets to
127.0.0.1 with SNI and Host `localhost`).
"""

import argparse
import http.client
import socket
import ssl
import sys
import threading
import time

BODY_LENGTH = 5000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--connections", type=int, default=3)
    parser.add_argument("--hold", type=int, default=40, help="seconds to keep the slow bodies going")
    parser.add_argument("--port", type=int, default=443, help="the published HTTPS port (HTTPS_PORT)")
    parser.add_argument("--path", default="/api/v1/auth/login", help="where the slow bodies are posted")
    parser.add_argument("--limit", type=float, default=1.0, help="the slowest acceptable probe, in seconds")
    args = parser.parse_args()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    socks = []
    for _ in range(args.connections):
        s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", args.port), timeout=10), server_hostname="localhost")
        s.sendall(
            (
                f"POST {args.path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                f"Content-Length: {BODY_LENGTH}\r\nConnection: keep-alive\r\n\r\n" + '{"email":"a'
            ).encode()
        )
        socks.append(s)
    print(f"{args.connections} slow connection(s) open, holding {args.hold} s", flush=True)

    stop = threading.Event()
    closed: set[int] = set()  # sockets the server closed (a send failed): they no longer hold anything

    def trickle() -> None:
        while not stop.is_set():
            for i, s in enumerate(socks):
                if i in closed:
                    continue
                try:
                    s.sendall(b"a")
                except OSError:
                    closed.add(i)
            stop.wait(2)

    threading.Thread(target=trickle, daemon=True).start()
    time.sleep(3)

    def probe(path: str) -> tuple[str, float]:
        started = time.monotonic()
        # A new connection per probe, as a fresh visitor's, to localhost (Caddy's site name, also its TLS SNI).
        conn = http.client.HTTPSConnection("localhost", args.port, timeout=max(5.0, args.limit * 5), context=ctx)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            outcome = str(response.status)
        except (OSError, http.client.HTTPException) as exc:  # a timeout or a dropped connection is a failed probe
            outcome = repr(exc)[:80]
        finally:
            conn.close()
        return outcome, time.monotonic() - started

    failures = 0
    probes = 0
    slowest = 0.0
    end = time.monotonic() + args.hold
    while time.monotonic() < end:
        line = [time.strftime("%H:%M:%S")]
        for path in ("/healthz", "/"):
            outcome, took = probe(path)
            probes += 1
            slowest = max(slowest, took)
            bad = outcome != "200" or took >= args.limit
            failures += bad
            line.append(f"{path} {outcome} {took:.2f}s{' SLOW/FAILED' if bad else ''}")
        print("  ".join(line), flush=True)
        time.sleep(2)

    stop.set()
    for s in socks:
        try:
            s.close()
        except OSError:
            pass
    open_at_end = args.connections - len(closed)
    print(f"{probes} probes, {failures} slow or failed, slowest {slowest:.2f}s (limit {args.limit:.2f}s)")
    print(f"{open_at_end} of {args.connections} slow connection(s) still open at the end of the hold")
    if open_at_end == 0:
        # Every slow socket was closed early, so the probes ran against an idle server and prove nothing.
        print("FAILED: the server closed every slow connection before the hold ended; nothing was tested")
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
