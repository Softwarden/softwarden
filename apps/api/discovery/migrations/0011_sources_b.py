"""The azure, powerplatform and halo sources (enterprise capabilities discovery.azure,
discovery.powerplatform and discovery.halo). Choices only change Django's state; no column changes. Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('discovery', '0010_entra_observation_keys'),
    ]

    operations = [
        migrations.AlterField(
            model_name='connector',
            name='source',
            field=models.CharField(choices=[('entra', 'Microsoft Entra ID'), ('entra_licenses', 'Microsoft Entra licences'), ('azure', 'Azure'), ('powerplatform', 'Power Platform'), ('halo', 'HaloITSM')], max_length=20),
        ),
    ]
