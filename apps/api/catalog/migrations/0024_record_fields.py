"""The application and component field additions and the shared department, cost center and data
classification. Component types gain api, service, workflow and infra_automation next to the earlier values (no
value is remapped). The department and the category must be in the record's organization: composite foreign keys,
added with the columns."""

import django.db.models.deletion
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("catalog_resource", "catalog_resource_org_department_fk", "department_id", "catalog_department"),
    ("catalog_application", "catalog_application_org_category_fk", "category_id", "catalog_softwarecategory"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0023_software_categories"),
    ]

    operations = [
        # -- shared (Resource)
        migrations.AddField(
            model_name="resource",
            name="department",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="catalog.department",
            ),
        ),
        migrations.AddField(
            model_name="resource",
            name="cost_center",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="resource",
            name="data_classification",
            field=models.CharField(
                blank=True,
                choices=[
                    ("public", "Public"),
                    ("internal", "Internal"),
                    ("confidential", "Confidential"),
                    ("restricted", "Restricted"),
                ],
                max_length=20,
            ),
        ),
        # -- applications
        migrations.AddField(
            model_name="application",
            name="category",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="catalog.softwarecategory",
            ),
        ),
        migrations.AddField(
            model_name="application",
            name="deployment_model",
            field=models.CharField(
                blank=True,
                choices=[("saas", "SaaS"), ("on_prem", "On-premises"), ("hybrid", "Hybrid"), ("desktop", "Desktop")],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="application",
            name="version",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="application",
            name="primary_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
        migrations.AddField(
            model_name="application",
            name="admin_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
        migrations.AddField(
            model_name="application",
            name="authentication_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("saml", "SAML"),
                    ("oidc", "OpenID Connect"),
                    ("oauth", "OAuth"),
                    ("ldap", "LDAP or Kerberos"),
                    ("local", "Local accounts"),
                    ("none", "No sign-in"),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="application",
            name="sso_enabled",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="application",
            name="scim_provisioning",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="application",
            name="entra_app_id",
            field=models.CharField(blank=True, max_length=36),
        ),
        migrations.AddField(
            model_name="application",
            name="service_principal_id",
            field=models.CharField(blank=True, max_length=36),
        ),
        # -- components
        migrations.AlterField(
            model_name="component",
            name="component_type",
            field=models.CharField(
                choices=[
                    ("script", "Script"),
                    ("scheduled_job", "Scheduled job"),
                    ("runbook", "Runbook"),
                    ("function", "Function"),
                    ("logic_app", "Logic App"),
                    ("automation_flow", "Automation flow"),
                    ("report", "Report"),
                    ("ai_service", "AI service"),
                    ("middleware", "Middleware"),
                    ("internal_api", "Internal API"),
                    ("api", "API"),
                    ("service", "Service"),
                    ("workflow", "Workflow"),
                    ("infra_automation", "Infrastructure automation"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="component",
            name="hosting",
            field=models.CharField(
                blank=True,
                choices=[
                    ("azure", "Azure"),
                    ("on_prem", "On-premises"),
                    ("power_platform", "Power Platform"),
                    ("other", "Other"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="component",
            name="runbook_url",
            field=models.URLField(blank=True, max_length=2000),
        ),
        migrations.AddField(
            model_name="component",
            name="schedule_or_trigger",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="component",
            name="environment",
            field=models.CharField(
                blank=True,
                choices=[("dev", "Development"), ("test", "Test"), ("prod", "Production")],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="component",
            name="azure_resource_id",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="component",
            name="pp_environment_id",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="component",
            name="external_id",
            field=models.CharField(blank=True, max_length=200),
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
