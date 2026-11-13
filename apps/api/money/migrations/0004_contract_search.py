"""Contracts join global search. A stored generated `search_vector` with a GIN index, as catalog 0031 gave the catalog tables: name A, contract
and PO numbers B, notes and cost centre D, in the `simple` configuration. The vendor's name is matched through the
vendor's own vector at query time (money/search.py). An expression GIN index over `custom_field_data` finds text-like
custom field values, as on records and vendors. The table is new in money 0003, so adding the column rewrites nothing
of size. Forward-only; the reverse exists only so a test database can be unmigrated."""

from django.db import migrations

VECTOR = (
    "setweight(to_tsvector('simple'::regconfig, name), 'A')"
    " || setweight(to_tsvector('simple'::regconfig, contract_number || ' ' || po_number), 'B')"
    " || setweight(to_tsvector('simple'::regconfig, notes || ' ' || cost_center), 'D')"
)


class Migration(migrations.Migration):
    dependencies = [("money", "0003_contracts")]

    operations = [
        migrations.RunSQL(
            sql=[
                f"ALTER TABLE money_contract ADD COLUMN search_vector tsvector GENERATED ALWAYS AS ({VECTOR}) STORED;",
                "CREATE INDEX money_contract_search ON money_contract USING gin (search_vector);",
                "CREATE INDEX money_contract_cf_search ON money_contract "
                "USING gin (to_tsvector('simple'::regconfig, custom_field_data));",
                "CREATE INDEX money_contract_name_trgm ON money_contract USING gin (name gin_trgm_ops);",
            ],
            reverse_sql=[
                "DROP INDEX money_contract_name_trgm;",
                "DROP INDEX money_contract_cf_search;",
                "DROP INDEX money_contract_search;",
                "ALTER TABLE money_contract DROP COLUMN search_vector;",
            ],
        ),
    ]
