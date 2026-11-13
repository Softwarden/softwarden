"""A unique key on a renewal cycle's (organization, obligation, id). The id alone is already unique,
so no existing row can break it; it is there to be referenced, so tickets 0002 can say in the database that a ticket
link's cycle is one of its own renewal's cycles (a three-column composite foreign key), not only in the service.
Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("money", "0007_calendar_feed"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="renewalcycle",
            constraint=models.UniqueConstraint(
                fields=("organization", "obligation", "id"), name="catalog_renewalcycle_org_obligation_id"
            ),
        ),
    ]
