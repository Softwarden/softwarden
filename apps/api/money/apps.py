from django.apps import AppConfig


class MoneyConfig(AppConfig):
    name = "money"

    def ready(self):
        from core import jobs

        from . import record_map, search, terms

        jobs.register_schedule(
            "contracts.advance_terms",
            terms.scheduled_advance,
            "10 * * * *",  # hourly: each organization's local midnight is acted on within the hour
            "Move contract terms along their dates (started, renewed, expired)",
        )

        search.register()  # contracts in global search (core/search.py)
        record_map.register_contributors()  # contracts and licence pools on a record's Map (catalog/record_map.py)
