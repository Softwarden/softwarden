"""Contracts: Contract, ContractItem and LicensePool, with their
composite foreign keys added with the tables, which are empty. Money is Decimal(14, 2) with a three-letter
currency code; the annualised cost and the cancellation deadline are derived by money/contracts.py and stored so the
lists, filters and reports can use them. Forward-only."""

import uuid

import django.contrib.postgres.indexes
import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("money_contract", "money_contract_org_vendor_fk", "vendor_id", "catalog_vendor"),
    ("money_contract", "money_contract_org_owner_fk", "renewal_owner_id", "catalog_person"),
    ("money_contract", "money_contract_org_predecessor_fk", "predecessor_id", "money_contract"),
    ("money_contract", "money_contract_org_obligation_fk", "renewal_obligation_id", "catalog_renewalobligation"),
    ("money_contractitem", "money_contractitem_org_contract_fk", "contract_id", "money_contract"),
    ("money_contractitem", "money_contractitem_org_resource_fk", "resource_id", "catalog_resource"),
    ("money_licensepool", "money_licensepool_org_application_fk", "application_id", "catalog_resource"),
    ("money_licensepool", "money_licensepool_org_contract_fk", "contract_id", "money_contract"),
]

BILLING = [
    ("monthly", "Monthly"),
    ("quarterly", "Quarterly"),
    ("semiannual", "Every six months"),
    ("annual", "Annual"),
    ("multi_year", "Multi-year"),
    ("one_time", "One-time"),
    ("usage_based", "Usage-based"),
]
STATUSES = [
    ("signed", "Signed (not started)"),
    ("active", "Active"),
    ("pending_renewal", "Pending renewal"),
    ("renewed", "Renewed"),
    ("cancelled", "Cancelled"),
    ("expired", "Expired"),
]
AUTO_RENEW = [("yes", "Yes"), ("no", "No"), ("unknown", "Unknown")]
UNITS = [("days", "Days"), ("months", "Months"), ("years", "Years")]


def _org():
    return models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization")


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0035_integration_fields"),
        ("core", "0008_auditevent_append_only"),
        ("money", "0002_coverage_uuid"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Contract",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("contract_number", models.CharField(blank=True, max_length=100)),
                ("po_number", models.CharField(blank=True, max_length=100)),
                ("amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("billing_frequency", models.CharField(choices=BILLING, default="annual", max_length=20)),
                (
                    "annualized_cost",
                    models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=14, null=True),
                ),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                ("auto_renew", models.CharField(choices=AUTO_RENEW, default="unknown", max_length=10)),
                ("notice_value", models.PositiveIntegerField(blank=True, null=True)),
                ("notice_unit", models.CharField(blank=True, choices=UNITS, max_length=10)),
                ("cancellation_deadline", models.DateField(blank=True, editable=False, null=True)),
                ("status", models.CharField(choices=STATUSES, default="active", max_length=20)),
                ("cost_center", models.CharField(blank=True, max_length=50)),
                ("notes", models.TextField(blank=True)),
                ("custom_field_data", models.JSONField(blank=True, default=dict)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("organization", _org()),
                (
                    "predecessor",
                    models.OneToOneField(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="successor",
                        to="money.contract",
                    ),
                ),
                (
                    "renewal_obligation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contracts",
                        to="money.renewalobligation",
                    ),
                ),
                (
                    "renewal_owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="catalog.person",
                    ),
                ),
                (
                    "vendor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contracts",
                        to="catalog.vendor",
                    ),
                ),
            ],
            options={
                "verbose_name": "contract",
                "ordering": ["name"],
                "abstract": False,
                "indexes": [
                    models.Index(fields=["organization", "end_date"], name="money_contract_end"),
                    models.Index(fields=["organization", "cancellation_deadline"], name="money_contract_deadline"),
                    models.Index(fields=["organization", "renewal_obligation"], name="money_contract_obligation"),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["custom_field_data"], name="money_contract_cf_gin", opclasses=["jsonb_path_ops"]
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="money_contract_org_id"),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("notice_value__isnull", True), ("notice_unit", "")),
                            models.Q(("notice_value__isnull", False), models.Q(("notice_unit", ""), _negated=True)),
                            _connector="OR",
                        ),
                        name="money_contract_notice_pair",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("start_date__isnull", True),
                            ("end_date__isnull", True),
                            ("end_date__gte", models.F("start_date")),
                            _connector="OR",
                        ),
                        name="money_contract_dates_ordered",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("amount__isnull", True), ("amount__gte", 0), _connector="OR"),
                        name="money_contract_amount",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("currency__regex", "^[A-Z]{3}$")), name="money_contract_currency"
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ContractItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("allocation_percent", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                (
                    "contract",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="items", to="money.contract"
                    ),
                ),
                ("organization", _org()),
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="contract_items",
                        to="catalog.resource",
                    ),
                ),
            ],
            options={
                "verbose_name": "contract line",
                "ordering": ["contract", "created_at", "id"],
                "abstract": False,
                "indexes": [models.Index(fields=["organization", "resource"], name="money_contractitem_resource")],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="money_contractitem_org_id"),
                    models.UniqueConstraint(fields=("contract", "resource"), name="money_contractitem_unique_resource"),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("allocation_percent__isnull", True),
                            models.Q(("allocation_percent__gte", 0), ("allocation_percent__lte", 100)),
                            _connector="OR",
                        ),
                        name="money_contractitem_percent_range",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="LicensePool",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sku_name", models.CharField(max_length=200)),
                ("sku_id", models.CharField(blank=True, max_length=64)),
                ("seats_purchased", models.PositiveIntegerField(default=0)),
                ("seats_assigned", models.PositiveIntegerField(default=0)),
                ("seat_sync", models.BooleanField(default=False)),
                ("last_synced", models.DateTimeField(blank=True, editable=False, null=True)),
                ("notes", models.TextField(blank=True)),
                ("custom_field_data", models.JSONField(blank=True, default=dict)),
                (
                    "application",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="license_pools",
                        to="catalog.resource",
                    ),
                ),
                (
                    "contract",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="license_pools",
                        to="money.contract",
                    ),
                ),
                ("organization", _org()),
            ],
            options={
                "verbose_name": "licence pool",
                "ordering": ["sku_name"],
                "abstract": False,
                "indexes": [
                    models.Index(fields=["organization", "application"], name="money_licensepool_application"),
                    models.Index(fields=["organization", "contract"], name="money_licensepool_contract"),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["custom_field_data"], name="money_licensepool_cf_gin", opclasses=["jsonb_path_ops"]
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="money_licensepool_org_id"),
                    models.UniqueConstraint(
                        models.F("organization"),
                        django.db.models.functions.text.Lower("sku_id"),
                        condition=models.Q(("seat_sync", True)),
                        name="money_licensepool_one_synced_per_sku",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("seat_sync", False), models.Q(("sku_id", ""), _negated=True), _connector="OR"),
                        name="money_licensepool_sync_sku",
                    ),
                ],
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
