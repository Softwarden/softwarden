"""Run the background worker: a tick every minute until stopped (core/worker.py)."""

import os
import signal
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from core import health, worker
from softwarden import otel


class Command(BaseCommand):
    help = "Fire due schedules and run queued jobs: one tick a minute until SIGTERM or Ctrl+C."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one tick and exit.")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between ticks (default 60).")
        parser.add_argument(
            "--long-jobs",
            default=None,
            help="Long jobs (discovery runs) run at once in threads beside the tick, 1 or more (default "
            "SOFTWARDEN_WORKER_LONG_JOBS, else 2). --once runs them inline instead.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 0 when the worker heartbeat is fresh, 1 otherwise (the container healthcheck); runs nothing.",
        )
        parser.add_argument(
            "--local",
            action="store_true",
            help="With --check: this container's own worker process (the file it touches after each beat), not the "
            "heartbeat any worker refreshes; the liveness probe when several worker replicas run. Needs no database.",
        )

    def handle(self, *args, once=False, interval=60, check=False, long_jobs=None, local=False, **options):
        if check and local:
            age = worker.local_heartbeat_age()
            stale_after = health.STALE_AFTER.total_seconds()
            if age is None:
                raise CommandError("This container's worker heartbeat: never.")
            if age > stale_after:
                raise CommandError(f"This container's worker heartbeat: stale ({int(age)} s ago).")
            self.stdout.write(f"This container's worker heartbeat: ok ({int(age)} s ago).")
            return
        if local:
            raise CommandError("--local goes with --check.")
        if check:
            state = health.worker_check()["state"]
            if state != "ok":
                raise CommandError(f"Worker heartbeat: {state}.")
            self.stdout.write("Worker heartbeat: ok.")
            return
        lane = self._lane(long_jobs)  # refused at start-up, before any tick, when it can't run discovery
        otel.configure("worker")  # a span per job when OTEL_EXPORTER_OTLP_ENDPOINT is set (softwarden/otel.py)
        if once:
            close_old_connections()
            keeper = worker.HeartbeatKeeper().start()  # renews a long schedule run's lease, as in the loop
            try:
                summary = worker.tick()
            finally:
                keeper.stop()
            self.stdout.write(f"Tick: {summary}")
            return
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.stdout.write("Worker started.")
        worker.keep_local_heartbeat()  # for `run_worker --check --local`, this replica's liveness probe
        worker.start_main_loop(interval)  # an idle spell past the interval (plus a margin) is a stuck loop
        keeper = worker.HeartbeatKeeper().start()
        try:
            while not stopping:
                close_old_connections()
                try:
                    worker.tick(should_stop=lambda: stopping, lanes=lane)
                except Exception:  # a database outage: log, wait, try again rather than exit
                    worker.log.exception("worker tick failed")
                slept = 0.0
                while slept < interval and not stopping:
                    time.sleep(0.5)
                    slept += 0.5
        finally:
            # The loop has stopped: waiting for the long lane below isn't a stuck loop, so lift the idle bound first.
            worker.stop_main_loop()
            lane.stop()  # running discovery runs fail themselves at their next request and release their connector
            left = lane.wait()
            keeper.stop()
            worker.keep_local_heartbeat(on=False)
        if left:
            self.stdout.write(f"Worker stopped; {left} long job(s) left to their leases.")
            return
        self.stdout.write("Worker stopped.")

    @staticmethod
    def _lane(value):
        raw = value if value is not None else (os.environ.get("SOFTWARDEN_WORKER_LONG_JOBS") or "").strip()
        if raw in (None, ""):
            return worker.LongLane(worker.LONG_LANE_SIZE)
        size: object
        try:
            size = int(str(raw).strip())
        except ValueError:
            size = raw
        try:
            return worker.LongLane(size)
        except worker.LaneSizeError as e:
            raise CommandError(str(e)) from None
