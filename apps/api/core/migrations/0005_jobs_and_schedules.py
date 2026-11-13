"""Jobs: the Job queue claimed with FOR UPDATE SKIP LOCKED and the Schedule run
state behind the System page and /readyz. New tables only; nothing existing changes. Forward-only."""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_organization_edition'),
    ]

    operations = [
        migrations.CreateModel(
            name='Schedule',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=80, unique=True)),
                ('cron', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('next_due', models.DateTimeField()),
                ('lease_token', models.UUIDField(blank=True, null=True)),
                ('lease_expires', models.DateTimeField(blank=True, null=True)),
                ('last_started', models.DateTimeField(blank=True, null=True)),
                ('last_finished', models.DateTimeField(blank=True, null=True)),
                ('last_succeeded', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
                ('last_result', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='Job',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(max_length=80)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('succeeded', 'Succeeded'), ('failed', 'Failed'), ('abandoned', 'Abandoned')], default='queued', max_length=10)),
                ('run_after', models.DateTimeField(default=django.utils.timezone.now)),
                ('lease_token', models.UUIDField(blank=True, null=True)),
                ('lease_expires', models.DateTimeField(blank=True, null=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('max_attempts', models.PositiveSmallIntegerField(default=3)),
                ('last_error', models.TextField(blank=True, default='')),
                ('result', models.JSONField(blank=True, default=dict)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'indexes': [models.Index(condition=models.Q(('status', 'queued')), fields=['run_after'], name='core_job_queued'), models.Index(condition=models.Q(('status', 'running')), fields=['lease_expires'], name='core_job_running'), models.Index(fields=['organization', 'status'], name='core_job_org_status')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_job_org_id')],
            },
        ),
    ]
