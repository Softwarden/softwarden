"""Saved views: SavedFilter gains an owner, sharing and columns. `shared` replaces "user is null" as the
mark of a shared view, `user` becomes the owner of every view, and `columns` holds the view's ordered column ids.
Existing shared filters (user null) become shared views owned by whoever created them (their `created_by`; none when
that is unknown, and then only Admins manage them). A private view must have its owner (check constraint); deleting a
user leaves their shared views ownerless (`user` SET_NULL) and deletes their private ones (a pre_delete handler).
Forward-only."""

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def shared_filters_become_shared_views(apps, schema_editor):
    SavedFilter = apps.get_model("catalog", "SavedFilter")
    SavedFilter.objects.filter(user__isnull=True).update(shared=True, user_id=models.F("created_by_id"))
    # The foreign keys are deferred; check them now, or the constraint added next meets pending trigger events.
    schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
    schema_editor.execute("SET CONSTRAINTS ALL DEFERRED")


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0038_snapshotlink_type_names'),
        ('core', '0008_auditevent_append_only'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='savedfilter',
            name='columns',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=60), blank=True, default=list, size=None),
        ),
        migrations.AddField(
            model_name='savedfilter',
            name='shared',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(shared_filters_become_shared_views, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='savedfilter',
            name='user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name='savedfilter',
            constraint=models.CheckConstraint(condition=models.Q(('shared', True), ('user__isnull', False), _connector='OR'), name='catalog_savedfilter_owner'),
        ),
    ]
