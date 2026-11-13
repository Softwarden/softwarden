# Job.organization becomes CASCADE (PROTECT would block
# deleting an organization; its jobs are worker bookkeeping, and SET_NULL would turn them into installation-wide jobs).
# Django enforces on_delete, so the column is unchanged. The partial index serves the notify.deliveries sweep's orphan
# check (a known cost).

import django.db.models.deletion
import django.db.models.fields.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_backup_state'),
    ]

    operations = [
        migrations.AlterField(
            model_name='job',
            name='organization',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='core.organization'),
        ),
        migrations.AddIndex(
            model_name='job',
            index=models.Index(django.db.models.fields.json.KeyTransform('delivery_id', 'payload'), condition=models.Q(('status__in', ['queued', 'running'])), name='core_job_delivery'),
        ),
    ]
