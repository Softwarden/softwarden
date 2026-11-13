"""AuditEvent extensions: request_id, related_target, prechange/postchange snapshots,
and the indexes the audit log filters use. Existing events keep nulls (and an empty related_target_type).
Forward-only."""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_bootstrap_state'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='auditevent',
            name='postchange',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='prechange',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='related_target_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='related_target_type',
            field=models.CharField(blank=True, db_default='', default='', max_length=40),
        ),
        migrations.AddField(
            model_name='auditevent',
            name='request_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['organization', 'created_at', 'id'], name='catalog_audit_org_time'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['organization', 'target_id'], name='catalog_audit_org_target'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(condition=models.Q(('related_target_id__isnull', False)), fields=['organization', 'related_target_id'], name='catalog_audit_org_related'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(condition=models.Q(('request_id__isnull', False)), fields=['organization', 'request_id'], name='catalog_audit_org_request'),
        ),
    ]
