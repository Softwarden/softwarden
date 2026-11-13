from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    name = "discovery"

    def ready(self):
        from catalog import services
        from core import jobs
        from notify import reminders

        from . import assistant, credentials, drift, engine, record_map

        jobs.register_schedule(
            "discovery.connectors",
            engine.scheduled_runs,
            "* * * * *",
            "Queue requested and daily discovery connector runs; fail runs that lost their lease",
        )
        # Run history is kept to the newest engine.KEEP_RUNS finished runs per connector (after jobs.prune).
        jobs.register_schedule(
            "discovery.runs_prune",
            engine.prune_runs,
            "45 3 * * *",
            "Delete discovery runs beyond the newest 100 per connector",
        )
        # Each connector run is its own job, in the worker's long lane so a slow tenant holds up
        # nothing else. One attempt: the run records its own failure, and the next run (or Run now) tries again.
        jobs.register_job(engine.RUN_JOB, engine.run_job, lease=engine.RUN_LEASE, max_attempts=1, lane=jobs.LONG_LANE)
        # The assistant's batches (capability `assistant`): long lane too (a batch makes several API calls).
        # Three attempts: a rate limit, a 5xx or an unreachable API puts the batch back for the job's retry.
        jobs.register_job(
            assistant.JOB, assistant.run_job, lease=assistant.JOB_LEASE, max_attempts=3, lane=jobs.LONG_LANE
        )
        jobs.register_schedule(
            "discovery.assistant",
            assistant.scheduled_sweep,
            "* * * * *",
            "Fail assistant batches whose job is gone; mark unfinished assistant calls abandoned",
        )
        record_map.register_contributor()  # linked sources on a record's Map (catalog/record_map.py)
        # Discovered client secrets and certificates are the credential expiry reminder source.
        reminders.register_credential_source(credentials.reminder_facts)
        # A record edit recomputes the "source differs" rows of the observations linked to it.
        services.on_record_changed(drift.record_changed)
        # Anonymizing a Person unlinks them from the licensed users a licence usage run linked.
        from catalog import admin_services

        from .entra_licence_usage import unlink_anonymized

        if unlink_anonymized not in admin_services.ANONYMIZE_HOOKS:
            admin_services.ANONYMIZE_HOOKS.append(unlink_anonymized)
