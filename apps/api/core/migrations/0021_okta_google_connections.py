"""Okta and Google Workspace discovery: per-organization OktaConnection (an Okta API service app signing in
with a private-key client assertion) and GoogleWorkspaceConnection (a service account with domain-wide delegation acting
as a Workspace administrator), each with the Connection secret slot (Fernet, write-only), revision and test state, and
the per-organization unique name. New, empty tables. discovery 0017 points connectors at them. Forward-only."""

import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_limits_and_tenant_backups'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GoogleWorkspaceConnection',
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
                ('admin_email', models.CharField(max_length=254)),
                ('service_account_email', models.CharField(blank=True, max_length=254)),
                ('service_account_client_id', models.CharField(blank=True, max_length=40)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_googleworkspaceconnection_org_id'), models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='core_googleconn_name')],
            },
        ),
        migrations.CreateModel(
            name='OktaConnection',
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
                ('client_id', models.CharField(max_length=100)),
                ('key_id', models.CharField(blank=True, max_length=200)),
                ('scope', models.CharField(default='okta.apps.read', max_length=200)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_oktaconnection_org_id'), models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='core_oktaconn_name')],
            },
        ),
    ]
