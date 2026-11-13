"""Imports: contracts and licence pools are import kinds (imports/money_import.py).

An external identifier names exactly one of a record, a contract or a licence pool; an import row has matched,
possible-match and result columns for contracts and a matched and result column for pools, plus what the preview
computed. Composite foreign keys keep every new reference in the row's organization; a NULL is not checked (MATCH
SIMPLE), so SET_NULL on a deleted contract or pool still works. The tables keep their `catalog_*` names. Forward-only."""

import django.db.models.deletion
from django.db import migrations, models


KEYS = [
    # (table, constraint, column, referenced table)
    ("catalog_externalidentifier", "catalog_externalid_org_contract_fk", "contract_id", "money_contract"),
    ("catalog_externalidentifier", "catalog_externalid_org_pool_fk", "license_pool_id", "money_licensepool"),
    ("catalog_importrow", "catalog_importrow_org_matched_contract_fk", "matched_contract_id", "money_contract"),
    ("catalog_importrow", "catalog_importrow_org_possible_contract_fk", "possible_contract_id", "money_contract"),
    ("catalog_importrow", "catalog_importrow_org_result_contract_fk", "result_contract_id", "money_contract"),
    ("catalog_importrow", "catalog_importrow_org_matched_pool_fk", "matched_license_pool_id", "money_licensepool"),
    ("catalog_importrow", "catalog_importrow_org_result_pool_fk", "result_license_pool_id", "money_licensepool"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0037_customfield_contract_kinds'),
        ('core', '0008_auditevent_append_only'),
        ('imports', '0002_importrow_tenancy'),
        ('money', '0007_calendar_feed'),
    ]

    operations = [
        migrations.AddField(
            model_name='externalidentifier',
            name='contract',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='external_identifiers', to='money.contract'),
        ),
        migrations.AddField(
            model_name='externalidentifier',
            name='license_pool',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='external_identifiers', to='money.licensepool'),
        ),
        migrations.AddField(
            model_name='importrow',
            name='computed',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='importrow',
            name='matched_contract',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.contract'),
        ),
        migrations.AddField(
            model_name='importrow',
            name='matched_license_pool',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.licensepool'),
        ),
        migrations.AddField(
            model_name='importrow',
            name='possible_contract',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.contract'),
        ),
        migrations.AddField(
            model_name='importrow',
            name='result_contract',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.contract'),
        ),
        migrations.AddField(
            model_name='importrow',
            name='result_license_pool',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='money.licensepool'),
        ),
        migrations.AlterField(
            model_name='externalidentifier',
            name='resource',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='external_identifiers', to='catalog.resource'),
        ),
        migrations.AlterField(
            model_name='importrun',
            name='kind',
            field=models.CharField(choices=[('application', 'Application'), ('component', 'Component'), ('contract', 'Contract'), ('license_pool', 'Licence pool')], max_length=20),
        ),
        migrations.AlterField(
            model_name='importsource',
            name='kind',
            field=models.CharField(choices=[('application', 'Application'), ('component', 'Component'), ('contract', 'Contract'), ('license_pool', 'Licence pool')], max_length=20),
        ),
        migrations.AddConstraint(
            model_name='externalidentifier',
            constraint=models.UniqueConstraint(fields=('source', 'contract'), name='catalog_externalid_unique_contract'),
        ),
        migrations.AddConstraint(
            model_name='externalidentifier',
            constraint=models.UniqueConstraint(fields=('source', 'license_pool'), name='catalog_externalid_unique_pool'),
        ),
        migrations.AddConstraint(
            model_name='externalidentifier',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('resource__isnull', False), ('contract__isnull', True), ('license_pool__isnull', True)), models.Q(('resource__isnull', True), ('contract__isnull', False), ('license_pool__isnull', True)), models.Q(('resource__isnull', True), ('contract__isnull', True), ('license_pool__isnull', False)), _connector='OR'), name='catalog_externalid_one_target'),
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target in KEYS
            ),
            reverse_sql="".join(f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, _c, _t in KEYS),
        ),
    ]
