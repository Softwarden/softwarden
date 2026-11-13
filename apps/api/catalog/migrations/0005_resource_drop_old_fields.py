"""Resource extensions, step 3 of 3: drop the columns that moved and enforce slug uniqueness per organization and kind."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_move_resource_data"),
    ]

    operations = [
        migrations.RemoveField(model_name="resource", name="purpose"),
        migrations.RemoveField(model_name="resource", name="vendor"),
        migrations.RemoveField(model_name="resource", name="archived"),
        migrations.AlterField(model_name="resource", name="slug", field=models.SlugField(max_length=220)),
        migrations.AlterField(model_name="resource", name="description", field=models.TextField(blank=True)),
        migrations.AddConstraint(
            model_name="resource",
            constraint=models.UniqueConstraint(
                fields=("organization", "kind", "slug"), name="catalog_resource_unique_slug"
            ),
        ),
    ]
