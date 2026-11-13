"""A renewal decision's terms on RenewalCycle, as optional columns rather than a
second table: the money and dates before and after a decision on a contract's term, snapshots written by the contract
renew action. Nullable or blank, so existing cycles need nothing. Forward-only."""

from django.db import migrations, models


def _money():
    return models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)


class Migration(migrations.Migration):
    dependencies = [("money", "0004_contract_search")]

    operations = [
        migrations.AddField(model_name="renewalcycle", name="old_amount", field=_money()),
        migrations.AddField(
            model_name="renewalcycle", name="old_currency", field=models.CharField(blank=True, max_length=3)
        ),
        migrations.AddField(
            model_name="renewalcycle", name="old_billing_frequency", field=models.CharField(blank=True, max_length=20)
        ),
        migrations.AddField(model_name="renewalcycle", name="old_annualized_cost", field=_money()),
        migrations.AddField(
            model_name="renewalcycle", name="old_start_date", field=models.DateField(blank=True, null=True)
        ),
        migrations.AddField(model_name="renewalcycle", name="old_end_date", field=models.DateField(blank=True, null=True)),
        migrations.AddField(model_name="renewalcycle", name="new_amount", field=_money()),
        migrations.AddField(
            model_name="renewalcycle", name="new_currency", field=models.CharField(blank=True, max_length=3)
        ),
        migrations.AddField(
            model_name="renewalcycle", name="new_billing_frequency", field=models.CharField(blank=True, max_length=20)
        ),
        migrations.AddField(model_name="renewalcycle", name="new_annualized_cost", field=_money()),
        migrations.AddField(
            model_name="renewalcycle", name="new_start_date", field=models.DateField(blank=True, null=True)
        ),
        migrations.AddField(model_name="renewalcycle", name="new_end_date", field=models.DateField(blank=True, null=True)),
    ]
