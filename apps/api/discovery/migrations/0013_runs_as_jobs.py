"""Runs: each connector run is its own `discovery.run` job with a lease. ConnectorRun gains the `queued` status
(`queued_at`, `job_id`; `started_at` is null until the job starts it), the run's lease (`lease_token`, `lease_expires`),
trigger choices and a history index. The one-running constraint becomes one queued-or-running run per connector: no
existing row is queued, so the new constraint holds for every row the old one allowed. Forward-only; the reverse
only exists so a test database can be unmigrated."""

import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_connections'),
        ('discovery', '0012_reconciliation'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='connectorrun',
            options={'ordering': ['-created_at', '-id']},
        ),
        migrations.RemoveConstraint(
            model_name='connectorrun',
            name='catalog_connectorrun_one_running',
        ),
        migrations.AddField(
            model_name='connectorrun',
            name='job_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connectorrun',
            name='lease_expires',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connectorrun',
            name='lease_token',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='connectorrun',
            name='queued_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='connectorrun',
            name='started_at',
            field=models.DateTimeField(blank=True, default=django.utils.timezone.now, null=True),
        ),
        migrations.AlterField(
            model_name='connectorrun',
            name='status',
            field=models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('succeeded', 'Succeeded'), ('failed', 'Failed')], default='running', max_length=10),
        ),
        migrations.AlterField(
            model_name='connectorrun',
            name='trigger',
            field=models.CharField(choices=[('manual', 'Run now'), ('scheduled', 'Daily schedule')], max_length=10),
        ),
        migrations.AddIndex(
            model_name='connectorrun',
            index=models.Index(fields=['organization', '-created_at'], name='discovery_run_history'),
        ),
        migrations.AddConstraint(
            model_name='connectorrun',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['queued', 'running'])), fields=('connector',), name='discovery_connectorrun_one_active'),
        ),
    ]
