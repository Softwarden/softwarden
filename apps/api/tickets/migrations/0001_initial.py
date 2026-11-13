"""Ticket links (tickets/models.py), in an app of its own so it doesn't collide with the catalog, notify and
discovery migrations other features add. Both tables are tenant tables: composite foreign keys keep a
link's assessment, renewal and cycle, and a ticket connection's Halo connection, in the row's organization. The
tables are new and empty, so the keys are added with them. Depends on catalog 0040, core 0020 and money 0007, the
newest of each at the time."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, columns, referenced table, referenced columns)
    (
        "tickets_ticketlink",
        "tickets_ticketlink_org_assessment_fk",
        "organization_id, assessment_id",
        "catalog_changeassessment",
        "organization_id, id",
    ),
    (
        "tickets_ticketlink",
        "tickets_ticketlink_org_obligation_fk",
        "organization_id, obligation_id",
        "catalog_renewalobligation",
        "organization_id, id",
    ),
    (
        "tickets_ticketlink",
        "tickets_ticketlink_org_cycle_fk",
        "organization_id, cycle_id",
        "catalog_renewalcycle",
        "organization_id, id",
    ),
    (
        "tickets_ticketconnection",
        "tickets_ticketconnection_org_halo_fk",
        "organization_id, halo_connection_id",
        "core_haloconnection",
        "organization_id, id",
    ),
]


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('catalog', '0040_vendor_risk'),
        ('core', '0020_limits_and_tenant_backups'),
        ('money', '0007_calendar_feed'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TicketConnection',
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
                ('system', models.CharField(choices=[('jira', 'Jira'), ('servicenow', 'ServiceNow'), ('halo', 'HaloITSM')], max_length=20)),
                ('base_url', models.CharField(blank=True, max_length=300)),
                ('username', models.CharField(blank=True, max_length=254)),
                ('create_enabled', models.BooleanField(default=False)),
                ('create_target', models.CharField(blank=True, max_length=100)),
                ('create_issue_type', models.CharField(blank=True, max_length=60)),
                ('halo_connection', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ticket_connections', to='core.haloconnection')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('tested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'ticket connection',
                'ordering': ['name'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='tickets_ticketconnection_org_id'), models.UniqueConstraint(fields=('organization', 'system'), name='tickets_ticketconnection_system'), models.CheckConstraint(condition=models.Q(models.Q(('halo_connection__isnull', False), ('system', 'halo')), models.Q(models.Q(('system', 'halo'), _negated=True), ('halo_connection__isnull', True)), _connector='OR'), name='tickets_ticketconnection_halo_pair')],
            },
        ),
        migrations.CreateModel(
            name='TicketLink',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('system', models.CharField(choices=[('jira', 'Jira'), ('servicenow', 'ServiceNow'), ('halo', 'HaloITSM'), ('other', 'Other')], max_length=20)),
                ('external_key', models.CharField(blank=True, max_length=100)),
                ('url', models.CharField(max_length=2048)),
                ('title', models.CharField(blank=True, max_length=300)),
                ('status', models.CharField(choices=[('open', 'Open'), ('in_progress', 'In progress'), ('resolved', 'Resolved'), ('closed', 'Closed'), ('unknown', 'Unknown')], default='unknown', max_length=20)),
                ('status_source', models.CharField(choices=[('manual', 'Set by hand'), ('refreshed', 'Read from the system')], default='manual', max_length=20)),
                ('external_status', models.CharField(blank=True, max_length=100)),
                ('status_checked_at', models.DateTimeField(blank=True, null=True)),
                ('status_error', models.CharField(blank=True, max_length=300)),
                ('created_in_system', models.BooleanField(default=False)),
                ('assessment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='ticket_links', to='catalog.changeassessment')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('cycle', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='ticket_links', to='money.renewalcycle')),
                ('obligation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='ticket_links', to='money.renewalobligation')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'verbose_name': 'ticket link',
                'ordering': ['created_at', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'system', 'status'], name='tickets_link_refresh')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='tickets_ticketlink_org_id'), models.CheckConstraint(condition=models.Q(models.Q(('assessment__isnull', False), ('obligation__isnull', True)), models.Q(('assessment__isnull', True), ('obligation__isnull', False)), _connector='OR'), name='tickets_ticketlink_one_target'), models.CheckConstraint(condition=models.Q(('cycle__isnull', True), ('obligation__isnull', False), _connector='OR'), name='tickets_ticketlink_cycle_needs_renewal'), models.UniqueConstraint(condition=models.Q(('assessment__isnull', False)), fields=('organization', 'assessment', 'url'), name='tickets_ticketlink_assessment_url'), models.UniqueConstraint(condition=models.Q(('obligation__isnull', False)), fields=('organization', 'obligation', 'url'), name='tickets_ticketlink_renewal_url')],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
                f"REFERENCES {target} ({key}) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, columns, target, key in KEYS
            ),
            reverse_sql="".join(f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, *_ in reversed(KEYS)),
        ),
    ]
