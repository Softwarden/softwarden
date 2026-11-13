"""Saved view subscriptions: SavedViewSubscription (a weekly email of a saved view's rows to one member,
notify/subscriptions.py) with composite foreign keys to its view and membership, and the member's
NotificationPreference.subscriptions_enabled (default on, so nothing changes for anyone). Forward-only."""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


KEYS = [
    # (constraint, column, referenced table)
    ("notify_savedviewsub_org_view_fk", "saved_view_id", "catalog_savedfilter"),
    ("notify_savedviewsub_org_membership_fk", "membership_id", "catalog_membership"),
]


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0039_saved_views'),
        ('core', '0008_auditevent_append_only'),
        ('notify', '0007_preference_reminders'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='notificationpreference',
            name='subscriptions_enabled',
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name='SavedViewSubscription',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('weekday', models.PositiveSmallIntegerField()),
                ('active', models.BooleanField(default=True)),
                ('last_sent', models.DateField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('membership', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='core.membership')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('saved_view', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='catalog.savedfilter')),
            ],
            options={
                'ordering': ['weekday', 'created_at', 'id'],
                'abstract': False,
                'indexes': [models.Index(fields=['organization', 'weekday', 'active'], name='notify_savedviewsub_day')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='notify_savedviewsubscription_org_id'), models.UniqueConstraint(fields=('saved_view', 'membership'), name='notify_savedviewsubscription_unique'), models.CheckConstraint(condition=models.Q(('weekday__lte', 6)), name='notify_savedviewsubscription_weekday')],
            },
        ),
        migrations.RunSQL(
            sql="".join(
                f"ALTER TABLE notify_savedviewsubscription ADD CONSTRAINT {name} FOREIGN KEY (organization_id, {column}) "
                f"REFERENCES {target} (organization_id, id) DEFERRABLE INITIALLY DEFERRED;\n"
                for name, column, target in KEYS
            ),
            reverse_sql="".join(
                f"ALTER TABLE notify_savedviewsubscription DROP CONSTRAINT {name};\n" for name, *_rest in reversed(KEYS)
            ),
        ),
    ]
