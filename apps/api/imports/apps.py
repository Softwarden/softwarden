from django.apps import AppConfig


class ImportsConfig(AppConfig):
    name = "imports"

    def ready(self):
        from core import jobs

        from . import importer

        # Every minute as before: cheap when nothing is due, and a purge date is reached at each organization's
        # local midnight.
        jobs.register_schedule("imports.purge", importer.scheduled_purge, "* * * * *", "Purge expired import files")
