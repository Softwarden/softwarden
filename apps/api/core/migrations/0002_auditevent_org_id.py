"""Tenancy fixes: audit events get the (organization, id) key every tenant table carries."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_auditevent_org_id"),
        ),
    ]
