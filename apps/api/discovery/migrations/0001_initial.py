"""Split: the discovery app takes over connector, connectorrun, observation, reconciliationdecision from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `discovery`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import django.contrib.postgres.indexes
import django.db.models.deletion
import django.db.models.functions.text
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['connector', 'connectorrun', 'observation', 'reconciliationdecision']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "discovery")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "discovery", "catalog")


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('catalog', '0018_import_tags_tokens'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('core', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Connector',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('source', models.CharField(choices=[('entra', 'Microsoft Entra ID')], max_length=20)),
                        ('name', models.CharField(max_length=200)),
                        ('enabled', models.BooleanField(default=True)),
                        ('config', models.JSONField(default=dict)),
                        ('scope', models.JSONField(default=dict)),
                        ('secret_ciphertext', models.TextField(blank=True)),
                        ('secret_set_at', models.DateTimeField(blank=True, null=True)),
                        ('schedule', models.CharField(choices=[('manual', 'Only when run'), ('daily', 'Daily')], default='manual', max_length=10)),
                        ('run_requested_at', models.DateTimeField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('reviewer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('run_requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_connector',
                        'ordering': ['name'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ConnectorRun',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('trigger', models.CharField(max_length=10)),
                        ('status', models.CharField(choices=[('running', 'Running'), ('succeeded', 'Succeeded'), ('failed', 'Failed')], default='running', max_length=10)),
                        ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                        ('finished_at', models.DateTimeField(blank=True, null=True)),
                        ('counts', models.JSONField(default=dict)),
                        ('error', models.CharField(blank=True, max_length=500)),
                        ('connector', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='runs', to='discovery.connector')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_connectorrun',
                        'ordering': ['-started_at'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='Observation',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('kind', models.CharField(choices=[('application', 'Application'), ('component', 'Component'), ('suggested_integration', 'Suggested integration'), ('suggested_owner', 'Suggested owner')], max_length=30)),
                        ('external_id', models.CharField(max_length=200)),
                        ('display_name', models.CharField(max_length=400)),
                        ('data', models.JSONField(default=dict)),
                        ('fingerprint', models.CharField(max_length=64)),
                        ('suggested_component_type', models.CharField(blank=True, choices=[('script', 'Script'), ('scheduled_job', 'Scheduled job'), ('runbook', 'Runbook'), ('function', 'Function'), ('logic_app', 'Logic App'), ('automation_flow', 'Automation flow'), ('report', 'Report'), ('ai_service', 'AI service'), ('middleware', 'Middleware'), ('internal_api', 'Internal API'), ('other', 'Other')], max_length=30)),
                        ('ai_agent', models.BooleanField(default=False)),
                        ('first_seen_at', models.DateTimeField()),
                        ('last_seen_at', models.DateTimeField()),
                        ('last_changed_at', models.DateTimeField()),
                        ('gone_at', models.DateTimeField(blank=True, null=True)),
                        ('connector', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='observations', to='discovery.connector')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_observation',
                        'ordering': ['display_name', 'id'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ReconciliationDecision',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('decision', models.CharField(choices=[('linked', 'Linked'), ('created', 'Created'), ('dismissed', 'Dismissed')], max_length=10)),
                        ('reason', models.CharField(blank=True, max_length=500)),
                        ('fingerprint', models.CharField(max_length=64)),
                        ('decided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('observation', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='decision', to='discovery.observation')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('resource', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='discovery_links', to='catalog.resource')),
                    ],
                    options={
                        'db_table': 'catalog_reconciliationdecision',
                        'abstract': False,
                    },
                ),
                migrations.AddConstraint(
                    model_name='connector',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_connector_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='connector',
                    constraint=models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='catalog_connector_unique_name'),
                ),
                migrations.AddConstraint(
                    model_name='connectorrun',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_connectorrun_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='connectorrun',
                    constraint=models.UniqueConstraint(condition=models.Q(('status', 'running')), fields=('connector',), name='catalog_connectorrun_one_running'),
                ),
                migrations.AddIndex(
                    model_name='observation',
                    index=django.contrib.postgres.indexes.GinIndex(fields=['display_name'], name='catalog_observation_name_trgm', opclasses=['gin_trgm_ops']),
                ),
                migrations.AddConstraint(
                    model_name='observation',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_observation_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='observation',
                    constraint=models.UniqueConstraint(fields=('connector', 'kind', 'external_id'), name='catalog_observation_unique_external'),
                ),
                migrations.AddIndex(
                    model_name='reconciliationdecision',
                    index=models.Index(fields=['organization', 'resource'], name='catalog_decision_resource'),
                ),
                migrations.AddConstraint(
                    model_name='reconciliationdecision',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_reconciliationdecision_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='reconciliationdecision',
                    constraint=models.CheckConstraint(condition=models.Q(models.Q(('decision', 'dismissed'), ('resource__isnull', True)), models.Q(models.Q(('decision', 'dismissed'), _negated=True), ('resource__isnull', False)), _connector='OR'), name='catalog_decision_resource_matches'),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
