from django.apps import AppConfig


class TicketsConfig(AppConfig):
    name = "tickets"
    verbose_name = "Ticket links"

    def ready(self):
        from . import refresh

        # The hourly status refresh of open ticket links through each organization's ticket connections.
        refresh.register()
