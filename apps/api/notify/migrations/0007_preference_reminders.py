"""A member can turn off reminder and overdue emails from notification rules for one organization
(NotificationPreference.reminders_enabled, default on, so nothing changes for anyone). Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notify", "0006_digest_on_deliveries"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationpreference",
            name="reminders_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
