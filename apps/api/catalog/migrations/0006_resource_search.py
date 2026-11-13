from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0005_resource_drop_old_fields")]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name="resource",
            index=GinIndex(fields=["name"], name="catalog_resource_name_trgm", opclasses=["gin_trgm_ops"]),
        ),
    ]
