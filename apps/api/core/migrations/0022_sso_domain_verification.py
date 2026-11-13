"""Sign-in domains can be pending. SsoDomain gets `verified_at` (null: pending, routes no
sign-in) and `challenge` (the value its DNS TXT record must carry). Only a verified domain is unique on the installation
(a partial unique constraint); pending claims of one domain by several organizations may coexist, one per organization.
Every existing domain already routed sign-ins, so it is marked verified now (with a challenge of its own); none is
lost or changes owner. Forward-only."""

import secrets

from django.db import migrations, models
from django.db.models.functions import Now


def mark_existing_verified(apps, schema_editor):
    SsoDomain = apps.get_model("core", "SsoDomain")
    for row in SsoDomain.objects.filter(verified_at__isnull=True).only("pk"):
        SsoDomain.objects.filter(pk=row.pk).update(verified_at=Now(), challenge=secrets.token_urlsafe(24))


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0021_okta_google_connections"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="ssodomain",
            name="core_ssodomain_unique",
        ),
        migrations.AddField(
            model_name="ssodomain",
            name="challenge",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="ssodomain",
            name="verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_verified, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="ssodomain",
            constraint=models.UniqueConstraint(fields=("organization", "domain"), name="core_ssodomain_org_domain"),
        ),
        migrations.AddConstraint(
            model_name="ssodomain",
            constraint=models.UniqueConstraint(
                condition=models.Q(("verified_at__isnull", False)), fields=("domain",), name="core_ssodomain_verified"
            ),
        ),
    ]
