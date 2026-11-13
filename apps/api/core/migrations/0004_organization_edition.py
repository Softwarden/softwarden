"""Editions: Organization gains `slug`, `edition`, `licence_key_ciphertext` and
`settings`.

- `slug` is added nullable, backfilled from each name (organizations in name then id order, so a collision gets
  `-2`, `-3`, ... deterministically), then made NOT NULL and unique. It is added without an index so that making it
  unique creates the one `_like` index Postgres needs (adding it indexed and then unique creates that index twice).
- `edition` defaults to community for every existing organization; an operator raises it with `set_edition`.
- The licence key slot (`licence_key_ciphertext`, `licence_key_key_id`, `licence_key_set_at`) is empty and `settings`
  is `{}` for existing rows.

Forward-only; the reverse of the backfill does nothing so the test suite can unwind migrations."""

from django.db import migrations, models
from django.utils.text import slugify


def unique_slug(name, taken):
    """A frozen copy of core.models.unique_org_slug as it was when this migration shipped."""
    base = (slugify(name) or "organization")[:54].strip("-") or "organization"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def backfill_slugs(apps, schema_editor):
    Organization = apps.get_model("core", "Organization")
    taken: set[str] = set()
    for org in Organization.objects.order_by("name", "id"):
        org.slug = unique_slug(org.name, taken)
        taken.add(org.slug)
        Organization.objects.filter(pk=org.pk).update(slug=org.slug)


class Migration(migrations.Migration):
    dependencies = [("core", "0003_membership_per_organization")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="slug",
            field=models.SlugField(db_index=False, max_length=60, null=True),
        ),
        migrations.RunPython(backfill_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="organization",
            name="slug",
            field=models.SlugField(max_length=60, unique=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="edition",
            field=models.CharField(
                choices=[("community", "Community"), ("cloud", "Cloud"), ("enterprise", "Enterprise")],
                default="community",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="licence_key_ciphertext",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="organization",
            name="licence_key_key_id",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="organization",
            name="licence_key_set_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="organization",
            name="settings",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
