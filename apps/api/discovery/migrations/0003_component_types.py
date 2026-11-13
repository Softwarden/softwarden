# Component types gain api, service, workflow and infra_automation (catalog 0024); choices only, no data change.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('discovery', '0002_connector_secret_key_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='observation',
            name='suggested_component_type',
            field=models.CharField(blank=True, choices=[('script', 'Script'), ('scheduled_job', 'Scheduled job'), ('runbook', 'Runbook'), ('function', 'Function'), ('logic_app', 'Logic App'), ('automation_flow', 'Automation flow'), ('report', 'Report'), ('ai_service', 'AI service'), ('middleware', 'Middleware'), ('internal_api', 'Internal API'), ('api', 'API'), ('service', 'Service'), ('workflow', 'Workflow'), ('infra_automation', 'Infrastructure automation'), ('other', 'Other')], max_length=30),
        ),
    ]
