"""Audit events are append-only in the database too.

UPDATE and DELETE on catalog_auditevent are refused by a row trigger, with two exceptions:
- nulling actor_id and nothing else, which is what ON DELETE SET NULL does when a user is deleted;
- DELETE while the transaction-local setting softwarden.audit_purge is 'on', which only
  AuditEventQuerySet.purge() sets (retention; rebuilding the synthetic scale organization).
TRUNCATE (the test runner's flush) is not a row operation and is not affected. Forward-only; the reverse
only exists so a test database can be unmigrated."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION catalog_auditevent_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('softwarden.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.actor_id IS NULL
       AND OLD.actor_id IS NOT NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'catalog_auditevent is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER catalog_auditevent_append_only
    BEFORE UPDATE OR DELETE ON catalog_auditevent
    FOR EACH ROW EXECUTE FUNCTION catalog_auditevent_append_only();
"""

REVERSE = """
DROP TRIGGER IF EXISTS catalog_auditevent_append_only ON catalog_auditevent;
DROP FUNCTION IF EXISTS catalog_auditevent_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0007_auditevent_snapshots")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
