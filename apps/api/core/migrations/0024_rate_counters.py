"""RateCounter, the shared fixed-window counters every rate limit counts in (core/counters.py),
so a limit holds across every process and pod instead of per worker. A new, empty table. Its rows are updated in place
on every counted request, so the table is created with fillfactor 70: an update that changes only `count` and
`updated_at` (no indexed column) stays on the same page as a HOT update and adds no index entries. Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0023_organization_purge_attempts"),
    ]

    operations = [
        migrations.CreateModel(
            name="RateCounter",
            fields=[
                ("id", models.CharField(max_length=100, primary_key=True, serialize=False)),
                ("scope", models.CharField(max_length=40)),
                ("key", models.CharField(max_length=32)),
                ("window_index", models.BigIntegerField()),
                ("count", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={
                "indexes": [models.Index(fields=["scope", "key"], name="core_ratecounter_scope_key")],
            },
        ),
        migrations.RunSQL(
            "ALTER TABLE core_ratecounter SET (fillfactor = 70)",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
