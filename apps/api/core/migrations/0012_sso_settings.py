"""Per-organization OpenID Connect sign-in (core/sso.py): SsoSettings, one per organization, with the active
and draft provider configuration, Fernet secret slots, revisions and the draft's test; SsoDomain, the sign-in domains
that find an organization from a work email, unique across the installation. Forward-only."""

import django.contrib.postgres.fields
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_useridentity_issuer_subject'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SsoDomain',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(max_length=253)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['domain'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_ssodomain_org_id'), models.UniqueConstraint(fields=('domain',), name='core_ssodomain_unique')],
            },
        ),
        migrations.CreateModel(
            name='SsoSettings',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('status', models.CharField(choices=[('legacy', 'Not set up here'), ('active', 'On'), ('disabled', 'Off')], default='legacy', max_length=10)),
                ('provider', models.CharField(blank=True, choices=[('entra', 'Microsoft Entra ID'), ('okta', 'Okta'), ('google', 'Google Workspace'), ('oidc', 'OpenID Connect')], default='', max_length=10)),
                ('tenant', models.CharField(blank=True, default='', max_length=64)),
                ('okta_domain', models.CharField(blank=True, default='', max_length=253)),
                ('okta_server', models.CharField(blank=True, default='', max_length=100)),
                ('discovery_url', models.CharField(blank=True, default='', max_length=2000)),
                ('issuer', models.CharField(blank=True, default='', max_length=500)),
                ('hosted_domains', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=253), blank=True, default=list, size=None)),
                ('client_id', models.CharField(blank=True, default='', max_length=255)),
                ('secret_ciphertext', models.TextField(blank=True, default='')),
                ('secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('active_revision', models.PositiveIntegerField(default=0)),
                ('activated_at', models.DateTimeField(blank=True, null=True)),
                ('draft_provider', models.CharField(blank=True, choices=[('entra', 'Microsoft Entra ID'), ('okta', 'Okta'), ('google', 'Google Workspace'), ('oidc', 'OpenID Connect')], default='', max_length=10)),
                ('draft_tenant', models.CharField(blank=True, default='', max_length=64)),
                ('draft_okta_domain', models.CharField(blank=True, default='', max_length=253)),
                ('draft_okta_server', models.CharField(blank=True, default='', max_length=100)),
                ('draft_discovery_url', models.CharField(blank=True, default='', max_length=2000)),
                ('draft_issuer', models.CharField(blank=True, default='', max_length=500)),
                ('draft_hosted_domains', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=253), blank=True, default=list, size=None)),
                ('draft_client_id', models.CharField(blank=True, default='', max_length=255)),
                ('draft_secret_ciphertext', models.TextField(blank=True, default='')),
                ('draft_secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('draft_secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('draft_revision', models.PositiveIntegerField(default=0)),
                ('draft_tested_revision', models.PositiveIntegerField(blank=True, null=True)),
                ('draft_test_ok', models.BooleanField(blank=True, null=True)),
                ('draft_tested_at', models.DateTimeField(blank=True, null=True)),
                ('draft_test_code', models.CharField(blank=True, default='', max_length=60)),
                ('draft_test_detail', models.CharField(blank=True, default='', max_length=300)),
                ('activated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('draft_tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_ssosettings_org_id'), models.UniqueConstraint(fields=('organization',), name='core_ssosettings_one_per_org')],
            },
        ),
    ]
