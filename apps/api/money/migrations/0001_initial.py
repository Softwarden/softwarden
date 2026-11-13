"""Split: the money app takes over renewalobligation, renewalcycle, renewalcycleapplication from catalog.

State only. The tables, columns, constraints and indexes were created by catalog migrations 0001 to 0018 and keep their
`catalog_*` names (each model sets `Meta.db_table`, and the constraint names are pinned), so no SQL runs against them.
The content types of these models move from the catalog app label to `money`, so existing permission rows follow them.
On a new database there are no content types yet at this point and that step does nothing."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


MODELS = ['renewalobligation', 'renewalcycle', 'renewalcycleapplication']


def _relabel(apps, schema_editor, old, new):
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias
    taken = set(ContentType.objects.using(db).filter(app_label=new, model__in=MODELS).values_list("model", flat=True))
    ContentType.objects.using(db).filter(app_label=old, model__in=[m for m in MODELS if m not in taken]).update(
        app_label=new
    )


def forwards(apps, schema_editor):
    _relabel(apps, schema_editor, "catalog", "money")


def backwards(apps, schema_editor):
    _relabel(apps, schema_editor, "money", "catalog")


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
                    name='RenewalCycle',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('sequence', models.PositiveIntegerField()),
                        ('renewal_date', models.DateField(blank=True, null=True)),
                        ('notice_value', models.PositiveIntegerField(blank=True, null=True)),
                        ('notice_unit', models.CharField(blank=True, choices=[('days', 'Days'), ('months', 'Months'), ('years', 'Years')], max_length=10)),
                        ('explicit_deadline', models.DateField(blank=True, null=True)),
                        ('term_value', models.PositiveIntegerField(blank=True, null=True)),
                        ('term_unit', models.CharField(blank=True, choices=[('days', 'Days'), ('months', 'Months'), ('years', 'Years')], max_length=10)),
                        ('decision', models.CharField(blank=True, choices=[('renew', 'Renew'), ('cancel', 'Cancel'), ('renegotiate', 'Renegotiate')], max_length=20)),
                        ('decision_notes', models.TextField(blank=True)),
                        ('decided_on', models.DateField(blank=True, null=True)),
                        ('completed_on', models.DateField(blank=True, null=True)),
                        ('completion_evidence', models.TextField(blank=True)),
                        ('deadline_set_on', models.DateField(blank=True, null=True)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'verbose_name': 'renewal cycle',
                        'db_table': 'catalog_renewalcycle',
                        'ordering': ['obligation', '-sequence'],
                        'abstract': False,
                    },
                ),
                migrations.CreateModel(
                    name='RenewalCycleApplication',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('application', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='renewal_coverage', to='catalog.resource')),
                        ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='coverage', to='money.renewalcycle')),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                    ],
                    options={
                        'verbose_name': 'renewal cycle',
                        'db_table': 'catalog_renewalcycleapplication',
                    },
                ),
                migrations.AddField(
                    model_name='renewalcycle',
                    name='applications',
                    field=models.ManyToManyField(related_name='+', through='money.RenewalCycleApplication', to='catalog.resource'),
                ),
                migrations.CreateModel(
                    name='RenewalObligation',
                    fields=[
                        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('name', models.CharField(max_length=200)),
                        ('auto_renew', models.CharField(choices=[('yes', 'Yes'), ('no', 'No'), ('unknown', 'Unknown')], default='unknown', max_length=10)),
                        ('archived_at', models.DateTimeField(blank=True, null=True)),
                        ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('followup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                        ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                        ('responsible', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.person')),
                        ('vendor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.vendor')),
                    ],
                    options={
                        'verbose_name': 'renewal',
                        'db_table': 'catalog_renewalobligation',
                        'ordering': ['name'],
                        'abstract': False,
                    },
                ),
                migrations.AddField(
                    model_name='renewalcycle',
                    name='obligation',
                    field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cycles', to='money.renewalobligation'),
                ),
                migrations.AddIndex(
                    model_name='renewalcycleapplication',
                    index=models.Index(fields=['organization', 'application'], name='catalog_renewalcycleapp_app'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycleapplication',
                    constraint=models.UniqueConstraint(fields=('cycle', 'application'), name='catalog_renewalcycleapp_unique'),
                ),
                migrations.AddConstraint(
                    model_name='renewalobligation',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_renewalobligation_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.UniqueConstraint(fields=('organization', 'id'), name='catalog_renewalcycle_org_id'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.UniqueConstraint(fields=('obligation', 'sequence'), name='catalog_renewalcycle_unique_sequence'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.UniqueConstraint(condition=models.Q(('completed_on__isnull', True)), fields=('obligation',), name='catalog_renewalcycle_one_open'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.CheckConstraint(condition=models.Q(models.Q(('notice_value__isnull', True), ('notice_unit', '')), models.Q(('notice_value__isnull', False), models.Q(('notice_unit', ''), _negated=True)), _connector='OR'), name='catalog_renewalcycle_notice_pair'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.CheckConstraint(condition=models.Q(models.Q(('term_value__isnull', True), ('term_unit', '')), models.Q(('term_value__isnull', False), models.Q(('term_unit', ''), _negated=True)), _connector='OR'), name='catalog_renewalcycle_term_pair'),
                ),
                migrations.AddConstraint(
                    model_name='renewalcycle',
                    constraint=models.CheckConstraint(condition=models.Q(('completed_on__isnull', True), models.Q(('decision', ''), _negated=True), _connector='OR'), name='catalog_renewalcycle_completed_decided'),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(forwards, backwards),
    ]
