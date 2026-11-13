"""Software categories, one list per organization, and the default list for
every organization that already exists. New organizations are seeded by catalog/categories.py when they are created;
this migration keeps its own copy of the list as it was, so later edits there don't change what it did."""

import uuid

import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

DEFAULT_CATEGORIES = (
    ("Productivity", "Office suites, notes, task and time tools."),
    ("Collaboration", "Chat, meetings, intranet and file sharing."),
    ("Security and identity", "Sign-in, access, endpoint and threat protection."),
    ("ERP and finance", "Ledger, purchasing, expenses and billing."),
    ("HR and people", "HR records, payroll inputs, recruiting and learning."),
    ("CRM and sales", "Customers, pipeline and quoting."),
    ("Marketing", "Campaigns, web content and analytics for marketing."),
    ("Customer service", "Ticketing, knowledge base and contact centre."),
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


def seed_existing(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    SoftwareCategory = apps.get_model("catalog", "SoftwareCategory")
    rows = []
    for org_id in Organization.objects.order_by("id").values_list("id", flat=True):
        rows += [SoftwareCategory(organization_id=org_id, name=n, description=d) for n, d in DEFAULT_CATEGORIES]
    SoftwareCategory.objects.bulk_create(rows)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0022_resourcetag_uuid"),
        ("core", "0008_auditevent_append_only"),
    ]

    operations = [
        migrations.CreateModel(
            name="SoftwareCategory",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=100)),
                ("description", models.CharField(blank=True, max_length=200)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "verbose_name": "software category",
                "verbose_name_plural": "software categories",
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="softwarecategory",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_softwarecategory_org_id"),
        ),
        migrations.AddConstraint(
            model_name="softwarecategory",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                django.db.models.functions.text.Lower("name"),
                name="catalog_softwarecategory_unique_name",
            ),
        ),
        # Forward-only data; reversing drops the table with it.
        migrations.RunPython(seed_existing, migrations.RunPython.noop),
    ]
