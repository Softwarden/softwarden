"""AI agents: the ai_agent record kind with its AIAgent extension row, AI providers and models, and the
agent-to-model link. Composite foreign keys keep the agent's resource, a model's provider and both ends of an
agent-model link in the row's organization; they are added with the tables, which are empty."""

import uuid

import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table, referenced key)
    ("catalog_aiagent", "catalog_aiagent_org_resource_fk", "resource_id", "catalog_resource", "id"),
    ("catalog_aimodel", "catalog_aimodel_org_provider_fk", "provider_id", "catalog_aiprovider", "id"),
    ("catalog_agentmodel", "catalog_agentmodel_org_agent_fk", "agent_id", "catalog_aiagent", "resource_id"),
    ("catalog_agentmodel", "catalog_agentmodel_org_model_fk", "model_id", "catalog_aimodel", "id"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0026_compliance_profile"),
        ("core", "0008_auditevent_append_only"),
    ]

    operations = [
        migrations.AlterField(
            model_name="resource",
            name="kind",
            field=models.CharField(
                choices=[
                    ("application", "Application"),
                    ("component", "Component"),
                    ("integration", "Integration"),
                    ("ai_agent", "AI agent"),
                ],
                default="application",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="savedfilter",
            name="kind",
            field=models.CharField(
                choices=[("application", "Application"), ("component", "Component"), ("ai_agent", "AI agent")],
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="AIAgent",
            fields=[
                (
                    "resource",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="ai_agent",
                        serialize=False,
                        to="catalog.resource",
                    ),
                ),
                ("purpose", models.TextField(blank=True)),
                (
                    "platform",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("copilot_studio", "Copilot Studio"),
                            ("claude", "Claude"),
                            ("azure_openai", "Azure OpenAI"),
                            ("openai", "OpenAI"),
                            ("m365_copilot_extension", "Microsoft 365 Copilot extension"),
                            ("custom", "Custom"),
                            ("other", "Other"),
                        ],
                        max_length=30,
                    ),
                ),
                (
                    "environment",
                    models.CharField(
                        blank=True,
                        choices=[("dev", "Development"), ("test", "Test"), ("prod", "Production")],
                        max_length=10,
                    ),
                ),
                ("pp_environment_id", models.CharField(blank=True, max_length=64)),
                (
                    "channels",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(
                            choices=[
                                ("teams", "Teams"),
                                ("m365_copilot", "Microsoft 365 Copilot"),
                                ("web", "Web"),
                                ("api", "API"),
                                ("email", "Email"),
                                ("other", "Other"),
                            ],
                            max_length=20,
                        ),
                        blank=True,
                        default=list,
                        size=None,
                    ),
                ),
                ("knowledge_sources", models.TextField(blank=True)),
                ("tools_connectors", models.TextField(blank=True)),
                (
                    "auth_mode",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("user", "Signed-in user"),
                            ("service_principal", "Service principal"),
                            ("api_key", "API key"),
                            ("managed_identity", "Managed identity"),
                        ],
                        max_length=20,
                    ),
                ),
                ("run_as_identity", models.CharField(blank=True, max_length=200)),
                ("data_access_scope", models.TextField(blank=True)),
                ("human_in_loop", models.BooleanField(blank=True, null=True)),
                ("actions_can_write", models.BooleanField(blank=True, null=True)),
                (
                    "risk_classification",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("minimal", "Minimal"),
                            ("limited", "Limited"),
                            ("high", "High"),
                            ("unacceptable", "Unacceptable"),
                        ],
                        max_length=20,
                    ),
                ),
                ("repo_url", models.URLField(blank=True, max_length=2000)),
                ("external_id", models.CharField(blank=True, max_length=200)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "verbose_name": "AI agent",
            },
        ),
        migrations.CreateModel(
            name="AgentModel",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
                (
                    "agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="model_links", to="catalog.aiagent"
                    ),
                ),
            ],
            options={
                "verbose_name": "AI agent model",
            },
        ),
        migrations.CreateModel(
            name="AIModel",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("version", models.CharField(blank=True, max_length=100)),
                (
                    "modality",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("text", "Text"),
                            ("multimodal", "Multimodal"),
                            ("image", "Image"),
                            ("audio", "Audio"),
                            ("embedding", "Embedding"),
                            ("other", "Other"),
                        ],
                        max_length=20,
                    ),
                ),
                ("hosting_region", models.CharField(blank=True, max_length=100)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "verbose_name": "AI model",
                "ordering": ["name", "version"],
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="aiagent",
            name="models_used",
            field=models.ManyToManyField(
                blank=True, related_name="agents", through="catalog.AgentModel", to="catalog.aimodel"
            ),
        ),
        migrations.AddField(
            model_name="agentmodel",
            name="model",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="agent_links", to="catalog.aimodel"
            ),
        ),
        migrations.CreateModel(
            name="AIProvider",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=200)),
                ("website", models.URLField(blank=True, max_length=2000)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to="core.organization"
                    ),
                ),
            ],
            options={
                "verbose_name": "AI provider",
                "verbose_name_plural": "AI providers",
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="aimodel",
            name="provider",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="models", to="catalog.aiprovider"
            ),
        ),
        migrations.AddConstraint(
            model_name="aiagent",
            constraint=models.UniqueConstraint(
                fields=("organization", "resource"), name="catalog_aiagent_org_resource_uq"
            ),
        ),
        migrations.AddIndex(
            model_name="agentmodel",
            index=models.Index(fields=["organization", "model"], name="catalog_agentmodel_model"),
        ),
        migrations.AddConstraint(
            model_name="agentmodel",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_agentmodel_org_id"),
        ),
        migrations.AddConstraint(
            model_name="agentmodel",
            constraint=models.UniqueConstraint(fields=("agent", "model"), name="catalog_agentmodel_unique"),
        ),
        migrations.AddConstraint(
            model_name="aiprovider",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_aiprovider_org_id"),
        ),
        migrations.AddConstraint(
            model_name="aiprovider",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                django.db.models.functions.text.Lower("name"),
                name="catalog_aiprovider_unique_name",
            ),
        ),
        migrations.AddIndex(
            model_name="aimodel",
            index=models.Index(fields=["organization", "provider"], name="catalog_aimodel_provider"),
        ),
        migrations.AddConstraint(
            model_name="aimodel",
            constraint=models.UniqueConstraint(fields=("organization", "id"), name="catalog_aimodel_org_id"),
        ),
        migrations.AddConstraint(
            model_name="aimodel",
            constraint=models.UniqueConstraint(
                models.F("organization"),
                models.F("provider"),
                django.db.models.functions.text.Lower("name"),
                django.db.models.functions.text.Lower("version"),
                name="catalog_aimodel_unique_name",
            ),
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
