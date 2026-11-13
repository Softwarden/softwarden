"""Attachments on contracts. The contract is a third explicit
parent column with its composite foreign key; the one-parent check is replaced so exactly one of the record, the
vendor and the contract is set. Existing rows have a record or a vendor and no contract, so they pass the new check.
Forward-only."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0035_integration_fields"),
        ("money", "0003_contracts"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="attachment", name="catalog_attachment_one_parent"),
        migrations.AddField(
            model_name="attachment",
            name="contract",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="attachments",
                to="money.contract",
            ),
        ),
        migrations.AddIndex(
            model_name="attachment",
            index=models.Index(fields=["organization", "contract"], name="catalog_attachment_contract"),
        ),
        migrations.AddConstraint(
            model_name="attachment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("resource__isnull", False), ("vendor__isnull", True), ("contract__isnull", True)),
                    models.Q(("resource__isnull", True), ("vendor__isnull", False), ("contract__isnull", True)),
                    models.Q(("resource__isnull", True), ("vendor__isnull", True), ("contract__isnull", False)),
                    _connector="OR",
                ),
                name="catalog_attachment_one_parent",
            ),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE catalog_attachment ADD CONSTRAINT catalog_attachment_org_contract_fk FOREIGN KEY "
            "(organization_id, contract_id) REFERENCES money_contract (organization_id, id) DEFERRABLE INITIALLY "
            "DEFERRED;",
            reverse_sql="ALTER TABLE catalog_attachment DROP CONSTRAINT catalog_attachment_org_contract_fk;",
        ),
    ]
