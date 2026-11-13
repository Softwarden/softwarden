"""Custom fields: CustomField definitions per organization, and a `custom_field_data` JSONB column with
a GIN index (jsonb_path_ops, for the `@>` containment the list filters use) on Resource (applications, components,
integrations, AI agents) and Vendor. CustomField references no tenant row besides its organization, so it needs no
composite foreign key. The new columns default to {} for existing rows."""

import uuid

import django.contrib.postgres.fields
import django.contrib.postgres.indexes
import django.db.models.deletion
from django.db import migrations, models

KINDS = [(k, k) for k in ("application", "component", "integration", "ai_agent", "vendor")]
TYPES = [
    ("text", "Text"),
    ("longtext", "Long text"),
    ("integer", "Whole number"),
    ("decimal", "Decimal number"),
    ("boolean", "Yes or no"),
    ("date", "Date"),
    ("url", "Web address"),
    ("select", "Choice"),
    ("multiselect", "Multiple choices"),
    ("object", "JSON object"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0027_ai_agents"),
        ("core", "0008_auditevent_append_only"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomField",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=50)),
                ("label", models.CharField(max_length=100)),
                ("type", models.CharField(choices=TYPES, max_length=20)),
                ("description", models.CharField(blank=True, max_length=500)),
                (
                    "choices",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=100), blank=True, default=list, size=None
                    ),
                ),
                ("required", models.BooleanField(default=False)),
                ("default", models.JSONField(blank=True, null=True)),
                (
                    "applies_to",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(choices=KINDS, max_length=20), size=None
                    ),
                ),
                ("weight", models.PositiveSmallIntegerField(default=100)),
                ("is_reminder_source", models.BooleanField(default=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "verbose_name": "custom field",
                "ordering": ["weight", "name"],
                "abstract": False,
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="catalog_customfield_org_id"),
                    models.UniqueConstraint(fields=("organization", "name"), name="catalog_customfield_unique_name"),
                    models.CheckConstraint(
                        condition=models.Q(("is_reminder_source", False), ("type", "date"), _connector="OR"),
                        name="catalog_customfield_reminder_is_date",
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name="resource",
            name="custom_field_data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="vendor",
            name="custom_field_data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddIndex(
            model_name="resource",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["custom_field_data"], name="catalog_resource_cf_gin", opclasses=["jsonb_path_ops"]
            ),
        ),
        migrations.AddIndex(
            model_name="vendor",
            index=django.contrib.postgres.indexes.GinIndex(
                fields=["custom_field_data"], name="catalog_vendor_cf_gin", opclasses=["jsonb_path_ops"]
            ),
        ),
    ]
