"""End-of-life tracking (eol/models.py), in an app of its own so it doesn't collide with the catalog and notify
migrations other features add. The endoflife.date cache (EolProduct, EolRelease, EolSyncState) is
installation-wide public data, not tenant data. TrackedVersion is a tenant table: composite foreign keys keep its
record and its instance in its organization, and the three-column key makes the instance one of the record's own
(catalog_applicationinstance's UNIQUE (organization_id, application_id, id)). The tables are new and empty, so the keys
are added with them. Depends on catalog 0039 and core 0020."""

import uuid

import django.db.models.deletion
from django.db import migrations, models

KEYS = [
    # (constraint, columns, referenced table, referenced columns)
    ("eol_trackedversion_org_resource_fk", "organization_id, resource_id", "catalog_resource", "organization_id, id"),
    (
        "eol_trackedversion_org_instance_fk",
        "organization_id, resource_id, instance_id",
        "catalog_applicationinstance",
        "organization_id, application_id, id",
    ),
]


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('catalog', '0039_saved_views'),
        ('core', '0020_limits_and_tenant_backups'),
    ]

    operations = [
        migrations.CreateModel(
            name='EolProduct',
            fields=[
                ('name', models.CharField(max_length=100, primary_key=True, serialize=False)),
                ('label', models.CharField(blank=True, max_length=200)),
                ('category', models.CharField(blank=True, max_length=60)),
                ('aliases', models.JSONField(blank=True, default=list)),
                ('tags', models.JSONField(blank=True, default=list)),
                ('listed', models.BooleanField(default=True)),
                ('link', models.URLField(blank=True, max_length=500)),
                ('release_policy', models.URLField(blank=True, max_length=500)),
                ('releases_etag', models.CharField(blank=True, max_length=200)),
                ('releases_fetched_at', models.DateTimeField(blank=True, null=True)),
                ('releases_checked_at', models.DateTimeField(blank=True, null=True)),
                ('source_modified_at', models.DateTimeField(blank=True, null=True)),
                ('error', models.CharField(blank=True, max_length=300)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='EolSyncState',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ('list_etag', models.CharField(blank=True, max_length=200)),
                ('list_fetched_at', models.DateTimeField(blank=True, null=True)),
                ('list_checked_at', models.DateTimeField(blank=True, null=True)),
                ('last_run_at', models.DateTimeField(blank=True, null=True)),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=300)),
                ('source', models.CharField(blank=True, max_length=10)),
                ('imported_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='EolRelease',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('label', models.CharField(blank=True, max_length=200)),
                ('codename', models.CharField(blank=True, max_length=200)),
                ('position', models.PositiveIntegerField(default=0)),
                ('release_date', models.DateField(blank=True, null=True)),
                ('is_lts', models.BooleanField(default=False)),
                ('lts_from', models.DateField(blank=True, null=True)),
                ('has_eoas', models.BooleanField(default=False)),
                ('is_eoas', models.BooleanField(blank=True, null=True)),
                ('eoas_from', models.DateField(blank=True, null=True)),
                ('is_eol', models.BooleanField(default=False)),
                ('eol_from', models.DateField(blank=True, null=True)),
                ('has_eoes', models.BooleanField(default=False)),
                ('is_eoes', models.BooleanField(blank=True, null=True)),
                ('eoes_from', models.DateField(blank=True, null=True)),
                ('is_maintained', models.BooleanField(default=False)),
                ('latest', models.CharField(blank=True, max_length=100)),
                ('latest_date', models.DateField(blank=True, null=True)),
                ('link', models.URLField(blank=True, max_length=500)),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='releases', to='eol.eolproduct')),
            ],
            options={
                'ordering': ['product', 'position'],
                'constraints': [models.UniqueConstraint(fields=('product', 'name'), name='eol_release_unique')],
            },
        ),
        migrations.CreateModel(
            name='TrackedVersion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product', models.CharField(max_length=100)),
                ('version', models.CharField(blank=True, max_length=50)),
                ('cycle', models.CharField(blank=True, max_length=100)),
                ('cycle_explicit', models.BooleanField(default=False)),
                ('instance', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='eol_tracking', to='catalog.applicationinstance')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('resource', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='eol_tracking', to='catalog.resource')),
            ],
            options={
                'verbose_name': 'tracked version',
                'ordering': ['created_at', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['product', 'cycle'], name='eol_trackedversion_product')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='eol_trackedversion_org_id'), models.UniqueConstraint(condition=models.Q(('instance__isnull', True)), fields=('organization', 'resource'), name='eol_trackedversion_record_unique'), models.UniqueConstraint(condition=models.Q(('instance__isnull', False)), fields=('organization', 'instance'), name='eol_trackedversion_instance_unique')],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE eol_trackedversion ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
                f"REFERENCES {target} ({key}) DEFERRABLE INITIALLY DEFERRED;\n"
                for name, columns, target, key in KEYS
            ),
            reverse_sql="".join(f"ALTER TABLE eol_trackedversion DROP CONSTRAINT {name};\n" for name, *_ in reversed(KEYS)),
        ),
    ]
