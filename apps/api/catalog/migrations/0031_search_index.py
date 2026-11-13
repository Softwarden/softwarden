"""Global search (catalog/search.py).

Each searchable table gets a stored generated `search_vector` (tsvector, the `simple` configuration) with a GIN index.
Postgres computes the column from the row on every INSERT and UPDATE and filled it for existing rows when the column
was added, so no service, import, seed or bulk path can leave it stale, and there is nothing to backfill by hand. The
columns are not on the Django models: nothing reads or writes them through the ORM, only catalog/search.py's SQL.

Weights: A the name (a tag's, a provider's or a model's name; an attachment's title), B description and purpose (and
an attachment's file name), D other free text. A record's vendor, tags and AI models are matched through their own
tables' vectors at query time (no copy of the vendor name on the record, so renaming a vendor needs no reindex).
Custom field values are matched through an expression index over `custom_field_data` and then checked against the
organization's text-like field definitions. An anonymized Person's vector is empty, so anonymized people never
match. Forward-only; the reverse exists only so a test database can be unmigrated."""

from django.db import migrations

# (table, generated expression). Every column used is NOT NULL (blank, never null) except where coalesced.
VECTORS = [
    (
        "catalog_resource",
        "setweight(to_tsvector('simple'::regconfig, name), 'A')"
        " || setweight(to_tsvector('simple'::regconfig, description), 'B')"
        " || setweight(to_tsvector('simple'::regconfig, cost_center), 'D')",
    ),
    (
        "catalog_application",
        "setweight(to_tsvector('simple'::regconfig, purpose), 'B')"
        " || setweight(to_tsvector('simple'::regconfig,"
        " version || ' ' || entra_app_id || ' ' || service_principal_id), 'D')",
    ),
    (
        "catalog_component",
        "setweight(to_tsvector('simple'::regconfig, purpose), 'B')"
        " || setweight(to_tsvector('simple'::regconfig,"
        " host_runtime || ' ' || schedule_or_trigger || ' ' || azure_resource_id || ' ' || external_id), 'D')",
    ),
    (
        "catalog_aiagent",
        "setweight(to_tsvector('simple'::regconfig, purpose), 'B')"
        " || setweight(to_tsvector('simple'::regconfig,"
        " knowledge_sources || ' ' || tools_connectors || ' ' || data_access_scope || ' ' || run_as_identity"
        " || ' ' || external_id), 'D')",
    ),
    (
        "catalog_integration",
        "setweight(to_tsvector('simple'::regconfig, data_purpose), 'B')"
        " || setweight(to_tsvector('simple'::regconfig, protocol || ' ' || schedule), 'D')",
    ),
    (
        "catalog_vendor",
        "setweight(to_tsvector('simple'::regconfig, name), 'A')"
        " || setweight(to_tsvector('simple'::regconfig, notes), 'D')",
    ),
    ("catalog_tag", "setweight(to_tsvector('simple'::regconfig, name), 'A')"),
    ("catalog_aiprovider", "setweight(to_tsvector('simple'::regconfig, name), 'A')"),
    (
        "catalog_aimodel",
        "setweight(to_tsvector('simple'::regconfig, name), 'A')"
        " || setweight(to_tsvector('simple'::regconfig, version), 'D')",
    ),
    (
        "catalog_person",
        "CASE WHEN anonymized_at IS NULL THEN setweight(to_tsvector('simple'::regconfig, name), 'A')"
        " ELSE ''::tsvector END",
    ),
    (
        "catalog_attachment",
        "setweight(to_tsvector('simple'::regconfig, title), 'A')"
        " || setweight(to_tsvector('simple'::regconfig, filename), 'B')",
    ),
]

FORWARD = [
    statement
    for table, expression in VECTORS
    for statement in (
        f"ALTER TABLE {table} ADD COLUMN search_vector tsvector GENERATED ALWAYS AS ({expression}) STORED;",
        f"CREATE INDEX {table}_search ON {table} USING gin (search_vector);",
    )
] + [
    # Custom field values: every string in the JSON, narrowed to text-like fields by the query (catalog/search.py).
    "CREATE INDEX catalog_resource_cf_search ON catalog_resource"
    " USING gin (to_tsvector('simple'::regconfig, custom_field_data));",
    "CREATE INDEX catalog_vendor_cf_search ON catalog_vendor"
    " USING gin (to_tsvector('simple'::regconfig, custom_field_data));",
    # Typo tolerance when nothing matches (trigram word similarity; pg_trgm is installed by migration 0006, which
    # already indexes catalog_resource.name the same way).
    "CREATE INDEX catalog_vendor_name_trgm ON catalog_vendor USING gin (name gin_trgm_ops);",
    "CREATE INDEX catalog_person_name_trgm ON catalog_person USING gin (name gin_trgm_ops);",
]

REVERSE = [
    "DROP INDEX IF EXISTS catalog_person_name_trgm;",
    "DROP INDEX IF EXISTS catalog_vendor_name_trgm;",
    "DROP INDEX IF EXISTS catalog_vendor_cf_search;",
    "DROP INDEX IF EXISTS catalog_resource_cf_search;",
] + [f"ALTER TABLE {table} DROP COLUMN IF EXISTS search_vector;" for table, _ in reversed(VECTORS)]


class Migration(migrations.Migration):
    dependencies = [("catalog", "0030_attachments")]

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]
