from django.apps import AppConfig
from django.core import checks


class CatalogConfig(AppConfig):
    name = "catalog"

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_delete

        from core import jobs
        from imports import xlsxio

        from . import (
            attachments,
            categories,
            customfields,
            org_lifecycle,
            password_reset,
            record_map,
            relationship_types,
            saved_filters,
            search,
            snapshots,
            usage,
        )

        snapshots.connect()  # "before" snapshots for audit events (catalog/snapshots.py)
        # A deleted user's private saved views go with them; their shared views stay, ownerless.
        pre_delete.connect(
            saved_filters.delete_private_views_of, sender=get_user_model(), dispatch_uid="saved-views-of-deleted-user"
        )
        categories.connect()  # every new organization starts with the default software categories
        relationship_types.connect()  # ... and with the core relationship types
        if customfields.excel_sheet not in xlsxio.EXTRA_SHEETS:  # the Excel export's "Custom fields" sheet
            xlsxio.EXTRA_SHEETS.append(customfields.excel_sheet)
        # Removed attachments' files, once past the retention window (catalog/attachments.py). After the nightly
        # backup (02:30 by default, core/backup.py), before jobs.prune.
        jobs.register_schedule(
            "attachments.sweep",
            attachments.scheduled_sweep,
            "20 3 * * *",
            "Delete removed attachment files after the retention window",
        )
        checks.register(attachments.check_settings, "softwarden", deploy=False)
        # A record's Map (catalog/record_map.py): relationships, integrations and instances; discovery registers its
        # linked sources from its own ready(), contracts and licence pools from money/.
        record_map.register_catalog()
        search.register()  # records, vendors, people and attachments in global search (core/search.py)
        # The export job, and the schedules that purge expired signups, expired exports and organizations
        # whose deletion grace period has ended (catalog/org_lifecycle.py).
        org_lifecycle.register()
        password_reset.register()  # the worker sends reset links, so every request takes the same time
        # Platform admin console: every organization's usage counts, hourly (catalog/usage.py).
        usage.register()
