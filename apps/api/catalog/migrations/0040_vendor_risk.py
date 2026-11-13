"""The vendor risk file. VendorRiskProfile, one per vendor (the questionnaire, the
assurance report, the DPA, the subprocessor list, the risk tier), and VendorRiskDocument, a vendor's dated documents.
Composite foreign keys to the vendor and to every attachment they link, deferred like the others so a purge can
delete in any order. Forward-only."""

import django.db.models.deletion
import uuid
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("catalog_vendorriskprofile", "catalog_vendorriskprofile_org_vendor_fk", "vendor_id", "catalog_vendor"),
    (
        "catalog_vendorriskprofile",
        "catalog_vendorriskprofile_org_assurance_fk",
        "assurance_attachment_id",
        "catalog_attachment",
    ),
    ("catalog_vendorriskprofile", "catalog_vendorriskprofile_org_dpa_fk", "dpa_attachment_id", "catalog_attachment"),
    (
        "catalog_vendorriskprofile",
        "catalog_vendorriskprofile_org_subprocessor_fk",
        "subprocessor_attachment_id",
        "catalog_attachment",
    ),
    ("catalog_vendorriskdocument", "catalog_vendorriskdocument_org_vendor_fk", "vendor_id", "catalog_vendor"),
    ("catalog_vendorriskdocument", "catalog_vendorriskdocument_org_attachment_fk", "attachment_id", "catalog_attachment"),
]


def composite_keys():
    return [
        migrations.RunSQL(
            sql=f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
            f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
        )
        for table, name, column, target in KEYS
    ]


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0039_saved_views'),
        ('core', '0020_limits_and_tenant_backups'),
    ]

    operations = [
        migrations.CreateModel(
            name='VendorRiskDocument',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('soc2', 'SOC 2 report'), ('iso27001', 'ISO 27001 certificate'), ('pen_test', 'Penetration test report'), ('questionnaire', 'Security questionnaire'), ('dpa', 'Data processing agreement'), ('insurance', 'Insurance certificate'), ('other', 'Other')], max_length=20)),
                ('title', models.CharField(blank=True, max_length=200)),
                ('issued_on', models.DateField(blank=True, null=True)),
                ('validity_months', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('expires_on', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('attachment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.attachment')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('vendor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='risk_documents', to='catalog.vendor')),
            ],
            options={
                'ordering': ['expires_on', 'kind', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'vendor'], name='catalog_vendorriskdoc_vendor'), models.Index(fields=['organization', 'expires_on'], name='catalog_vendorriskdoc_expiry')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='catalog_vendorriskdocument_org_id')],
            },
        ),
        migrations.CreateModel(
            name='VendorRiskProfile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('risk_tier', models.CharField(blank=True, choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('critical', 'Critical')], max_length=10)),
                ('processes_personal_data', models.BooleanField(default=False)),
                ('questionnaire_status', models.CharField(choices=[('not_requested', 'Not requested'), ('requested', 'Requested'), ('received', 'Received'), ('reviewed', 'Reviewed'), ('not_required', 'Not required')], default='not_requested', max_length=20)),
                ('questionnaire_requested_on', models.DateField(blank=True, null=True)),
                ('questionnaire_reviewed_on', models.DateField(blank=True, null=True)),
                ('assurance_type', models.CharField(blank=True, choices=[('soc2_type1', 'SOC 2 Type I'), ('soc2_type2', 'SOC 2 Type II'), ('iso27001', 'ISO 27001 certificate'), ('other', 'Other assurance report')], max_length=20)),
                ('assurance_issued_on', models.DateField(blank=True, null=True)),
                ('assurance_validity_months', models.PositiveSmallIntegerField(default=12)),
                ('assurance_expires_on', models.DateField(blank=True, null=True)),
                ('dpa_status', models.CharField(blank=True, choices=[('yes', 'Signed'), ('no', 'Not signed'), ('not_applicable', 'Not applicable')], max_length=20)),
                ('dpa_signed_on', models.DateField(blank=True, null=True)),
                ('subprocessor_url', models.URLField(blank=True, max_length=2000)),
                ('subprocessors_reviewed_on', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('assurance_attachment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.attachment')),
                ('dpa_attachment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.attachment')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('subprocessor_attachment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.attachment')),
                ('vendor', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='risk_profile', to='catalog.vendor')),
            ],
            options={
                'ordering': ['created_at', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'risk_tier'], name='catalog_vendorrisk_tier')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='catalog_vendorriskprofile_org_id')],
            },
        ),
        *composite_keys(),
    ]
