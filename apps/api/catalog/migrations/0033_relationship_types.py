"""Relationship types, one list per organization, and the three core types
for every organization that already exists. New organizations are seeded by catalog/relationship_types.py when they
are created; this migration keeps its own copy of the core types as they were, so later edits there don't change what
it did. Migration 0034 points Relationship.type at these rows."""

import uuid

import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

# (slug, name, inverse name, description, is_dependency)
CORE_TYPES = (
    ("depends_on", "Depends on", "Is needed by", "The source needs the target to work.", True),
    ("implements", "Implements", "Is implemented by", "A component that carries out an integration's data flow.", False),
    ("replaces", "Replaces", "Is replaced by", "The source takes over from the target, which is being retired.", False),
)


def seed_existing(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    RelationshipType = apps.get_model("catalog", "RelationshipType")
    rows = []
    for org_id in Organization.objects.order_by("id").values_list("id", flat=True):
        rows += [
            RelationshipType(
                organization_id=org_id,
                slug=slug,
                name=name,
                inverse_name=inverse,
                description=description,
                is_dependency=is_dependency,
                is_core=True,
            )
            for slug, name, inverse, description, is_dependency in CORE_TYPES
        ]
    RelationshipType.objects.bulk_create(rows)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0032_quality_exceptions"),
        ("core", "0008_auditevent_append_only"),
    ]

    operations = [
        migrations.CreateModel(
            name="RelationshipType",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("slug", models.SlugField()),
                ("name", models.CharField(max_length=100)),
                ("inverse_name", models.CharField(blank=True, max_length=100)),
                ("description", models.CharField(blank=True, max_length=200)),
                ("is_dependency", models.BooleanField(default=False)),
                ("is_core", models.BooleanField(default=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="relationshiptype",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_relationshiptype_org_id"),
        ),
        migrations.AddConstraint(
            model_name="relationshiptype",
            constraint=models.UniqueConstraint(
                fields=("organization", "slug"), name="catalog_relationshiptype_unique_slug"
            ),
        ),
        migrations.AddConstraint(
            model_name="relationshiptype",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                django.db.models.functions.text.Lower("name"),
                name="catalog_relationshiptype_unique_name",
            ),
        ),
        # Forward-only data; reversing drops the table with it.
        migrations.RunPython(seed_existing, migrations.RunPython.noop),
    ]
