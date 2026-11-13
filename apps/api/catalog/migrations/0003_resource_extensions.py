"""Resource extensions, step 1 of 3: add the extension tables and the new shared columns beside the old ones."""

import django.db.models.deletion
from django.db import migrations, models

BUILT_BY = [("vendor", "Vendor"), ("internal", "Internal"), ("contractor", "Contractor"), ("community", "Community")]
LICENSE = [
    ("commercial", "Commercial"),
    ("open_source", "Open source"),
    ("free", "Free"),
    ("internal", "Internal"),
    ("unknown", "Unknown"),
]
COMPONENT_TYPES = [
    ("script", "Script"),
    ("scheduled_job", "Scheduled job"),
    ("runbook", "Runbook"),
    ("function", "Function"),
    ("logic_app", "Logic App"),
    ("automation_flow", "Automation flow"),
    ("report", "Report"),
    ("ai_service", "AI service"),
    ("middleware", "Middleware"),
    ("internal_api", "Internal API"),
    ("other", "Other"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_accounts_reference_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource", name="slug", field=models.SlugField(max_length=220, default="")
        ),
        migrations.AddField(model_name="resource", name="description", field=models.TextField(blank=True, default="")),
        migrations.AddField(
            model_name="resource", name="archived_at", field=models.DateTimeField(blank=True, null=True)
        ),
        migrations.CreateModel(
            name="Application",
            fields=[
                (
                    "resource",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="application",
                        serialize=False,
                        to="catalog.resource",
                    ),
                ),
                ("purpose", models.TextField(blank=True)),
                ("built_by", models.CharField(blank=True, choices=BUILT_BY, max_length=20)),
                ("license_model", models.CharField(blank=True, choices=LICENSE, max_length=20)),
                ("documentation_url", models.URLField(blank=True)),
                ("support_url", models.URLField(blank=True)),
                (
                    "vendor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="catalog.vendor",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Component",
            fields=[
                (
                    "resource",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="component",
                        serialize=False,
                        to="catalog.resource",
                    ),
                ),
                ("component_type", models.CharField(choices=COMPONENT_TYPES, default="other", max_length=30)),
                ("purpose", models.TextField(blank=True)),
                ("host_runtime", models.CharField(blank=True, max_length=200)),
                ("repository_url", models.URLField(blank=True)),
                ("documentation_url", models.URLField(blank=True)),
                ("built_by", models.CharField(blank=True, choices=BUILT_BY, max_length=20)),
                ("license_model", models.CharField(blank=True, choices=LICENSE, max_length=20)),
            ],
        ),
    ]
