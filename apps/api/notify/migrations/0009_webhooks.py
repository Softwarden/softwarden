"""Webhooks (notify/webhooks.py): WebhookDestination becomes a Webhook (event subscriptions,
a description, a previous signing secret slot for rotation, the delivery summary and auto-disable state); Delivery
gains the event type and id and the kind `event`; DeliveryAttempt is the per-attempt delivery log, with a composite
foreign key to its delivery. Existing destinations subscribe to nothing, so they keep receiving only what
notification rules send them. Existing webhook deliveries get their event type from their stored content."""

import django.contrib.postgres.fields
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

ATTEMPT_DELIVERY_FK = "notify_deliveryattempt_org_delivery_fk"


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_halo_scope_read_assets'),
        ('notify', '0008_saved_view_subscriptions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DeliveryAttempt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('number', models.PositiveSmallIntegerField()),
                ('started_at', models.DateTimeField()),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('outcome', models.CharField(choices=[('sent', 'Sent'), ('retrying', 'Failed, will retry'), ('failed', 'Failed'), ('skipped', 'Skipped')], max_length=10)),
                ('status_code', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('response_excerpt', models.CharField(blank=True, max_length=500)),
                ('error', models.CharField(blank=True, max_length=500)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'ordering': ['started_at', 'number'],
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='delivery',
            name='event_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='delivery',
            name='event_type',
            field=models.CharField(blank=True, max_length=60),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='consecutive_failures',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='description',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='disabled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='disabled_reason',
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='events',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=60), blank=True, default=list, size=None),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='last_delivery_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='last_delivery_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='last_delivery_outcome',
            field=models.CharField(blank=True, max_length=10),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='last_delivery_status_code',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='previous_secret_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='previous_signing_secret_ciphertext',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='previous_signing_secret_key_id',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='webhookdestination',
            name='previous_signing_secret_set_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='delivery',
            name='kind',
            field=models.CharField(choices=[('reminder', 'Reminder'), ('escalation', 'Overdue escalation'), ('digest', 'Digest'), ('subscription', 'Saved view subscription'), ('test', 'Test'), ('event', 'Webhook event')], default='reminder', max_length=20),
        ),
        migrations.AddIndex(
            model_name='delivery',
            index=models.Index(fields=['organization', 'webhook_destination', 'created_at'], name='notify_delivery_webhook'),
        ),
        migrations.AddField(
            model_name='deliveryattempt',
            name='delivery',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attempt_log', to='notify.delivery'),
        ),
        migrations.AddField(
            model_name='deliveryattempt',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddIndex(
            model_name='deliveryattempt',
            index=models.Index(fields=['organization', 'delivery'], name='notify_deliveryattempt_of'),
        ),
        migrations.AddConstraint(
            model_name='deliveryattempt',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_deliveryattempt_org_id'),
        ),
        migrations.RunSQL(
            sql=f"ALTER TABLE notify_deliveryattempt ADD CONSTRAINT {ATTEMPT_DELIVERY_FK} FOREIGN KEY "
            "(organization_id, delivery_id) REFERENCES notify_delivery (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql=f"ALTER TABLE notify_deliveryattempt DROP CONSTRAINT {ATTEMPT_DELIVERY_FK};",
        ),
        migrations.RunSQL(
            sql="UPDATE notify_delivery SET event_type = LEFT(content->>'event', 60) "
            "WHERE channel = 'webhook' AND event_type = '' AND content ? 'event';",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
