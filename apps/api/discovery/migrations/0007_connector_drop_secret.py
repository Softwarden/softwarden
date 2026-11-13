"""Drop the connector's own secret slot. discovery 0006 copied every stored value, still encrypted, to the
connector's MicrosoftConnection; from here on only connections hold credentials. Forward-only."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('discovery', '0006_connector_credentials_to_connections'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='connector',
            name='secret_ciphertext',
        ),
        migrations.RemoveField(
            model_name='connector',
            name='secret_key_id',
        ),
        migrations.RemoveField(
            model_name='connector',
            name='secret_set_at',
        ),
    ]
