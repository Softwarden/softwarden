"""Software categories: per organization, seeded with DEFAULT_CATEGORIES when the
organization is created. Editors and Admins add, rename and delete them like teams and vendors
(catalog/admin_services.py); a category in use can't be deleted.

Seeding is part of creating the organization (like its default settings), so it writes no audit event. Migration
catalog 0023 seeded the organizations that existed before categories did, from its own frozen copy of this list."""

from django.db.models.signals import post_save

from core.models import Organization

from .models import SoftwareCategory

# (name, description). Edit this list for new organizations only; existing organizations keep their own categories.
DEFAULT_CATEGORIES = (
    ("Productivity", "Office suites, notes, task and time tools."),
    ("Collaboration", "Chat, meetings, intranet and file sharing."),
    ("Security and identity", "Sign-in, access, endpoint and threat protection."),
    ("ERP and finance", "Ledger, purchasing, expenses and billing."),
    ("HR and people", "HR records, payroll inputs, recruiting and learning."),
    ("CRM and sales", "Customers, pipeline and quoting."),
    ("Marketing", "Campaigns, web content and analytics for marketing."),
    ("Customer service", "Ticketing, knowledge base and contact center."),
    ("IT service management", "Service desk, assets, monitoring and IT operations."),
    ("Developer tools", "Source control, build, test and deployment."),
    ("Data and analytics", "Reporting, BI, warehouses and data pipelines."),
    ("AI platform", "AI models, assistants and agent platforms."),
    ("Infrastructure and hosting", "Cloud, servers, networks and backups."),
    ("Document management", "Records, e-signature and document control."),
    ("Quality and compliance", "Quality management, validation, audit and risk."),
    ("Design and creative", "Graphics, video and design tools."),
    ("Other", "Anything that fits no other category."),
)


def seed(org) -> int:
    """Add the default categories the organization doesn't have yet (by name, ignoring case). Returns how many."""
    have = {name.lower() for name in SoftwareCategory.objects.filter(organization=org).values_list("name", flat=True)}
    rows = [
        SoftwareCategory(organization=org, name=name, description=description)
        for name, description in DEFAULT_CATEGORIES
        if name.lower() not in have
    ]
    SoftwareCategory.objects.bulk_create(rows)
    return len(rows)


def _on_organization_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw:
        seed(instance)


def connect() -> None:
    """Called once from CatalogConfig.ready: every new organization starts with the default list."""
    post_save.connect(_on_organization_saved, sender=Organization, dispatch_uid="catalog-seed-categories")
