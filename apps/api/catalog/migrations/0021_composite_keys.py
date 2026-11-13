"""Tenancy fixes: composite foreign keys for the references that had only single-column ones.

A Person's department, a Resource's owners, support team and creating import run, an Application's vendor, and a
RecordReview's record and confirming Person must all be in the row's own organization. Each target already has its
(organization, id) key. Adding a constraint checks the existing rows, so a database holding a cross-organization
reference stops here (one transaction, nothing changed) rather than carrying it forward."""

from django.db import migrations

KEYS = [
    # (table, constraint, column, referenced table)
    ("catalog_person", "catalog_person_org_department_fk", "department_id", "catalog_department"),
    ("catalog_resource", "catalog_resource_org_business_owner_fk", "business_owner_id", "catalog_person"),
    ("catalog_resource", "catalog_resource_org_technical_owner_fk", "technical_owner_id", "catalog_person"),
    ("catalog_resource", "catalog_resource_org_support_team_fk", "support_team_id", "catalog_team"),
    ("catalog_resource", "catalog_resource_org_created_by_import_fk", "created_by_import_id", "catalog_importrun"),
    ("catalog_application", "catalog_application_org_vendor_fk", "vendor_id", "catalog_vendor"),
    ("catalog_recordreview", "catalog_recordreview_org_resource_fk", "resource_id", "catalog_resource"),
    ("catalog_recordreview", "catalog_recordreview_org_confirmed_by_fk", "confirmed_by_id", "catalog_person"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0020_extension_organization"),
        ("imports", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, _column, _target in reversed(KEYS)
            ),
        ),
    ]
