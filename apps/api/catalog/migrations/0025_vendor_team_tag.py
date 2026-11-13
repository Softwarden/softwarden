"""Vendor support details and account manager, team members and a tag's colour and
description. The account manager and both ends of a membership must be in the row's organization: composite foreign
keys, added with the columns."""

import uuid

import django.db.models.deletion
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("catalog_vendor", "catalog_vendor_org_account_manager_fk", "account_manager_id", "catalog_person"),
    ("catalog_teammember", "catalog_teammember_org_team_fk", "team_id", "catalog_team"),
    ("catalog_teammember", "catalog_teammember_org_person_fk", "person_id", "catalog_person"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0024_record_fields"),
        ("core", "0008_auditevent_append_only"),
    ]

    operations = [
        # -- vendors
        migrations.AddField(
            model_name="vendor",
            name="support_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
        migrations.AddField(
            model_name="vendor",
            name="support_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="vendor",
            name="support_phone",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="vendor",
            name="portal_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
        migrations.AddField(
            model_name="vendor",
            name="account_manager",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="catalog.person",
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="notes",
            field=models.TextField(blank=True),
        ),
        # -- team members
        migrations.CreateModel(
            name="TeamMember",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="team_links", to="catalog.person"
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="member_links", to="catalog.team"
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="team",
            name="members",
            field=models.ManyToManyField(
                blank=True, related_name="teams", through="catalog.TeamMember", to="catalog.person"
            ),
        ),
        migrations.AddIndex(
            model_name="teammember",
            index=models.Index(fields=["organization", "person"], name="catalog_teammember_person"),
        ),
        migrations.AddConstraint(
            model_name="teammember",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_teammember_org_id"),
        ),
        migrations.AddConstraint(
            model_name="teammember",
            constraint=models.UniqueConstraint(fields=("team", "person"), name="catalog_teammember_unique"),
        ),
        # -- tags
        migrations.AddField(
            model_name="tag",
            name="color",
            field=models.CharField(
                blank=True,
                choices=[
                    ("gray", "Gray"),
                    ("blue", "Blue"),
                    ("teal", "Teal"),
                    ("green", "Green"),
                    ("amber", "Amber"),
                    ("orange", "Orange"),
                    ("red", "Red"),
                    ("violet", "Violet"),
                    ("magenta", "Magenta"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="description",
            field=models.CharField(blank=True, max_length=500),
        ),
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
