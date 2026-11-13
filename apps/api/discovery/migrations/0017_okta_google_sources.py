"""Okta and Google Workspace discovery (discovery/okta.py, discovery/google_workspace.py): the source choices
`okta` and `google_workspace`, and a connector's connection columns for them (core 0021), with composite foreign keys
to the connection's organization like discovery 0005; the one-connection check covers all four columns. The new
columns are empty on every existing row. Forward-only; the reverse only exists so a test database can be
unmigrated."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (constraint, column, referenced table)
    ("discovery_connector_org_okta_fk", "okta_connection_id", "core_oktaconnection"),
    ("discovery_connector_org_google_fk", "google_workspace_connection_id", "core_googleworkspaceconnection"),
]

class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_okta_google_connections'),
        ('discovery', '0016_sso_coverage'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='connector',
            name='discovery_connector_one_connection',
        ),
        migrations.AddField(
            model_name='connector',
            name='google_workspace_connection',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='connectors', to='core.googleworkspaceconnection'),
        ),
        migrations.AddField(
            model_name='connector',
            name='okta_connection',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='connectors', to='core.oktaconnection'),
        ),
        migrations.AlterField(
            model_name='connector',
            name='source',
            field=models.CharField(choices=[('entra', 'Microsoft Entra ID'), ('entra_licenses', 'Microsoft Entra licences'), ('azure', 'Azure'), ('powerplatform', 'Power Platform'), ('halo', 'HaloITSM'), ('entra_shadow_it', 'Microsoft Entra shadow IT'), ('entra_coverage', 'Microsoft Entra SSO and MFA coverage'), ('okta', 'Okta'), ('google_workspace', 'Google Workspace')], max_length=20),
        ),
        migrations.AddConstraint(
            model_name='connector',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('microsoft_connection__isnull', True), ('halo_connection__isnull', True), _connector='OR'), models.Q(('microsoft_connection__isnull', True), ('okta_connection__isnull', True), _connector='OR'), models.Q(('microsoft_connection__isnull', True), ('google_workspace_connection__isnull', True), _connector='OR'), models.Q(('halo_connection__isnull', True), ('okta_connection__isnull', True), _connector='OR'), models.Q(('halo_connection__isnull', True), ('google_workspace_connection__isnull', True), _connector='OR'), models.Q(('okta_connection__isnull', True), ('google_workspace_connection__isnull', True), _connector='OR')), name='discovery_connector_one_connection'),
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
