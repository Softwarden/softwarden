"""The `end_of_life` reminder source (a tracked version's end-of-life date, eol/reminders.py) and its built-in
notification rule, "End of life" at 90, 30 and 7 days before, for every organization that already exists
(notify/rule_services.BUILTINS keeps the live copy; this migration keeps its own, frozen). With it End of life leaves
the daily digest (notify/digest.SECTIONS), so nothing is emailed twice. As in notify 0011 the seeded rule has
`baselined_at` null, so the planner's first run records the stages its reminders are already inside instead of
sending them, and it writes no audit event. An organization that already has a rule with the same name gets the
built-in as "End of life (built-in)", or "(built-in 2)", "(built-in 3)" and so on when that is taken too (names are
unique per organization, ignoring case), so the upgrade never stops on a name. An organization that already has the
built-in is left alone. Forward-only."""

import django.contrib.postgres.fields
from django.db import migrations, models

KEY, NAME, KIND, TYPES, LEADS = "end_of_life", "End of life", "reminders", ["end_of_life"], [90, 30, 7]

CHOICES = [
    ("renewal_cycle", "Renewal action deadline"),
    ("contract", "Contract cancellation deadline"),
    ("periodic_review", "Periodic review"),
    ("record_review", "Record review"),
    ("custom_date", "Custom date"),
    ("credential_expiry", "Credential expiry"),
    ("vendor_risk", "Vendor risk document expiry"),
    ("end_of_life", "Version end of life"),
]


def free_name(taken):
    """The built-in's name, or the first "(built-in)", "(built-in 2)", ... variant the organization doesn't use."""
    for name in (NAME, f"{NAME} (built-in)", *(f"{NAME} (built-in {n})" for n in range(2, 1000))):
        if name.lower() not in taken:
            return name
    raise RuntimeError("No free name for the end-of-life built-in rule.")  # a thousand clashing rules: not real


def seed_existing(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    NotificationRule = apps.get_model("notify", "NotificationRule")
    have = set(NotificationRule.objects.filter(builtin_key=KEY).values_list("organization_id", flat=True))
    taken: dict = {}
    for org_id, name in NotificationRule.objects.values_list("organization_id", "name"):
        taken.setdefault(org_id, set()).add(name.lower())
    rows = [
        NotificationRule(
            organization_id=org_id,
            builtin_key=KEY,
            name=free_name(taken.get(org_id, set())),
            kind=KIND,
            source_types=TYPES,
            lead_times_days=LEADS,
            escalation_days=[],
        )
        for org_id in Organization.objects.order_by("id").values_list("id", flat=True)
        if org_id not in have
    ]
    NotificationRule.objects.bulk_create(rows)
    # The composite keys are deferred; check them now, so later migrations don't meet pending trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):
    dependencies = [
        ("notify", "0011_vendor_risk_reminders"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notificationrule",
            name="source_types",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(choices=CHOICES, max_length=30),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="reminder",
            name="source_type",
            field=models.CharField(choices=CHOICES, max_length=30),
        ),
        migrations.RunPython(seed_existing, migrations.RunPython.noop),
    ]
