"""End-of-life tracking: EolProduct.renamed_to, the name endoflife.date redirected a tracked
product's address to (a rename), so the screen can offer to re-map. Installation-wide cache table; a new blank column."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("eol", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="eolproduct",
            name="renamed_to",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
