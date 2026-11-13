"""Reminders: the Reminder table (notify/models.py), with composite foreign keys to its source's renewal
cycle, contract, record and licence pool, added with the table, which is empty. Forward-only."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (constraint, column, referenced table)
    ("notify_reminder_org_cycle_fk", "cycle_id", "catalog_renewalcycle"),
    ("notify_reminder_org_contract_fk", "contract_id", "money_contract"),
    ("notify_reminder_org_resource_fk", "resource_id", "catalog_resource"),
    ("notify_reminder_org_license_pool_fk", "license_pool_id", "money_licensepool"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0038_snapshotlink_type_names'),
        ('core', '0008_auditevent_append_only'),
        ('money', '0007_calendar_feed'),
        ('notify', '0002_preference_per_membership'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Reminder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source_type', models.CharField(choices=[('renewal_cycle', 'Renewal action deadline'), ('contract', 'Contract cancellation deadline'), ('periodic_review', 'Periodic review'), ('record_review', 'Record review'), ('custom_date', 'Custom date'), ('credential_expiry', 'Credential expiry')], max_length=30)),
                ('source_id', models.UUIDField()),
                ('source_field', models.CharField(blank=True, max_length=100)),
                ('due_date', models.DateField()),
                ('deadline_label', models.CharField(blank=True, max_length=30)),
                ('title', models.CharField(max_length=300)),
                ('label', models.CharField(max_length=300)),
                ('status', models.CharField(choices=[('open', 'Open'), ('snoozed', 'Snoozed'), ('acknowledged', 'Acknowledged'), ('done', 'Done'), ('obsolete', 'No longer applies')], default='open', max_length=20)),
                ('snoozed_until', models.DateField(blank=True, null=True)),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('resolution_kind', models.CharField(blank=True, choices=[('done', 'Done'), ('risk_accepted', 'Risk accepted'), ('no_longer_applies', 'No longer applies'), ('source_changed', 'Source changed')], max_length=20)),
                ('resolution_note', models.TextField(blank=True)),
                ('resolution_evidence', models.CharField(blank=True, max_length=500)),
                ('notify_cycle', models.PositiveIntegerField(default=0)),
                ('acknowledged_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('contract', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.contract')),
                ('cycle', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.renewalcycle')),
                ('license_pool', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.licensepool')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('resource', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.resource')),
            ],
            options={
                'ordering': ['due_date', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'status', 'due_date'], name='notify_reminder_status_due'), models.Index(fields=['organization', 'source_type', 'source_id'], name='notify_reminder_source')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='notify_reminder_org_id'), models.UniqueConstraint(fields=('organization', 'source_type', 'source_id', 'source_field', 'due_date'), name='notify_reminder_unique_occurrence'), models.CheckConstraint(condition=models.Q(models.Q(('snoozed_until__isnull', False), ('status', 'snoozed')), models.Q(models.Q(('status', 'snoozed'), _negated=True), ('snoozed_until__isnull', True)), _connector='OR'), name='notify_reminder_snooze_date')],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE notify_reminder ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE notify_reminder DROP CONSTRAINT {name};\n" for name, *_rest in reversed(KEYS)
            ),
        ),
    ]
