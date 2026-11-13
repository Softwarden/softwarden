"""A ticket link's cycle is one of its own renewal's cycles, enforced by the database, not only by
tickets/services.py. A composite foreign key from the link's (organization_id, obligation_id, cycle_id) to the
cycle's (organization_id, obligation_id, id), the unique key money 0008 adds. It is MATCH SIMPLE, so a link with no
cycle (an assessment's, or a renewal's on no particular cycle) isn't checked; deferrable, like the other composite
keys.

The services have always refused a cycle of another renewal, so no row should break the key. Should one exist, the
link is kept on its renewal and only its cycle is cleared (it couldn't be shown under a cycle of another renewal
anyway), so the upgrade never stops on it. Forward-only."""

from django.db import migrations

NAME = "tickets_ticketlink_org_obligation_cycle_fk"

CLEAR_FOREIGN_CYCLES = """
UPDATE tickets_ticketlink AS link SET cycle_id = NULL
WHERE link.cycle_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM catalog_renewalcycle AS cycle
    WHERE cycle.id = link.cycle_id
      AND cycle.obligation_id = link.obligation_id
      AND cycle.organization_id = link.organization_id
);
"""

ADD_KEY = f"""
ALTER TABLE tickets_ticketlink ADD CONSTRAINT {NAME}
FOREIGN KEY (organization_id, obligation_id, cycle_id)
REFERENCES catalog_renewalcycle (organization_id, obligation_id, id) DEFERRABLE INITIALLY DEFERRED;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("tickets", "0001_initial"),
        ("money", "0008_renewal_cycle_obligation_key"),
    ]

    operations = [
        migrations.RunSQL(sql=CLEAR_FOREIGN_CYCLES, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(sql=ADD_KEY, reverse_sql=f"ALTER TABLE tickets_ticketlink DROP CONSTRAINT {NAME};"),
    ]
