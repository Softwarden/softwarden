"""Resource extensions, step 2 of 3: give every existing resource its slug, archived_at and matching extension row. Forward-only."""

from django.db import migrations
from django.utils.text import slugify


def move_data(apps, schema_editor):
    Resource = apps.get_model("catalog", "Resource")
    Application = apps.get_model("catalog", "Application")
    Component = apps.get_model("catalog", "Component")
    taken = set()
    for r in Resource.objects.order_by("created_at", "id"):
        base = slugify(r.name)[:200] or r.kind
        slug, n = base, 1
        while (r.organization_id, r.kind, slug) in taken:
            n += 1
            slug = f"{base}-{n}"
        taken.add((r.organization_id, r.kind, slug))
        r.slug = slug
        r.archived_at = r.updated_at if r.archived else None
        r.save(update_fields=["slug", "archived_at"])
        if r.kind == "component":
            Component.objects.create(resource=r, purpose=r.purpose)
        else:
            Application.objects.create(resource=r, purpose=r.purpose, vendor_id=r.vendor_id)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_resource_extensions"),
    ]

    operations = [
        migrations.RunPython(move_data, migrations.RunPython.noop, elidable=False),
    ]
