"""DiscoveredCredential, a client secret or certificate a source saw on an observed app (key ID,
type, display name, end date; never key material), with a composite foreign key to the observation, added with the
table, which is empty. Forward-only; the reverse only exists so a test database can be unmigrated."""

import django.db.models.deletion
import uuid
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("discovery_discoveredcredential", "discovery_credential_org_observation_fk", "observation_id", "catalog_observation"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_connections'),
        ('discovery', '0007_connector_drop_secret'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscoveredCredential',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('key_id', models.CharField(max_length=100)),
                ('credential_type', models.CharField(choices=[('secret', 'Client secret'), ('certificate', 'Certificate')], max_length=20)),
                ('display_name', models.CharField(blank=True, max_length=300)),
                ('origin', models.CharField(choices=[('app_registration', 'App registration'), ('enterprise_app', 'Enterprise application')], max_length=20)),
                ('object_id', models.CharField(blank=True, max_length=64)),
                ('expires_on', models.DateField(blank=True, null=True)),
                ('first_seen_at', models.DateTimeField()),
                ('last_seen_at', models.DateTimeField()),
                ('removed_at', models.DateTimeField(blank=True, null=True)),
                ('observation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='credentials', to='discovery.observation')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'verbose_name': 'discovered credential',
                'ordering': ['expires_on', 'id'],
                'abstract': False,
                'indexes': [models.Index(condition=models.Q(('removed_at__isnull', True)), fields=['organization', 'expires_on'], name='discovery_credential_live')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='discovery_discoveredcredential_org_id'), models.UniqueConstraint(fields=('observation', 'key_id'), name='discovery_credential_unique_key')],
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
