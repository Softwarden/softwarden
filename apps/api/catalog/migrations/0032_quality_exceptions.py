"""Data quality: QualityException, a reasoned "not applicable" for one data-quality rule
(catalog/quality.py) on one record, with an accountable Person and an expiry. Composite foreign keys keep the
record and the owner in its organization; they are added with the table, which is empty. created_by is a User, not a
tenant row."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table, referenced key)
    ("catalog_qualityexception", "catalog_qualityexc_org_resource_fk", "resource_id", "catalog_resource", "id"),
    ("catalog_qualityexception", "catalog_qualityexc_org_owner_fk", "owner_id", "catalog_person", "id"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0031_search_index"),
        ("core", "0008_auditevent_append_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="QualityException",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("rule", models.CharField(max_length=60)),
                ("reason", models.TextField()),
                ("expires_on", models.DateField()),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="catalog.person"
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="quality_exceptions",
                        to="catalog.resource",
                    ),
                ),
            ],
            options={
                "verbose_name": "data quality exception",
                "ordering": ["expires_on", "id"],
                "abstract": False,
                "indexes": [models.Index(fields=["organization", "rule"], name="catalog_qualityexc_rule")],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="catalog_qualityexception_org_id"),
                    models.UniqueConstraint(fields=("resource", "rule"), name="catalog_qualityexception_unique"),
                ],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, {key}) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target, key in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, *_rest in reversed(KEYS)
            ),
        ),
    ]
