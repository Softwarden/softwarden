from django.apps import AppConfig


class EolConfig(AppConfig):
    name = "eol"
    verbose_name = "End of life"

    def ready(self):
        from catalog import services

        from . import cache

        # The daily refresh of the endoflife.date cache (a job in the long lane, so the tick never waits on the
        # network), and the job a new mapping queues for a product whose releases aren't cached yet.
        cache.register()
        # An application's version is the default source of its derived cycle: a record edit re-derives it.
        services.on_record_changed(cache.record_changed)
