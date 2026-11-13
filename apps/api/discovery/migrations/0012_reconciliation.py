"""Reconciliation: SourceDifference ("source differs", one curated field of a linked record whose source value
differs, with when it started and whether someone ignored it), with a composite foreign key to the observation,
added with the table, which is empty; and the "no longer seen in source" resolution on ReconciliationDecision
(acknowledged at and by, kept or archived, a note). Forward-only; the reverse only exists so a test database can
be unmigrated. Observation gains `out_of_scope` and `out_of_scope_at`: an object the source still has but the scope
leaves out keeps its state instead of becoming "no longer seen"."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("discovery_sourcedifference", "discovery_difference_org_observation_fk", "observation_id", "catalog_observation"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_connections'),
        ('discovery', '0011_sources_b'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='observation',
            name='out_of_scope',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='observation',
            name='out_of_scope_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reconciliationdecision',
            name='gone_acknowledged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='reconciliationdecision',
            name='gone_acknowledged_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='reconciliationdecision',
            name='gone_note',
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='reconciliationdecision',
            name='gone_resolution',
            field=models.CharField(blank=True, choices=[('kept', 'Kept the record'), ('archived', 'Archived the record')], max_length=10),
        ),
        migrations.CreateModel(
            name='SourceDifference',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('field', models.CharField(max_length=60)),
                ('source_value', models.TextField()),
                ('since', models.DateTimeField()),
                ('ignored_at', models.DateTimeField(blank=True, null=True)),
                ('ignored_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('observation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='differences', to='discovery.observation')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'verbose_name': 'source difference',
                'ordering': ['observation', 'field'],
                'abstract': False,
                'indexes': [models.Index(condition=models.Q(('ignored_at__isnull', True)), fields=['organization', 'observation'], name='discovery_difference_open')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='discovery_sourcedifference_org_id'), models.UniqueConstraint(fields=('observation', 'field'), name='discovery_difference_unique_field')],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, *_rest in reversed(KEYS)
            ),
        ),
    ]
