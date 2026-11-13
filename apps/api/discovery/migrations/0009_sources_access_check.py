"""The Entra licences source (`entra_licenses`), the `ai_agent` observation kind (Copilot Studio
agents and Entra Agent ID agent identities become AI agent candidates), and a connector's last access check
(discovery/access.py). Choices only change Django's state; the new columns are nullable or have defaults. Forward-only."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('discovery', '0008_discovered_credential'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='connector',
            name='access_check',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='connector',
            name='access_checked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connector',
            name='access_checked_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='connector',
            name='source',
            field=models.CharField(choices=[('entra', 'Microsoft Entra ID'), ('entra_licenses', 'Microsoft Entra licences')], max_length=20),
        ),
        migrations.AlterField(
            model_name='observation',
            name='kind',
            field=models.CharField(choices=[('application', 'Application'), ('component', 'Component'), ('suggested_integration', 'Suggested integration'), ('suggested_owner', 'Suggested owner'), ('license', 'Licence seat counts'), ('ai_agent', 'AI agent')], max_length=30),
        ),
    ]
