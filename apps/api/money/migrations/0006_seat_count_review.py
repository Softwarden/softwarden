"""Renewal and licences: SeatCountReview, a person's decision (applied or dismissed) on a proposed seat count
from a licence observation, with composite foreign keys to the pool and the observation, added with the table,
which is empty. Forward-only."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models

KEYS = [
    # (table, constraint, column, referenced table)
    ("money_seatcountreview", "money_seatcountreview_org_pool_fk", "pool_id", "money_licensepool"),
    ("money_seatcountreview", "money_seatcountreview_org_observation_fk", "observation_id", "catalog_observation"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_auditevent_append_only'),
        ('discovery', '0004_observation_license_kind'),
        ('money', '0005_renewalcycle_decision_terms'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SeatCountReview',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('decision', models.CharField(choices=[('applied', 'Applied'), ('dismissed', 'Dismissed')], max_length=10)),
                ('fingerprint', models.CharField(max_length=64)),
                ('reason', models.CharField(blank=True, max_length=500)),
                ('decided_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('observation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='seat_reviews', to='discovery.observation')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('pool', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='seat_reviews', to='money.licensepool')),
            ],
            options={
                'verbose_name': 'seat count review',
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'observation'], name='money_seatcountreview_obs')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='money_seatcountreview_org_id'), models.UniqueConstraint(fields=('pool', 'observation'), name='money_seatcountreview_one_per_pair')],
            },
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
    ]
