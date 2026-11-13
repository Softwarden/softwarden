"""An identity is keyed by (issuer, subject) instead of Entra's (tenant ID, object ID), so Okta, Google and any
OpenID Connect provider can pin one too (core/oidc_discovery.identity_key). Existing rows are Entra sign-ins: their
issuer becomes `https://login.microsoftonline.com/{tenant in lower case}/v2.0` and their subject the object ID, which
is exactly what an Entra sign-in pins from now on, so nobody has to link again. The table keeps its name (catalog_useridentity).
Forward-only."""

from django.db import migrations, models

TO_ISSUER = """
UPDATE catalog_useridentity
SET issuer = 'https://login.microsoftonline.com/' || lower(tenant_id) || '/v2.0', subject = object_id
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_halo_scope_read_assets"),
    ]

    operations = [
        migrations.AddField(
            model_name="useridentity",
            name="issuer",
            field=models.CharField(default="", max_length=500),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="useridentity",
            name="subject",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
        migrations.RunSQL(TO_ISSUER, reverse_sql=migrations.RunSQL.noop),
        migrations.RemoveConstraint(model_name="useridentity", name="catalog_useridentity_unique"),
        migrations.RemoveField(model_name="useridentity", name="tenant_id"),
        migrations.RemoveField(model_name="useridentity", name="object_id"),
        migrations.AddConstraint(
            model_name="useridentity",
            constraint=models.UniqueConstraint(fields=("issuer", "subject"), name="core_useridentity_issuer_subject"),
        ),
    ]
