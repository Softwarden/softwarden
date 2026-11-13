"""Access and provisioning: Invitation (a hashed, single-use, revocable link that creates the membership when
it is accepted; one live invitation per email per organization) and PendingAccess (an unknown single sign-on identity
waiting for an Admin while the organization's auto-provision is on; one row per identity). Forward-only."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_sso_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Invitation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('email', models.CharField(max_length=254)),
                ('name', models.CharField(blank=True, default='', max_length=200)),
                ('role', models.CharField(choices=[('admin', 'Admin'), ('editor', 'Editor'), ('viewer', 'Viewer')], max_length=10)),
                ('token_hash', models.CharField(max_length=64, unique=True)),
                ('expires_at', models.DateTimeField()),
                ('accepted_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('accepted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('invited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_invitation_org_id'), models.UniqueConstraint(condition=models.Q(('accepted_at__isnull', True), ('revoked_at__isnull', True)), fields=('organization', 'email'), name='core_invitation_one_live')],
            },
        ),
        migrations.CreateModel(
            name='PendingAccess',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('issuer', models.CharField(max_length=500)),
                ('subject', models.CharField(max_length=255)),
                ('email', models.CharField(blank=True, default='', max_length=254)),
                ('name', models.CharField(blank=True, default='', max_length=200)),
                ('first_seen', models.DateTimeField()),
                ('last_seen', models.DateTimeField()),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['-last_seen'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_pendingaccess_org_id'), models.UniqueConstraint(fields=('organization', 'issuer', 'subject'), name='core_pendingaccess_identity')],
            },
        ),
    ]
