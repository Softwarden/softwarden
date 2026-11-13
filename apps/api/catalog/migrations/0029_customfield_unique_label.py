"""Custom fields: a field's label is unique per organization ignoring case in the database too, so
two concurrent creates (or renames) can't both take it. The service already refused duplicates, so existing rows
satisfy the constraint."""

import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0028_custom_fields"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="customfield",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                django.db.models.functions.text.Lower("label"),
                name="catalog_customfield_unique_label",
            ),
        ),
    ]
