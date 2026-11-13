"""More Integration fields: direction, auth_method and
monitoring_url. All blank by default (not recorded), so existing rows stay valid. The data classification is the
Resource column added in 0024, now also written for integrations. Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0034_relationship_type_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='integration',
            name='auth_method',
            field=models.CharField(blank=True, choices=[('oauth', 'OAuth 2.0'), ('api_key', 'API key'), ('basic', 'Username and password'), ('certificate', 'Certificate'), ('managed_identity', 'Managed identity'), ('ssh_key', 'SSH key'), ('none', 'No authentication'), ('other', 'Other')], max_length=20),
        ),
        migrations.AddField(
            model_name='integration',
            name='direction',
            field=models.CharField(blank=True, choices=[('one_way', 'One way (source to target)'), ('two_way', 'Both ways')], max_length=20),
        ),
        migrations.AddField(
            model_name='integration',
            name='monitoring_url',
            field=models.URLField(blank=True, max_length=2000),
        ),
    ]
