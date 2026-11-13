# The webhook outbox (notify/webhooks.py). Replaces the commit hooks on Django's private
# run_on_commit list: an event is a WebhookEvent row written in the write's own transaction, which the worker's
# `webhooks.outbox` schedule fans out into deliveries. `sequence` orders the drain; its sequence is created first and
# owned by the column, so dropping the table drops it.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_backup_state'),
        ('notify', '0009_webhooks'),
    ]

    operations = [
        migrations.RunSQL(
            "CREATE SEQUENCE notify_webhookevent_sequence AS bigint",
            reverse_sql="DROP SEQUENCE IF EXISTS notify_webhookevent_sequence",
        ),
        migrations.CreateModel(
            name='WebhookEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('sequence', models.BigIntegerField(db_default=models.Func(models.Value('notify_webhookevent_sequence'), function='nextval'), editable=False, unique=True)),
                ('event_type', models.CharField(max_length=60)),
                ('content', models.JSONField(default=dict)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['sequence'],
                'abstract': False,
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='notify_webhookevent_org_id')],
            },
        ),
        migrations.RunSQL(
            "ALTER SEQUENCE notify_webhookevent_sequence OWNED BY notify_webhookevent.sequence",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
