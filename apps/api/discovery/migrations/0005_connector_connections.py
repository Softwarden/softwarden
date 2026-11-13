"""Connections: a connector signs in through a core.MicrosoftConnection or core.HaloConnection (never both),
with composite foreign keys to the connection's organization. discovery 0006 moves the Entra connectors'
credentials over and 0007 drops the connector's secret columns. Forward-only; the reverse only exists so a test
database can be unmigrated."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (constraint, column, referenced table)
    ("discovery_connector_org_microsoft_fk", "microsoft_connection_id", "core_microsoftconnection"),
    ("discovery_connector_org_halo_fk", "halo_connection_id", "core_haloconnection"),
]
class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_connections'),
        ('discovery', '0004_observation_license_kind'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='connector',
            name='halo_connection',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='connectors', to='core.haloconnection'),
        ),
        migrations.AddField(
            model_name='connector',
            name='microsoft_connection',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='connectors', to='core.microsoftconnection'),
        ),
        migrations.AddConstraint(
            model_name='connector',
            constraint=models.CheckConstraint(condition=models.Q(('microsoft_connection__isnull', True), ('halo_connection__isnull', True), _connector='OR'), name='discovery_connector_one_connection'),
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE catalog_connector ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE catalog_connector DROP CONSTRAINT {name};\n" for name, *_rest in reversed(KEYS)
            ),
        ),
    ]
