from django.apps import AppConfig


class NotifyConfig(AppConfig):
    name = "notify"

    def ready(self):
        from core import jobs

        from . import deliveries, digest, reminders, rule_services, rules, subscriptions, webhooks

        rule_services.connect()  # every new organization starts with the built-in notification rules

        # One attempt per job; the delivery keeps its own attempts, backoff and Retry-After (notify/deliveries.py), so
        # the job itself is never retried by core/jobs.py.
        jobs.register_job(deliveries.JOB_KIND, deliveries.run_job, max_attempts=1)
        jobs.register_schedule(
            "notify.deliveries",
            deliveries.sweep,
            "* * * * *",
            "Fail deliveries whose worker stopped mid-send; requeue deliveries whose job is gone",
        )

        # The webhook outbox. Registered before the other notify schedules; every schedule fires before the
        # tick claims jobs, so an event committed before a tick is posted by that tick.
        jobs.register_schedule(
            "webhooks.outbox",
            webhooks.drain,
            "* * * * *",
            "Queue webhook deliveries for the events recorded since the last run (the outbox)",
        )
        jobs.register_schedule(
            "notify.retention",
            deliveries.purge,
            "40 3 * * *",
            "Delete finished deliveries past their retention (sent 30 days, failed and skipped 90; configurable)",
        )

        # Every minute: each organization's digest time is local, so the function decides who is due (as before).
        # Each digest is a delivery (a `notify.deliver` job), so there is no separate send schedule.
        jobs.register_schedule("digest.build", digest.scheduled_build, "* * * * *", "Build daily digests")
        jobs.register_schedule(
            "reminders.generate",
            reminders.scheduled_generate,
            "20 * * * *",  # hourly, after contracts.advance_terms (:10): each organization's local midnight within the hour
            "Generate reminders from deadlines (each organization's local date)",
        )
        jobs.register_schedule(
            "notify.rules",
            rules.scheduled_run,
            "25 * * * *",  # hourly, after reminders.generate (:20): each organization on its local date
            "Send reminder notices from notification rules",
        )
        jobs.register_schedule(
            "saved_views.subscriptions",
            subscriptions.scheduled_run,
            "26 * * * *",  # hourly, a minute after notify.rules (:25): the same local send times, each organization's date
            "Send saved view subscriptions (weekly, each organization's local date)",
        )
