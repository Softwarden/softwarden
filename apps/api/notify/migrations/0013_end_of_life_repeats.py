"""Notification rules can repeat (`repeat_every_days`: an escalation step at every multiple of it after the
due date, notify/rules.decide), and the built-in "End of life" rule repeats every 30 days while a version is past its
end of life or flagged end of life with no date. The End of life rules that
already exist get `repeat_every_days` 30; those with no escalation steps of their own (the flag had no effect on them)
also get `escalate_when_acknowledged` off, so acknowledging a version's reminder quiets its repeats as it quiets its
leads. Their `baselined_at` goes back to null, so the planner's first run records the repeat each version past its
end of life is already inside instead of sending it: no burst on the upgrade, and each one is announced at its next
30-day mark. No audit event (as notify 0011 and 0012). Forward-only."""

from django.db import migrations, models

KEY, REPEAT = "end_of_life", 30


def repeat_existing(apps, schema_editor):
    NotificationRule = apps.get_model("notify", "NotificationRule")
    rules = NotificationRule.objects.filter(builtin_key=KEY, repeat_every_days__isnull=True)
    rules.filter(escalation_days=[]).update(escalate_when_acknowledged=False)
    rules.update(repeat_every_days=REPEAT, baselined_at=None)


class Migration(migrations.Migration):
    dependencies = [
        ("notify", "0012_end_of_life_reminders"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrule",
            name="repeat_every_days",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(repeat_existing, migrations.RunPython.noop),
    ]
