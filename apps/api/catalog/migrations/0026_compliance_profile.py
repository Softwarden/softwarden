"""The compliance profile's columns on Resource. All nullable, so existing records
read as "not recorded"; the API reads and writes them only while the organization has the profile on
(catalog/compliance.py)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0025_vendor_team_tag"),
    ]

    operations = [
        migrations.AddField(
            model_name="resource",
            name="is_gxp",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="resource",
            name="validation_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("not_assessed", "Not assessed"),
                    ("not_required", "Not required"),
                    ("in_validation", "In validation"),
                    ("validated", "Validated"),
                    ("retired", "Retired"),
                ],
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="resource",
            name="validation_doc_url",
            field=models.URLField(blank=True, max_length=2000, null=True),
        ),
        migrations.AddField(
            model_name="resource",
            name="last_periodic_review",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="resource",
            name="next_periodic_review",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="resource",
            name="risk_tier",
            field=models.CharField(
                blank=True,
                choices=[("low", "Low"), ("medium", "Medium"), ("high", "High")],
                max_length=10,
                null=True,
            ),
        ),
    ]
