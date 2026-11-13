# The `license` observation kind (one SKU's seat counts, for the licence pools' seat review; produced by the
# Entra licences source). Choices only, no data change.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('discovery', '0003_component_types'),
    ]

    operations = [
        migrations.AlterField(
            model_name='observation',
            name='kind',
            field=models.CharField(choices=[('application', 'Application'), ('component', 'Component'), ('suggested_integration', 'Suggested integration'), ('suggested_owner', 'Suggested owner'), ('license', 'Licence seat counts')], max_length=30),
        ),
    ]
