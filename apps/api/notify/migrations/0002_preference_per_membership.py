"""Multi-organization membership: the digest preference belongs to a membership, not to a user.

Each existing row (keyed by user, with the organization it was saved in) moves to the membership with that user and
organization. A row with no such membership is meaningless (the user isn't in that organization any more) and is
deleted; until this migration every user had one membership, so there are none on a normal install. The row gets a
UUID key, UNIQUE (organization, id), and a composite foreign key (organization_id, membership_id) to the membership,
so a preference can never point at another organization's membership. Forward-only; the reverse SQL only
lets the test suite unwind migrations."""

import uuid

import django.db.models.deletion
from django.db import migrations, models

TABLE = "catalog_notificationpreference"

FORWARD = f"""
    ALTER TABLE {TABLE} ADD COLUMN membership_id uuid NULL;
    UPDATE {TABLE} p SET membership_id = m.id
        FROM catalog_membership m
        WHERE m.user_id = p.user_id AND m.organization_id = p.organization_id;
    DELETE FROM {TABLE} WHERE membership_id IS NULL;
    ALTER TABLE {TABLE} ALTER COLUMN membership_id SET NOT NULL;
    ALTER TABLE {TABLE} ADD COLUMN id uuid NOT NULL DEFAULT gen_random_uuid();
    ALTER TABLE {TABLE} DROP CONSTRAINT catalog_notificationpreference_pkey;
    ALTER TABLE {TABLE} DROP COLUMN user_id;
    ALTER TABLE {TABLE} ALTER COLUMN id DROP DEFAULT;
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_pkey PRIMARY KEY (id);
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_membership_id_key UNIQUE (membership_id);
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_membership_id_fk
        FOREIGN KEY (membership_id) REFERENCES catalog_membership (id) DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_org_membership_fk
        FOREIGN KEY (organization_id, membership_id) REFERENCES catalog_membership (organization_id, id)
        DEFERRABLE INITIALLY DEFERRED;
"""

# Only for unwinding in tests; releases never run it.
REVERSE = f"""
    ALTER TABLE {TABLE} DROP CONSTRAINT catalog_notificationpreference_org_membership_fk;
    ALTER TABLE {TABLE} ADD COLUMN user_id integer NULL;
    UPDATE {TABLE} p SET user_id = m.user_id FROM catalog_membership m WHERE m.id = p.membership_id;
    ALTER TABLE {TABLE} DROP CONSTRAINT catalog_notificationpreference_pkey;
    ALTER TABLE {TABLE} DROP COLUMN id;
    ALTER TABLE {TABLE} DROP COLUMN membership_id;
    ALTER TABLE {TABLE} ALTER COLUMN user_id SET NOT NULL;
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_pkey PRIMARY KEY (user_id);
    ALTER TABLE {TABLE} ADD CONSTRAINT catalog_notificationpreference_user_id_adffddfe_fk_auth_user_id
        FOREIGN KEY (user_id) REFERENCES auth_user (id) DEFERRABLE INITIALLY DEFERRED;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_membership_per_organization"),
        ("notify", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)],
            state_operations=[
                migrations.RemoveField(model_name="notificationpreference", name="user"),
                migrations.AddField(
                    model_name="notificationpreference",
                    name="id",
                    field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                migrations.AddField(
                    model_name="notificationpreference",
                    name="membership",
                    field=models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notification_preference",
                        to="core.membership",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="notificationpreference",
            constraint=models.UniqueConstraint(
                fields=("organization", "id"), name="catalog_notificationpreference_org_id"
            ),
        ),
    ]
