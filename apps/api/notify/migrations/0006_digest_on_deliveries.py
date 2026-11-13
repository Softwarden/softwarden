"""The daily digest sends through deliveries (notify/deliveries.py), so it uses the organization's email
settings and shows in the notification log. Each existing Digest becomes a Delivery (kind `digest`, key
`digest:{user_id}:{date}`, rule the built-in Daily digest) carrying its subject, text and HTML, status, attempts,
error and times; pending digests become queued deliveries, which the `notify.deliveries` sweep gives a job, unless
they were built more than a day ago: those become failed ("Not sent: pending since <date> ..."), not sent late. Then the
message and status columns leave `catalog_digest`, which keeps who, when, how many items and its delivery (composite
key). The retired `digest.send` schedule's row is removed (deliveries are jobs now). Forward-only."""

import datetime

import django.db.models.deletion
from django.db import migrations, models

STATUS = {"pending": "queued", "sent": "sent", "failed": "failed", "skipped": "skipped"}
STALE = datetime.timedelta(days=1)  # a digest pending longer than this is no longer news: failed, not sent late


def move_digests(apps, schema_editor):
    Digest = apps.get_model("notify", "Digest")
    Delivery = apps.get_model("notify", "Delivery")
    NotificationRule = apps.get_model("notify", "NotificationRule")
    Schedule = apps.get_model("core", "Schedule")
    rules = dict(
        NotificationRule.objects.filter(builtin_key="daily_digest").values_list("organization_id", "id")
    )
    from django.utils import timezone

    now = timezone.now()
    for digest in Digest.objects.select_related("user").order_by("for_date", "id").iterator():
        status = STATUS.get(digest.status, "failed")
        last_error = digest.last_error
        if status == "queued" and digest.created_at < now - STALE:
            status = "failed"
            last_error = f"Not sent: pending since {digest.for_date.isoformat()} when deliveries were introduced."
        delivery = Delivery.objects.create(
            organization_id=digest.organization_id,
            channel="email",
            kind="digest",
            status=status,
            dedupe_key=f"digest:{digest.user_id}:{digest.for_date.isoformat()}",
            recipient=(digest.user.email or "")[:254],
            subject=digest.subject[:300],
            content={"text": digest.text_body, "html": digest.html_body},
            rule_id=rules.get(digest.organization_id),
            rule_name="Daily digest",
            attempts=min(digest.attempts, 32767),
            max_attempts=4,
            next_attempt_at=digest.next_attempt_at if status == "queued" else None,
            last_error=last_error[:500],
            sent_at=digest.sent_at,
        )
        # Keep the log's order: a delivery reads as queued when its digest was built.
        Delivery.objects.filter(id=delivery.id).update(created_at=digest.created_at, updated_at=digest.updated_at)
        Digest.objects.filter(id=digest.id).update(delivery_id=delivery.id)
    Schedule.objects.filter(name="digest.send").delete()
    # The composite keys are deferred; check them now, or the index and column changes after this meet pending
    # trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):

    dependencies = [
        ("notify", "0005_notification_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="digest",
            name="delivery",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="notify.delivery"
            ),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE catalog_digest ADD CONSTRAINT notify_digest_org_delivery_fk FOREIGN KEY "
            "(organization_id, delivery_id) REFERENCES notify_delivery (organization_id, id) "
            "DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE catalog_digest DROP CONSTRAINT notify_digest_org_delivery_fk;",
        ),
        migrations.RunPython(move_digests, migrations.RunPython.noop),
        migrations.RemoveIndex(model_name="digest", name="catalog_digest_due"),
        migrations.RemoveField(model_name="digest", name="attempts"),
        migrations.RemoveField(model_name="digest", name="html_body"),
        migrations.RemoveField(model_name="digest", name="last_error"),
        migrations.RemoveField(model_name="digest", name="next_attempt_at"),
        migrations.RemoveField(model_name="digest", name="sent_at"),
        migrations.RemoveField(model_name="digest", name="status"),
        migrations.RemoveField(model_name="digest", name="subject"),
        migrations.RemoveField(model_name="digest", name="text_body"),
    ]
