"""Attachments: Attachment, a file or a link on a record (Resource) or a vendor, and
RetiredFile, a stored file waiting for the nightly sweep. Composite foreign keys keep an attachment's record or
vendor in its organization; they are added with the tables, which are empty. uploaded_by is a User, not a tenant row."""

import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table, referenced key)
    ("catalog_attachment", "catalog_attachment_org_resource_fk", "resource_id", "catalog_resource", "id"),
    ("catalog_attachment", "catalog_attachment_org_vendor_fk", "vendor_id", "catalog_vendor", "id"),
]

CATEGORIES = [
    ("contract", "Contract"),
    ("invoice", "Invoice"),
    ("sop_runbook", "SOP or runbook"),
    ("architecture", "Architecture"),
    ("validation", "Validation"),
    ("vendor_docs", "Vendor documentation"),
    ("security_review", "Security review"),
    ("other", "Other"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0029_customfield_unique_label"),
        ("core", "0008_auditevent_append_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Attachment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("kind", models.CharField(choices=[("file", "File"), ("link", "Link")], max_length=10)),
                ("title", models.CharField(max_length=200)),
                ("category", models.CharField(choices=CATEGORIES, default="other", max_length=30)),
                ("url", models.URLField(blank=True, max_length=2000)),
                ("storage_name", models.CharField(blank=True, max_length=200)),
                ("filename", models.CharField(blank=True, max_length=200)),
                ("content_type", models.CharField(blank=True, max_length=100)),
                ("size", models.PositiveBigIntegerField(blank=True, null=True)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attachments",
                        to="catalog.resource",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "vendor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="attachments",
                        to="catalog.vendor",
                    ),
                ),
            ],
            options={
                "ordering": ["category", "title"],
                "abstract": False,
                "indexes": [
                    models.Index(fields=["organization", "resource"], name="catalog_attachment_resource"),
                    models.Index(fields=["organization", "vendor"], name="catalog_attachment_vendor"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="catalog_attachment_org_id"),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("resource__isnull", False), ("vendor__isnull", True)),
                            models.Q(("resource__isnull", True), ("vendor__isnull", False)),
                            _connector="OR",
                        ),
                        name="catalog_attachment_one_parent",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("kind", "file"),
                                models.Q(("storage_name", ""), _negated=True),
                                ("size__isnull", False),
                                ("sha256__regex", "^[0-9a-f]{64}$"),
                                ("url", ""),
                            ),
                            models.Q(
                                ("kind", "link"),
                                models.Q(("url", ""), _negated=True),
                                ("storage_name", ""),
                                ("size__isnull", True),
                                ("sha256", ""),
                            ),
                            _connector="OR",
                        ),
                        name="catalog_attachment_kind_fields",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("storage_name", ""), _negated=True),
                        fields=("storage_name",),
                        name="catalog_attachment_unique_storage",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RetiredFile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200, unique=True)),
                ("sha256", models.CharField(blank=True, max_length=64)),
                ("size", models.PositiveBigIntegerField(blank=True, null=True)),
                ("retired_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "ordering": ["retired_at"],
                "abstract": False,
                "constraints": [
                    models.UniqueConstraint(fields=("organization", "id"), name="catalog_retiredfile_org_id")
                ],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, {key}) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target, key in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, *_rest in reversed(KEYS)
            ),
        ),
    ]
