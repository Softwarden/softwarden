"""Notification rules: NotificationRule (lead times, escalation steps, channels, audiences, fallback, filters)
with its four link tables (people, escalation people, Teams and webhook destinations); on Delivery the `rule`
reference, the notice's `due_date` and the `baseline` flag; composite foreign keys for every tenant reference;
and the built-in rules for every organization that already exists (notify/rule_services.BUILTINS keeps the live copy; this migration keeps its own, frozen). Seeding is
part of the organization's setup, so it writes no audit event. The seeded rules have `baselined_at` null: the
planner's first run records the stages their reminders are already inside as done instead of sending them, so an
upgrade sends no burst (notify/rules.baseline). Forward-only."""

import django.contrib.postgres.fields
import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("notify_notificationrule", "notify_notificationrule_org_team_fk", "team_id", "catalog_team"),
    ("notify_notificationrule", "notify_notificationrule_org_escalation_team_fk", "escalation_team_id", "catalog_team"),
    ("notify_notificationrule", "notify_notificationrule_org_fallback_team_fk", "fallback_team_id", "catalog_team"),
    ("notify_notificationrule", "notify_notificationrule_org_fallback_person_fk", "fallback_person_id", "catalog_person"),
    ("notify_notificationrule", "notify_notificationrule_org_vendor_fk", "vendor_id", "catalog_vendor"),
    ("notify_notificationruleperson", "notify_ruleperson_org_rule_fk", "rule_id", "notify_notificationrule"),
    ("notify_notificationruleperson", "notify_ruleperson_org_person_fk", "person_id", "catalog_person"),
    ("notify_notificationruleescalationperson", "notify_ruleescperson_org_rule_fk", "rule_id", "notify_notificationrule"),
    ("notify_notificationruleescalationperson", "notify_ruleescperson_org_person_fk", "person_id", "catalog_person"),
    ("notify_notificationruleteamsdestination", "notify_ruleteams_org_rule_fk", "rule_id", "notify_notificationrule"),
    (
        "notify_notificationruleteamsdestination",
        "notify_ruleteams_org_destination_fk",
        "destination_id",
        "notify_teamsdestination",
    ),
    ("notify_notificationrulewebhookdestination", "notify_rulewebhook_org_rule_fk", "rule_id", "notify_notificationrule"),
    (
        "notify_notificationrulewebhookdestination",
        "notify_rulewebhook_org_destination_fk",
        "destination_id",
        "notify_webhookdestination",
    ),
    ("notify_delivery", "notify_delivery_org_rule_fk", "rule_id", "notify_notificationrule"),
]

# (builtin_key, name, kind, source types, lead times, escalation days) as first shipped.
BUILTINS = (
    ("renewals_contracts", "Renewals and contracts", "reminders", ["renewal_cycle", "contract"], [90, 60, 30, 7], []),
    ("record_reviews", "Record reviews", "reminders", ["record_review"], [0], []),
    ("periodic_reviews", "Periodic reviews", "reminders", ["periodic_review"], [30, 0], []),
    ("custom_dates", "Custom dates", "reminders", ["custom_date"], [30, 7], []),
    ("credentials", "Credential expiry", "reminders", ["credential_expiry"], [30, 7, 1], []),
    ("daily_digest", "Daily digest", "digest", [], [], []),
)


def seed_existing(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    NotificationRule = apps.get_model("notify", "NotificationRule")
    rows = []
    for org_id in Organization.objects.order_by("id").values_list("id", flat=True):
        rows += [
            NotificationRule(
                organization_id=org_id,
                builtin_key=key,
                name=name,
                kind=kind,
                source_types=types,
                lead_times_days=leads,
                escalation_days=steps,
            )
            for key, name, kind, types, leads, steps in BUILTINS
        ]
    NotificationRule.objects.bulk_create(rows)
    # The composite keys are deferred; check them now, or the index and column changes after this meet pending
    # trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0038_snapshotlink_type_names'),
        ('core', '0008_auditevent_append_only'),
        ('notify', '0004_channels_and_deliveries'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationRule',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=200)),
                ('kind', models.CharField(choices=[('reminders', 'Reminder notices'), ('digest', 'Daily digest')], default='reminders', max_length=20)),
                ('builtin_key', models.CharField(blank=True, max_length=40)),
                ('enabled', models.BooleanField(default=True)),
                ('baselined_at', models.DateTimeField(blank=True, null=True)),
                ('source_types', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('renewal_cycle', 'Renewal action deadline'), ('contract', 'Contract cancellation deadline'), ('periodic_review', 'Periodic review'), ('record_review', 'Record review'), ('custom_date', 'Custom date'), ('credential_expiry', 'Credential expiry')], max_length=30), blank=True, default=list, size=None)),
                ('lead_times_days', django.contrib.postgres.fields.ArrayField(base_field=models.PositiveIntegerField(), blank=True, default=list, size=None)),
                ('escalation_days', django.contrib.postgres.fields.ArrayField(base_field=models.PositiveIntegerField(), blank=True, default=list, size=None)),
                ('escalate_when_acknowledged', models.BooleanField(default=True)),
                ('send_email', models.BooleanField(default=True)),
                ('notify_routed', models.BooleanField(default=True)),
                ('roles', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('admin', 'Admin'), ('editor', 'Editor'), ('viewer', 'Viewer')], max_length=10), blank=True, default=list, size=None)),
                ('escalation_roles', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(choices=[('admin', 'Admin'), ('editor', 'Editor'), ('viewer', 'Viewer')], max_length=10), blank=True, default=list, size=None)),
                ('criticalities', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=20), blank=True, default=list, size=None)),
                ('record_kinds', django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=20), blank=True, default=list, size=None)),
                ('min_annual_value', models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
            ],
            options={
                'ordering': ['name'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='NotificationRuleEscalationPerson',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='NotificationRulePerson',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='NotificationRuleTeamsDestination',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='NotificationRuleWebhookDestination',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='escalation_team',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.team'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='fallback_person',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.person'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='fallback_team',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.team'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='team',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.team'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='vendor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.vendor'),
        ),
        migrations.AddField(
            model_name='delivery',
            name='due_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='delivery',
            name='baseline',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='delivery',
            name='rule',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='notify.notificationrule'),
        ),
        migrations.AddField(
            model_name='notificationruleescalationperson',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddField(
            model_name='notificationruleescalationperson',
            name='person',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.person'),
        ),
        migrations.AddField(
            model_name='notificationruleescalationperson',
            name='rule',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='notify.notificationrule'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='escalation_people',
            field=models.ManyToManyField(blank=True, related_name='+', through='notify.NotificationRuleEscalationPerson', to='catalog.person'),
        ),
        migrations.AddField(
            model_name='notificationruleperson',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddField(
            model_name='notificationruleperson',
            name='person',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='catalog.person'),
        ),
        migrations.AddField(
            model_name='notificationruleperson',
            name='rule',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='notify.notificationrule'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='people',
            field=models.ManyToManyField(blank=True, related_name='+', through='notify.NotificationRulePerson', to='catalog.person'),
        ),
        migrations.AddField(
            model_name='notificationruleteamsdestination',
            name='destination',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='notify.teamsdestination'),
        ),
        migrations.AddField(
            model_name='notificationruleteamsdestination',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddField(
            model_name='notificationruleteamsdestination',
            name='rule',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='notify.notificationrule'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='teams_destinations',
            field=models.ManyToManyField(blank=True, related_name='+', through='notify.NotificationRuleTeamsDestination', to='notify.teamsdestination'),
        ),
        migrations.AddField(
            model_name='notificationrulewebhookdestination',
            name='destination',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='notify.webhookdestination'),
        ),
        migrations.AddField(
            model_name='notificationrulewebhookdestination',
            name='organization',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization'),
        ),
        migrations.AddField(
            model_name='notificationrulewebhookdestination',
            name='rule',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='notify.notificationrule'),
        ),
        migrations.AddField(
            model_name='notificationrule',
            name='webhook_destinations',
            field=models.ManyToManyField(blank=True, related_name='+', through='notify.NotificationRuleWebhookDestination', to='notify.webhookdestination'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleescalationperson',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_notificationruleescalationperson_org_id'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleescalationperson',
            constraint=models.UniqueConstraint(fields=('rule', 'person'), name='notify_notificationruleescperson_unique'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleperson',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_notificationruleperson_org_id'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleperson',
            constraint=models.UniqueConstraint(fields=('rule', 'person'), name='notify_notificationruleperson_unique'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleteamsdestination',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_notificationruleteamsdestination_org_id'),
        ),
        migrations.AddConstraint(
            model_name='notificationruleteamsdestination',
            constraint=models.UniqueConstraint(fields=('rule', 'destination'), name='notify_notificationruleteams_unique'),
        ),
        migrations.AddConstraint(
            model_name='notificationrulewebhookdestination',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_notificationrulewebhookdestination_org_id'),
        ),
        migrations.AddConstraint(
            model_name='notificationrulewebhookdestination',
            constraint=models.UniqueConstraint(fields=('rule', 'destination'), name='notify_notificationrulewebhook_unique'),
        ),
        migrations.AddConstraint(
            model_name='notificationrule',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='notify_notificationrule_org_id'),
        ),
        migrations.AddConstraint(
            model_name='notificationrule',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), models.F('organization'), name='notify_notificationrule_unique_name'),
        ),
        migrations.AddConstraint(
            model_name='notificationrule',
            constraint=models.UniqueConstraint(condition=models.Q(('builtin_key', ''), _negated=True), fields=('organization', 'builtin_key'), name='notify_notificationrule_unique_builtin'),
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for table, name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE {table} DROP CONSTRAINT {name};\n" for table, name, *_rest in reversed(KEYS)
            ),
        ),
        migrations.RunPython(seed_existing, migrations.RunPython.noop),
    ]
