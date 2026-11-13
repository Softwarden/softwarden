"""Split: the imports app takes over importsource, externalidentifier, importrun, importrow from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `imports`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['importsource', 'externalidentifier', 'importrun', 'importrow']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "imports")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "imports", "catalog")


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
                    name='ImportSource',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('name', models.CharField(max_length=200)),
                        ('kind', models.CharField(choices=[('application', 'Application'), ('component', 'Component')], max_length=20)),
                        ('column_mapping', models.JSONField(blank=True, default=dict)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'db_table': 'catalog_importsource',
                        'ordering': ['name'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ImportRun',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('kind', models.CharField(choices=[('application', 'Application'), ('component', 'Component')], max_length=20)),
                        ('filename', models.CharField(max_length=255)),
                        ('file_bytes', models.BinaryField(blank=True, null=True)),
                        ('headers', models.JSONField(default=list)),
                        ('mapping', models.JSONField(blank=True, default=dict)),
                        ('status', models.CharField(choices=[('uploaded', 'Uploaded'), ('previewed', 'Previewed'), ('committed', 'Committed'), ('refused', 'Refused'), ('cancelled', 'Cancelled')], default='uploaded', max_length=10)),
                        ('decisions', models.JSONField(blank=True, default=dict)),
                        ('counts', models.JSONField(blank=True, default=dict)),
                        ('previewed_at', models.DateTimeField(blank=True, null=True)),
                        ('committed_at', models.DateTimeField(blank=True, null=True)),
                        ('refused_reason', models.CharField(blank=True, max_length=500)),
                        ('purge_after', models.DateField()),
                        ('purged_at', models.DateTimeField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('uploaded_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('source', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runs', to='imports.importsource')),
                    ],
                    options={
                        'db_table': 'catalog_importrun',
                        'ordering': ['-created_at'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ExternalIdentifier',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('external_key', models.CharField(max_length=200)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('resource', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='external_identifiers', to='catalog.resource')),
                        ('source', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='identifiers', to='imports.importsource')),
                    ],
                    options={
                        'db_table': 'catalog_externalidentifier',
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='ImportRow',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('row_number', models.PositiveIntegerField()),
                        ('raw', models.JSONField(default=dict)),
                        ('outcome', models.CharField(choices=[('create', 'Create'), ('update', 'Update'), ('unchanged', 'Unchanged'), ('error', 'Error'), ('excluded', 'Excluded')], max_length=10)),
                        ('matched_updated_at', models.DateTimeField(blank=True, null=True)),
                        ('errors', models.JSONField(default=list)),
                        ('changes', models.JSONField(default=dict)),
                        ('needs_confirmation', models.BooleanField(default=False)),
                        ('excluded', models.BooleanField(default=False)),
                        ('matched_resource', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.resource')),
                        ('possible_match', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.resource')),
                        ('result_resource', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.resource')),
                        ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rows', to='imports.importrun')),
                    ],
                    options={
                        'db_table': 'catalog_importrow',
                        'ordering': ['run', 'row_number'],
                        'constraints': [models.UniqueConstraint(fields=('run', 'row_number'), name='catalog_importrow_unique_number')],
                    },
                ),
                migrations.AddConstraint(
                    model_name='importsource',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_importsource_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='importsource',
                    constraint=models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('name'), name='catalog_importsource_unique_name'),
                ),
                migrations.AddConstraint(
                    model_name='importrun',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_importrun_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='externalidentifier',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_externalidentifier_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='externalidentifier',
                    constraint=models.UniqueConstraint(fields=('source', 'external_key'), name='catalog_externalid_unique_key'),
                ),
                migrations.AddConstraint(
                    model_name='externalidentifier',
                    constraint=models.UniqueConstraint(fields=('source', 'resource'), name='catalog_externalid_unique_resource'),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
