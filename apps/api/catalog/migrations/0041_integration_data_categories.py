"""Data-flow map: what kinds of data an integration carries, a multi-select beside its
data classification (personal, special-category personal, financial, credentials or secrets, health, customer content,
other). Existing integrations start with none. Forward-only."""

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0040_vendor_risk"),
    ]

    operations = [
        migrations.AddField(
            model_name="integration",
            name="data_categories",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(
                    choices=[
                        ("personal", "Personal data"),
                        ("special_category", "Special-category personal data"),
                        ("financial", "Financial"),
                        ("credentials", "Credentials or secrets"),
                        ("health", "Health"),
                        ("customer_content", "Customer content"),
                        ("other", "Other"),
                    ],
                    max_length=20,
                ),
                blank=True,
                default=list,
                size=None,
            ),
        ),
    ]
