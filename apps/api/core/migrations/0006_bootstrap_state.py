"""First-run setup: the BootstrapState singleton (core/bootstrap.py). An installation that already has an
organization or a user is marked completed, so setup never reopens on an upgrade. Forward-only."""

from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def create_state(apps, schema_editor):
    BootstrapState = apps.get_model("core", "BootstrapState")
    Organization = apps.get_model("core", "Organization")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    existing = Organization.objects.exists() or User.objects.exists()
    BootstrapState.objects.create(id=1, completed_at=timezone.now() if existing else None)


def drop_state(apps, schema_editor):
    apps.get_model("core", "BootstrapState").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_jobs_and_schedules'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BootstrapState',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'constraints': [models.CheckConstraint(condition=models.Q(('id', 1)), name='core_bootstrapstate_singleton')],
            },
        ),
        # The reverse only lets the test suite replay migrations; releases never migrate backwards.
        migrations.RunPython(create_state, drop_state),
    ]
