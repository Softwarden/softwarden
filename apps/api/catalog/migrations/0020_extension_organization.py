"""Tenancy fixes: Application and Component rows carry their own organization, like Integration.

The column is added nullable, filled from each row's Resource, then made NOT NULL. (organization, resource) is the
rows' (organization, id) key, and a composite foreign key keeps each row in its resource's organization."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0019_split_apps"),
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="application",
            name="organization",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
            ),
        ),
        migrations.AddField(
            model_name="component",
            name="organization",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
            ),
        ),
        migrations.RunSQL(
            sql="""
                UPDATE catalog_application e SET organization_id = r.organization_id
                FROM catalog_resource r WHERE r.id = e.resource_id;
                UPDATE catalog_component e SET organization_id = r.organization_id
                FROM catalog_resource r WHERE r.id = e.resource_id;
                SET CONSTRAINTS ALL IMMEDIATE;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="application",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
            ),
        ),
        migrations.AlterField(
            model_name="component",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
            ),
        ),
        migrations.AddConstraint(
            model_name="application",
            constraint=models.UniqueConstraint(
                fields=("organization", "resource"), name="catalog_application_org_resource_uq"
            ),
        ),
        migrations.AddConstraint(
            model_name="component",
            constraint=models.UniqueConstraint(
                fields=("organization", "resource"), name="catalog_component_org_resource_uq"
            ),
        ),
        migrations.RunSQL(
            sql="""
                ALTER TABLE catalog_application
                ADD CONSTRAINT catalog_application_org_resource_fk
                FOREIGN KEY (organization_id, resource_id)
                REFERENCES catalog_resource (organization_id, id) DEFERRABLE INITIALLY DEFERRED;
                ALTER TABLE catalog_component
                ADD CONSTRAINT catalog_component_org_resource_fk
                FOREIGN KEY (organization_id, resource_id)
                REFERENCES catalog_resource (organization_id, id) DEFERRABLE INITIALLY DEFERRED;
            """,
            reverse_sql="""
                ALTER TABLE catalog_component DROP CONSTRAINT catalog_component_org_resource_fk;
                ALTER TABLE catalog_application DROP CONSTRAINT catalog_application_org_resource_fk;
            """,
        ),
    ]
