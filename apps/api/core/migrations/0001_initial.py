"""Split: the core app takes over organization, membership, useridentity, workerheartbeat, auditevent from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `core`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import core.models
import datetime
import django.contrib.postgres.fields
import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['organization', 'membership', 'useridentity', 'workerheartbeat', 'auditevent']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "core")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "core", "catalog")


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('catalog', '0018_import_tags_tokens'),
        ('contenttypes', '0002_remove_content_type_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='WorkerHeartbeat',
                    fields=[
                        ('name', models.CharField(max_length=40, primary_key=True, serialize=False)),
                        ('last_tick', models.DateTimeField()),
                    ],
                    options={
                        'db_table': 'catalog_workerheartbeat',
                    },
                ),
                migrations.CreateModel(
                    name='Organization',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('name', models.CharField(max_length=200)),
                        ('timezone', models.CharField(default='UTC', max_length=64)),
                        ('review_interval_days', models.PositiveIntegerField(default=90)),
                        ('renewal_lead_days', models.PositiveIntegerField(default=90)),
                        ('reminder_thresholds', django.contrib.postgres.fields.ArrayField(base_field=models.PositiveIntegerField(), default=core.models.default_thresholds, size=None)),
                        ('digest_time', models.TimeField(default=datetime.time(8, 0))),
                        ('coordinator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_organization',
                    },
                ),
                migrations.CreateModel(
                    name='Membership',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('role', models.CharField(choices=[('admin', 'Admin'), ('editor', 'Editor'), ('viewer', 'Viewer')], max_length=10)),
                        ('must_change_password', models.BooleanField(default=False)),
                        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='membership', to=settings.AUTH_USER_MODEL)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='memberships', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_membership',
                    },
                ),
                migrations.CreateModel(
                    name='AuditEvent',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('action', models.CharField(max_length=60)),
                        ('target_type', models.CharField(blank=True, max_length=40)),
                        ('target_id', models.UUIDField(null=True)),
                        ('changes', models.JSONField(default=dict)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_auditevent',
                        'ordering': ['-created_at'],
                    },
                ),
                migrations.CreateModel(
                    name='UserIdentity',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('tenant_id', models.CharField(max_length=64)),
                        ('object_id', models.CharField(max_length=64)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('last_login_at', models.DateTimeField(blank=True, null=True)),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='identities', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_useridentity',
                        'constraints': [models.UniqueConstraint(fields=('tenant_id', 'object_id'), name='catalog_useridentity_unique')],
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
