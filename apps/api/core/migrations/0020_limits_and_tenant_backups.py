"""Limits, tenant backups and export: per-organization limit overrides set by platform staff
(core/limits.py) with `organization.has_limit_overrides` beside them (false for every existing organization, which
has none), and two more export purposes, the nightly tenant backup and an export the operator made on the
organization's request. The purpose change is a choices change only (no SQL).

Forward-only."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_platform_admin'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='has_limit_overrides',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AlterField(
            model_name='orgexport',
            name='purpose',
            field=models.CharField(choices=[('export', 'Export on request'), ('deletion', 'Before deletion'), ('backup', 'Nightly backup'), ('operator', 'Requested by the operator')], default='export', max_length=10),
        ),
        migrations.CreateModel(
            name='OrganizationLimitOverride',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('limit', models.CharField(max_length=40)),
                ('value', models.PositiveBigIntegerField(blank=True, null=True)),
                ('reason', models.CharField(max_length=500)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['limit'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_organizationlimitoverride_org_id'), models.UniqueConstraint(fields=('organization', 'limit'), name='core_limitoverride_one')],
            },
        ),
    ]
