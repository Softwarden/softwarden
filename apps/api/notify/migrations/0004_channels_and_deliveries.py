"""Channels and deliveries: per-organization EmailSettings, Teams and generic webhook destinations (secret
slots) and the Delivery table behind the notification log, with composite foreign keys to the destinations, the
reminder and the delivery it resends. All tables are new and empty. Forward-only."""

import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (constraint, column, referenced table)
    ("notify_delivery_org_teams_destination_fk", "teams_destination_id", "notify_teamsdestination"),
    ("notify_delivery_org_webhook_destination_fk", "webhook_destination_id", "notify_webhookdestination"),
    ("notify_delivery_org_reminder_fk", "reminder_id", "notify_reminder"),
    ("notify_delivery_org_resend_of_fk", "resend_of_id", "notify_delivery"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_auditevent_append_only'),
        ('notify', '0003_reminder'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamsDestination',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_test_at', models.DateTimeField(blank=True, null=True)),
                ('last_test_ok', models.BooleanField(blank=True, null=True)),
                ('last_test_detail', models.CharField(blank=True, max_length=500)),
                ('name', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('url_ciphertext', models.TextField(blank=True, default='')),
                ('url_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('url_set_at', models.DateTimeField(blank=True, null=True)),
                ('url_host', models.CharField(blank=True, max_length=253)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='WebhookDestination',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_test_at', models.DateTimeField(blank=True, null=True)),
                ('last_test_ok', models.BooleanField(blank=True, null=True)),
                ('last_test_detail', models.CharField(blank=True, max_length=500)),
                ('name', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('url_ciphertext', models.TextField(blank=True, default='')),
                ('url_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('url_set_at', models.DateTimeField(blank=True, null=True)),
                ('url_host', models.CharField(blank=True, max_length=253)),
                ('signing_secret_ciphertext', models.TextField(blank=True, default='')),
                ('signing_secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('signing_secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Delivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('channel', models.CharField(choices=[('email', 'Email'), ('teams', 'Teams'), ('webhook', 'Webhook')], max_length=10)),
                ('kind', models.CharField(choices=[('reminder', 'Reminder'), ('escalation', 'Overdue escalation'), ('digest', 'Digest'), ('subscription', 'Saved view subscription'), ('test', 'Test')], default='reminder', max_length=20)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('sending', 'Sending'), ('sent', 'Sent'), ('failed', 'Failed'), ('skipped', 'Skipped')], default='queued', max_length=10)),
                ('dedupe_key', models.CharField(max_length=200)),
                ('recipient', models.CharField(blank=True, max_length=254)),
                ('destination_name', models.CharField(blank=True, max_length=100)),
                ('subject', models.CharField(max_length=300)),
                ('content', models.JSONField(default=dict)),
                ('rule_name', models.CharField(blank=True, max_length=200)),
                ('lead_time_days', models.IntegerField(blank=True, null=True)),
                ('overdue_step', models.PositiveIntegerField(blank=True, null=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('max_attempts', models.PositiveSmallIntegerField(default=4)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('claim_token', models.UUIDField(blank=True, null=True)),
                ('claimed_until', models.DateTimeField(blank=True, null=True)),
                ('client_request_id', models.UUIDField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=500)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('reminder', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='notify.reminder')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('resend_of', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='resends', to='notify.delivery')),
                ('teams_destination', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='notify.teamsdestination')),
                ('webhook_destination', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='notify.webhookdestination')),
            ],
            options={
                'ordering': ['-created_at', '-id'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='EmailSettings',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_test_at', models.DateTimeField(blank=True, null=True)),
                ('last_test_ok', models.BooleanField(blank=True, null=True)),
                ('last_test_detail', models.CharField(blank=True, max_length=500)),
                ('transport', models.CharField(choices=[('installation_default', "Installation default (the server's SMTP settings)"), ('graph', 'Microsoft Graph (sendMail)'), ('smtp', 'SMTP server')], default='installation_default', max_length=30)),
                ('graph_tenant_id', models.CharField(blank=True, max_length=253)),
                ('graph_client_id', models.CharField(blank=True, max_length=36)),
                ('graph_secret_ciphertext', models.TextField(blank=True, default='')),
                ('graph_secret_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('graph_secret_set_at', models.DateTimeField(blank=True, null=True)),
                ('sender_mailbox', models.CharField(blank=True, max_length=254)),
                ('smtp_host', models.CharField(blank=True, max_length=253)),
                ('smtp_port', models.PositiveIntegerField(blank=True, null=True)),
                ('smtp_security', models.CharField(choices=[('starttls', 'STARTTLS'), ('tls', 'TLS (implicit, usually port 465)'), ('none', 'None (local relays and Mailpit only)')], default='starttls', max_length=10)),
                ('smtp_username', models.CharField(blank=True, max_length=254)),
                ('smtp_password_ciphertext', models.TextField(blank=True, default='')),
                ('smtp_password_key_id', models.CharField(blank=True, default='', max_length=20)),
                ('smtp_password_set_at', models.DateTimeField(blank=True, null=True)),
                ('from_address', models.CharField(blank=True, max_length=254)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='notify_emailsettings_org_id'), models.UniqueConstraint(fields=('organization',), name='notify_emailsettings_one_per_org')],
            },
        ),
        migrations.AddConstraint(
            model_name='teamsdestination',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_teamsdestination_org_id'),
        ),
        migrations.AddConstraint(
            model_name='teamsdestination',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), models.F('organization'), name='notify_teamsdestination_unique_name'),
        ),
        migrations.AddConstraint(
            model_name='webhookdestination',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_webhookdestination_org_id'),
        ),
        migrations.AddConstraint(
            model_name='webhookdestination',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), models.F('organization'), name='notify_webhookdestination_unique_name'),
        ),
        migrations.AddIndex(
            model_name='delivery',
            index=models.Index(fields=['status', 'next_attempt_at'], name='notify_delivery_due'),
        ),
        migrations.AddIndex(
            model_name='delivery',
            index=models.Index(fields=['organization', 'created_at'], name='notify_delivery_log'),
        ),
        migrations.AddIndex(
            model_name='delivery',
            index=models.Index(fields=['organization', 'reminder'], name='notify_delivery_reminder'),
        ),
        migrations.AddConstraint(
            model_name='delivery',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_delivery_org_id'),
        ),
        migrations.AddConstraint(
            model_name='delivery',
            constraint=models.UniqueConstraint(fields=('organization', 'dedupe_key'), name='notify_delivery_dedupe'),
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE notify_delivery ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE notify_delivery DROP CONSTRAINT {name};\n" for name, *_rest in reversed(KEYS)
            ),
        ),
    ]
