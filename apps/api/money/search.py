"""Contracts in global search (the registry and the response are core/search.py, the catalog's searchers
catalog/search.py). Registered from MoneyConfig.ready() with the `contracts` capability.

A contract matches on its own vector (money 0004: name A, contract and PO numbers B, notes and cost centre D), on its
vendor's name (the vendor's own vector, weight A, so renaming a vendor needs no rewrite) and on its text-like custom
field values. Every word must match somewhere in those. Archived contracts are left out unless asked. The score is
`ts_rank` over everything it matched through (the vendor's name counts as B, custom values as C) plus 1 when the name
equals the query and 0.5 when it starts with it, as for records. One SQL statement; typo matching is trigram word
similarity on the name (money_contract_name_trgm)."""

from __future__ import annotations

from typing import Any

from catalog.search import CF_SNIPPETS, CF_TEXT, HEADLINE, SNIPPET, _bonus, _common, _matched, _rows, _similar_rows
from catalog.search import _text_fields as text_fields
from core import search as core_search
from core.capabilities import CONTRACTS
from core.search import Context, Group, Hit, Searcher, snippet

LABELS = {"numbers": "Contract or PO number", "vendor": "Vendor", "notes": "Notes", "details": "Details"}


class ContractSearcher(Searcher):
    kinds = ("contract",)
    capabilities = {"contract": CONTRACTS}

    def sources(self, cf: list[str]) -> list[str]:
        found = [
            "SELECT id FROM money_contract WHERE organization_id = %(org)s AND search_vector @@ @T@",
            "SELECT c.id FROM money_contract c JOIN catalog_vendor v ON v.id = c.vendor_id "
            "WHERE c.organization_id = %(org)s AND v.search_vector @@ @A@",
        ]
        if cf:
            found.append(
                "SELECT id FROM money_contract c WHERE c.organization_id = %(org)s "
                "AND to_tsvector('simple'::regconfig, c.custom_field_data) @@ @T@ "
                f"AND to_tsvector('simple'::regconfig, {CF_TEXT.format(alias='c')}) @@ @T@"
            )
        return found

    def search(self, ctx: Context) -> dict[str, Group]:
        cf, labels = text_fields(ctx.org)
        matched, term_params = _matched(self.sources(cf), ctx)
        archived = "" if ctx.include_archived else "AND c.archived_at IS NULL"
        cf_vector = f"setweight(to_tsvector('simple'::regconfig, {CF_TEXT.format(alias='c')}), 'C')"
        candidates = f"""
            SELECT 1, 0, 'numbers', concat_ws(' ', ranked.contract_number, ranked.po_number)
            UNION ALL SELECT 2, 0, 'vendor', ranked.vendor_name
            UNION ALL SELECT 3, 0, 'notes', ranked.notes{CF_SNIPPETS.format(alias="ranked")}
            UNION ALL SELECT 7, 0, 'details', ranked.cost_center"""
        sql = f"""
WITH matched AS (
    {matched}
),
ranked AS (
    SELECT c.id, c.name, c.archived_at, c.contract_number, c.po_number, c.notes, c.cost_center,
        c.custom_field_data AS cfdata, v.name AS vendor_name, count(*) OVER () AS total,
        ts_rank(
            c.search_vector
            || coalesce(setweight(ts_filter(v.search_vector, '{{a}}'), 'B'), ''::tsvector)
            || coalesce({cf_vector}, ''::tsvector),
            to_tsquery('simple'::regconfig, %(all)s)
        ) + {_bonus("c.name")} AS score
    FROM matched
    JOIN money_contract c ON c.id = matched.id
    LEFT JOIN catalog_vendor v ON v.id = c.vendor_id
    WHERE c.organization_id = %(org)s {archived}
    ORDER BY score DESC, lower(c.name), c.id
    LIMIT %(limit)s
)
SELECT ranked.id, ranked.name, ranked.archived_at IS NOT NULL, ranked.score, ranked.total, snip.field, {HEADLINE}
FROM ranked {SNIPPET.format(candidates=candidates)}
ORDER BY ranked.score DESC, lower(ranked.name), ranked.id
"""
        group = Group("contract")
        for cid, name, archived_flag, score, total, field, headline in _rows(sql, {**_common(ctx, cf), **term_params}):
            group.count = total
            group.results.append(
                Hit(
                    str(cid),
                    "contract",
                    name,
                    f"contracts/{cid}",
                    float(score),
                    archived=archived_flag,
                    snippet=self.snippet(field, headline, labels),
                )
            )
        return {"contract": group}

    @staticmethod
    def snippet(field, headline, labels) -> dict[str, Any] | None:
        if field is None:
            return None
        if field.startswith("cf:"):
            return snippet("custom_field", labels.get(field[3:], field[3:]), headline)
        return snippet(field, LABELS[field], headline)

    def similar(self, ctx: Context) -> dict[str, Group]:
        archived = "" if ctx.include_archived else "AND c.archived_at IS NULL"
        sql = f"""
SELECT c.id, c.name, c.archived_at IS NOT NULL, word_similarity(%(lowered)s, c.name) AS score,
    count(*) OVER () AS total
FROM money_contract c
WHERE c.organization_id = %(org)s AND %(lowered)s <%% c.name {archived}
ORDER BY score DESC, lower(c.name), c.id
LIMIT %(limit)s
"""
        group = Group("contract")
        params = {"org": ctx.org.pk, "lowered": ctx.query.lowered, "limit": ctx.limit}
        for cid, name, archived_flag, score, total in _similar_rows(sql, params):
            group.count = total
            group.results.append(Hit(str(cid), "contract", name, f"contracts/{cid}", float(score), archived_flag))
        return {"contract": group}


def register() -> None:
    """Called once from MoneyConfig.ready()."""
    core_search.register(ContractSearcher())
