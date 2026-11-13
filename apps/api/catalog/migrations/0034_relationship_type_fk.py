"""Relationship.type becomes a foreign key to the organization's RelationshipType (migration 0033), from the
choice column it was. Steps: drop the unique-link index (it names the column), rename the old column, add the
foreign key as nullable, fill it from the old value through the organization's type with that slug, make it NOT NULL,
drop the old column, put the unique-link index back on the new column, and add the composite foreign key so a link
can only use its own organization's type. Forward-only: the old values are gone once it has run."""

import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F, Value
from django.db.models.functions import Coalesce

NO_INSTANCE = uuid.UUID(int=0)


def unique_link():
    return models.UniqueConstraint(
        F("organization"),
        F("type"),
        F("source"),
        Coalesce("source_instance", Value(NO_INSTANCE, output_field=models.UUIDField())),
        F("target"),
        Coalesce("target_instance", Value(NO_INSTANCE, output_field=models.UUIDField())),
        name="catalog_relationship_unique_link",
    )


def backfill(apps, schema_editor):
    """Every link gets its organization's type of the same slug. A slug with no type (a value written behind the
    services' back) gets one made for it, not core and not a dependency, so no link is lost."""
    Relationship = apps.get_model("catalog", "Relationship")
    RelationshipType = apps.get_model("catalog", "RelationshipType")
    pairs = Relationship.objects.values_list("organization_id", "legacy_type").distinct()
    for org_id, slug in pairs:
        rtype = RelationshipType.objects.filter(organization_id=org_id, slug=slug).first()
        if rtype is None:
            base = slug.replace("_", " ").capitalize()
            name, n = base, 1
            while RelationshipType.objects.filter(organization_id=org_id, name__iexact=name).exists():
                n += 1  # "DEPENDS_ON" would be named like the core "Depends on": "Depends on (2)"
                name = f"{base} ({n})"
            rtype = RelationshipType.objects.create(organization_id=org_id, slug=slug, name=name)
        Relationship.objects.filter(organization_id=org_id, legacy_type=slug).update(type=rtype)
    # The foreign key checks are deferred; run them now, or the ALTER TABLE below meets pending trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0033_relationship_types"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="relationship", name="catalog_relationship_unique_link"),
        migrations.RenameField(model_name="relationship", old_name="type", new_name="legacy_type"),
        migrations.AddField(
            model_name="relationship",
            name="type",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="relationships",
                to="catalog.relationshiptype",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="relationship",
            name="type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="relationships",
                to="catalog.relationshiptype",
            ),
        ),
        migrations.RemoveField(model_name="relationship", name="legacy_type"),
        migrations.AddConstraint(model_name="relationship", constraint=unique_link()),
        migrations.RunSQL(
            sql="ALTER TABLE catalog_relationship ADD CONSTRAINT catalog_relationship_org_type_fk "
            "FOREIGN KEY (organization_id, type_id) REFERENCES catalog_relationshiptype (organization_id, id) "
            "DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE catalog_relationship DROP CONSTRAINT catalog_relationship_org_type_fk;",
        ),
    ]
