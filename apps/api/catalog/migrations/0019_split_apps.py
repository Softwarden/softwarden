"""Split: models move from catalog to core, money, notify, discovery, imports and api.

State only, no SQL. Each new app's 0001 adds its models to the migration state on the tables catalog created; this
migration points the remaining catalog models' foreign keys at their new homes (Organization is now core's, ImportRun
imports') and removes the moved models from catalog's state. Table, constraint and index names are unchanged."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0018_import_tags_tokens"),
        ("core", "0001_initial"),
        ("money", "0001_initial"),
        ("notify", "0001_initial"),
        ("discovery", "0001_initial"),
        ("imports", "0001_initial"),
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="applicationinstance",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="assessmentsnapshot",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="changeassessment",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="department",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="integration",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="linkconfirmation",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="person",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="recordreview",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="relationship",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="resource",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="resourcetag",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="savedfilter",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="snapshotlink",
                    name="organization",
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="snapshotnode",
                    name="organization",
                    field=models.ForeignKey(
                        db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="tag",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="team",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="vendor",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                migrations.AlterField(
                    model_name="resource",
                    name="created_by_import",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_resources",
                        to="imports.importrun",
                    ),
                ),
                migrations.DeleteModel(name="apitoken"),
                migrations.DeleteModel(name="reconciliationdecision"),
                migrations.DeleteModel(name="observation"),
                migrations.DeleteModel(name="connectorrun"),
                migrations.DeleteModel(name="connector"),
                migrations.DeleteModel(name="importrow"),
                migrations.DeleteModel(name="externalidentifier"),
                migrations.DeleteModel(name="importrun"),
                migrations.DeleteModel(name="importsource"),
                migrations.DeleteModel(name="reminderdispatch"),
                migrations.DeleteModel(name="digest"),
                migrations.DeleteModel(name="digestrun"),
                migrations.DeleteModel(name="notificationsnooze"),
                migrations.DeleteModel(name="notificationpreference"),
                migrations.DeleteModel(name="renewalcycle"),
                migrations.DeleteModel(name="renewalcycleapplication"),
                migrations.DeleteModel(name="renewalobligation"),
                migrations.DeleteModel(name="useridentity"),
                migrations.DeleteModel(name="auditevent"),
                migrations.DeleteModel(name="workerheartbeat"),
                migrations.DeleteModel(name="membership"),
                migrations.DeleteModel(name="organization"),
            ],
            database_operations=[],
        ),
    ]
