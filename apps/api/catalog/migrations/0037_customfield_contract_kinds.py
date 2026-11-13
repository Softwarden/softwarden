"""Custom fields may apply to contracts and licence pools.
Only the choices of `applies_to` change; no stored value does. Forward-only."""

import django.contrib.postgres.fields
from django.db import migrations, models

KINDS = ["application", "component", "integration", "ai_agent", "vendor", "contract", "license_pool"]


class Migration(migrations.Migration):
    dependencies = [("catalog", "0036_attachment_contract")]

    operations = [
        migrations.AlterField(
            model_name="customfield",
            name="applies_to",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(choices=[(k, k) for k in KINDS], max_length=20), size=None
            ),
        ),
    ]
