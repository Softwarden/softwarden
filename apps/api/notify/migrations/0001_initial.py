"""Split: the notify app takes over notificationpreference, digestrun, digest, reminderdispatch, notificationsnooze from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `notify`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['notificationpreference', 'digestrun', 'digest', 'reminderdispatch', 'notificationsnooze']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "notify")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "notify", "catalog")


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('catalog', '0018_import_tags_tokens'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('core', '0001_initial'),
        ('money', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='DigestRun',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('for_date', models.DateField()),
                        ('started_at', models.DateTimeField(auto_now_add=True)),
                        ('finished_at', models.DateTimeField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_digestrun',
                        'ordering': ['-for_date'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='Digest',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('for_date', models.DateField()),
                        ('status', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed'), ('skipped', "Not sent: email isn't configured")], default='pending', max_length=10)),
                        ('item_count', models.PositiveIntegerField(default=0)),
                        ('subject', models.CharField(max_length=200)),
                        ('text_body', models.TextField()),
                        ('html_body', models.TextField()),
                        ('attempts', models.PositiveIntegerField(default=0)),
                        ('last_error', models.CharField(blank=True, max_length=500)),
                        ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                        ('sent_at', models.DateTimeField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='digests', to='notify.digestrun')),
                    ],
                    options={
                        'db_table': 'catalog_digest',
                        'ordering': ['-for_date', 'user_id'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='NotificationPreference',
                    fields=[
                        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='notification_preference', serialize=False, to=settings.AUTH_USER_MODEL)),
                        ('digest_enabled', models.BooleanField(default=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_notificationpreference',
                    },
                ),
                migrations.CreateModel(
                    name='NotificationSnooze',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('subject_type', models.CharField(max_length=30)),
                        ('subject_id', models.CharField(max_length=80)),
                        ('kind', models.CharField(choices=[('snooze', 'Snooze'), ('acknowledge', 'Acknowledge')], max_length=12)),
                        ('until', models.DateField(blank=True, null=True)),
                        ('threshold', models.IntegerField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_notificationsnooze',
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ReminderDispatch',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('threshold_days', models.PositiveIntegerField()),
                        ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reminders', to='money.renewalcycle')),
                        ('digest', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reminders', to='notify.digest')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_reminderdispatch',
                        'ordering': ['-threshold_days'],
                        'abstract': False,
                    },
                ),
                migrations.AddConstraint(
                    model_name='digestrun',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_digestrun_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='digestrun',
                    constraint=models.UniqueConstraint(fields=('organization', 'for_date'), name='catalog_digestrun_unique_date'),
                ),
                migrations.AddIndex(
                    model_name='digest',
                    index=models.Index(fields=['status', 'next_attempt_at'], name='catalog_digest_due'),
                ),
                migrations.AddConstraint(
                    model_name='digest',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_digest_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='digest',
                    constraint=models.UniqueConstraint(fields=('organization', 'user', 'for_date'), name='catalog_digest_unique_day'),
                ),
                migrations.AddConstraint(
                    model_name='notificationsnooze',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_notificationsnooze_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='notificationsnooze',
                    constraint=models.UniqueConstraint(fields=('organization', 'user', 'subject_type', 'subject_id'), name='catalog_snooze_unique'),
                ),
                migrations.AddConstraint(
                    model_name='reminderdispatch',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_reminderdispatch_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='reminderdispatch',
                    constraint=models.UniqueConstraint(fields=('cycle', 'threshold_days'), name='catalog_reminderdispatch_unique'),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
