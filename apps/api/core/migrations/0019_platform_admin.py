"""Platform admin console: platform staff, the platform audit stream, usage snapshots, capability
overrides, an organization's creation time and suspension, and `impersonated_by` on audit events.

- `organization.created_at` is backfilled from the organization's oldest membership or audit event (else now).
- The audit-event trigger of core 0008 is replaced so that, besides nulling `actor_id`, deleting an account may null
  `impersonated_by_id`; anything else stays refused.
- core_platformauditevent gets the same append-only trigger (nulling `actor_id` only; no purge path).

Forward-only; the reverse SQL exists only so a test database can be unmigrated."""

import django.db.models.deletion
import django.db.models.functions.datetime
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Min

AUDIT_FORWARD = """
CREATE OR REPLACE FUNCTION catalog_auditevent_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('softwarden.audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE'
       AND (to_jsonb(NEW) - 'actor_id' - 'impersonated_by_id') = (to_jsonb(OLD) - 'actor_id' - 'impersonated_by_id')
       AND (NEW.actor_id IS NULL OR NEW.actor_id = OLD.actor_id)
       AND (NEW.impersonated_by_id IS NULL OR NEW.impersonated_by_id = OLD.impersonated_by_id)
       AND (NEW.actor_id IS DISTINCT FROM OLD.actor_id
            OR NEW.impersonated_by_id IS DISTINCT FROM OLD.impersonated_by_id) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'catalog_auditevent is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""

# The core 0008 function, as it was.
AUDIT_REVERSE = """
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
"""

PLATFORM_FORWARD = """
CREATE OR REPLACE FUNCTION core_platformauditevent_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.actor_id IS NULL
       AND OLD.actor_id IS NOT NULL
       AND (to_jsonb(NEW) - 'actor_id') = (to_jsonb(OLD) - 'actor_id') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'core_platformauditevent is append-only (% refused)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_platformauditevent_append_only
    BEFORE UPDATE OR DELETE ON core_platformauditevent
    FOR EACH ROW EXECUTE FUNCTION core_platformauditevent_append_only();
"""

PLATFORM_REVERSE = """
DROP TRIGGER IF EXISTS core_platformauditevent_append_only ON core_platformauditevent;
DROP FUNCTION IF EXISTS core_platformauditevent_append_only();
"""


def backfill_created_at(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    Membership = apps.get_model("core", "Membership")
    AuditEvent = apps.get_model("core", "AuditEvent")
    first_member = dict(
        Membership.objects.values("organization_id").annotate(at=Min("created_at")).values_list("organization_id", "at")
    )
    first_event = dict(
        AuditEvent.objects.values("organization_id").annotate(at=Min("created_at")).values_list("organization_id", "at")
    )
    for org in Organization.objects.only("id", "created_at"):
        found = [at for at in (first_member.get(org.id), first_event.get(org.id)) if at is not None]
        if found:
            Organization.objects.filter(pk=org.pk).update(created_at=min(found))


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_cloud_signup_and_lifecycle'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='auditevent',
            name='impersonated_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='organization',
            name='created_at',
            field=models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), default=django.utils.timezone.now, editable=False),
        ),
        migrations.AddField(
            model_name='organization',
            name='suspended_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='organization',
            name='suspended_reason',
            field=models.CharField(blank=True, db_default='', default='', max_length=500),
        ),
        migrations.CreateModel(
            name='PlatformStaff',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('granted_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='platform_staff', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['granted_at'],
            },
        ),
        migrations.CreateModel(
            name='CapabilityOverride',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('capability', models.CharField(max_length=60)),
                ('effect', models.CharField(choices=[('grant', 'Grant'), ('revoke', 'Revoke')], max_length=10)),
                ('reason', models.CharField(max_length=500)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['capability'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_capabilityoverride_org_id'), models.UniqueConstraint(fields=('organization', 'capability'), name='core_capabilityoverride_one')],
            },
        ),
        migrations.CreateModel(
            name='OrganizationUsage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('computed_at', models.DateTimeField()),
                ('counts', models.JSONField(default=dict)),
                ('last_activity_at', models.DateTimeField(blank=True, null=True)),
                ('last_sign_in_at', models.DateTimeField(blank=True, null=True)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_organizationusage_org_id'), models.UniqueConstraint(fields=('organization',), name='core_organizationusage_one_per_org')],
            },
        ),
        migrations.CreateModel(
            name='PlatformAuditEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('actor_email', models.CharField(blank=True, default='', max_length=254)),
                ('action', models.CharField(max_length=60)),
                ('organization_id', models.UUIDField(blank=True, null=True)),
                ('details', models.JSONField(default=dict)),
                ('request_id', models.UUIDField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['created_at', 'id'], name='core_platformaudit_time'), models.Index(condition=models.Q(('organization_id__isnull', False)), fields=['organization_id', 'created_at'], name='core_platformaudit_org')],
            },
        ),
        migrations.RunPython(backfill_created_at, migrations.RunPython.noop),
        migrations.RunSQL(AUDIT_FORWARD, AUDIT_REVERSE),
        migrations.RunSQL(PLATFORM_FORWARD, PLATFORM_REVERSE),
    ]
