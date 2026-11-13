"""Split: the api app takes over apitoken from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `api`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['apitoken']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "api")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "api", "catalog")


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
                    name='ApiToken',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('name', models.CharField(max_length=100)),
                        ('token_hash', models.CharField(max_length=64, unique=True)),
                        ('prefix', models.CharField(max_length=12)),
                        ('read_only', models.BooleanField(default=True)),
                        ('expires_on', models.DateField()),
                        ('last_used_at', models.DateTimeField(blank=True, null=True)),
                        ('revoked_at', models.DateTimeField(blank=True, null=True)),
                        ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='api_tokens', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'catalog_apitoken',
                        'ordering': ['-created_at'],
                        'abstract': False,
                        'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='catalog_apitoken_org_id')],
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
