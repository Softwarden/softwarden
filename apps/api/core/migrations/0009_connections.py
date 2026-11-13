"""Per-organization MicrosoftConnection and HaloConnection (core/models.py Connection): credentials with a
Fernet secret slot, a revision and the revision their last successful test covered. discovery 0005 points connectors
at them and moves the Entra connectors' credentials over. Forward-only."""

import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_auditevent_append_only'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HaloConnection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True)),
                ('secret_ciphertext', models.TextField(blank=True, default='')),
                ('secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('tested_revision', models.PositiveIntegerField(blank=True, null=True)),
                ('tested_at', models.DateTimeField(blank=True, null=True)),
                ('test_ok', models.BooleanField(blank=True, null=True)),
                ('test_result', models.JSONField(blank=True, default=dict)),
                ('base_url', models.CharField(max_length=300)),
                ('client_id', models.CharField(max_length=200)),
                ('scope', models.CharField(default='read:assets read:tickets', max_length=200)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_haloconnection_org_id'), models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='core_haloconn_name')],
            },
        ),
        migrations.CreateModel(
            name='MicrosoftConnection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True)),
                ('secret_ciphertext', models.TextField(blank=True, default='')),
                ('secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('tested_revision', models.PositiveIntegerField(blank=True, null=True)),
                ('tested_at', models.DateTimeField(blank=True, null=True)),
                ('test_ok', models.BooleanField(blank=True, null=True)),
                ('test_result', models.JSONField(blank=True, default=dict)),
                ('tenant_id', models.CharField(max_length=253)),
                ('client_id', models.CharField(blank=True, max_length=36)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_microsoftconnection_org_id'), models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='core_microsoftconn_name')],
            },
        ),
    ]
