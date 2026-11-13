"""Global search over the catalog (the registry and the response are core/search.py).

Four searchers, registered in CatalogConfig.ready():

- records: applications, components, integrations and AI agents. A record matches on its own vector (name A,
  description B, cost centre D), its kind's row (purpose or data purpose B, other free text D), its vendor's name (an
  application), its tags, its AI models and their providers (an agent) and its text-like custom field values (text,
  long text, URL, choice and multiple choice). Every word must match somewhere in those; each word may match in a
  different one ("contoso payroll" finds Payroll whose vendor is Contoso). Archived records are left out unless asked.
- vendors: name A, notes D, custom field values; with `vendor_risk` also the risk file's notes and its documents'
  titles and notes (matched, not ranked or quoted in the snippet, and read without an index: an
  organization's risk files are a few hundred rows).
- people: the name only. An anonymized person's vector is empty (migration 0031), so they never match; people
  marked inactive count as archived.
- attachments (the `attachments` capability): title A, file name B. One on an archived record (or contract) is
  archived; one on an AI agent needs the `ai_agents` capability as well, one on a contract the `contracts` one.

Per kind, the score is `ts_rank` over everything the record matched through (re-weighted: the vendor, tags and models
count as B, custom values as C) plus 1 when the name equals the query and 0.5 when it starts with it. Each searcher is
one SQL statement; the matching uses only the GIN indexes of migration 0031, then ranks the matched rows."""

from __future__ import annotations

from typing import Any

from django.db import connection, transaction

from core import search as core_search
from core.capabilities import AI_AGENTS, ATTACHMENTS, CATALOG, CONTRACTS, CUSTOM_FIELDS, VENDOR_RISK, has_capability
from core.search import Context, Group, Hit, Searcher, snippet

from . import customfields
from .models import KIND_PATHS, CustomFieldType, Kind

# Custom field types whose values are text people search for (dates, numbers, yes/no and JSON objects are not).
TEXT_TYPES = (
    CustomFieldType.TEXT,
    CustomFieldType.LONGTEXT,
    CustomFieldType.URL,
    CustomFieldType.SELECT,
    CustomFieldType.MULTISELECT,
)
RECORD_SEARCH_KINDS = (Kind.APPLICATION.value, Kind.COMPONENT.value, Kind.INTEGRATION.value, Kind.AI_AGENT.value)
PATHS = {**KIND_PATHS, Kind.INTEGRATION: "integrations"}
# Typo matching: trigram word similarity of the whole query to a name, at least this (pg_trgm's default is 0.6).
SIMILARITY = 0.5

TSQ = "to_tsquery('simple'::regconfig, %({})s)"


def _matched(sources: list[str], ctx: Context) -> tuple[str, dict[str, str]]:
    """SQL for the IDs where every word matches at least one source: (source 1 UNION source 2 ...) for each word,
    INTERSECTed. In a source, @T@ is the word's tsquery and @A@ the same restricted to weight A (names)."""
    blocks, params = [], {}
    for i in range(len(ctx.query.terms)):
        params[f"t{i}"] = ctx.query.term(i)
        params[f"a{i}"] = ctx.query.term(i, "A")
        union = "\n        UNION ".join(
            s.replace("@T@", TSQ.format(f"t{i}")).replace("@A@", TSQ.format(f"a{i}")) for s in sources
        )
        blocks.append(f"({union})")
    return "\n    INTERSECT ".join(blocks), params


def _text_fields(org) -> tuple[list[str], dict[str, str]]:
    """The organization's text-like custom field names in display order, and their labels; empty without the
    custom_fields capability."""
    if not has_capability(org, CUSTOM_FIELDS):
        return [], {}
    fields = [f for f in customfields.definitions(org) if f.type in TEXT_TYPES]
    return [f.name for f in fields], {f.name: f.label for f in fields}


def _common(ctx: Context, cf: list[str]) -> dict[str, Any]:
    return {
        "org": ctx.org.pk,
        "all": ctx.query.all,
        "any": ctx.query.any,
        "lowered": ctx.query.lowered,
        "limit": ctx.limit,
        "cf": cf,
        "markers": core_search.MARKERS,
        "headline": core_search.HEADLINE_OPTIONS,
    }


def _rows(sql: str, params: dict[str, Any]) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _similar_rows(sql: str, params: dict[str, Any]) -> list[tuple]:
    """Trigram rows with the word-similarity threshold set for this transaction only (so `<%` can use the index)."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)", [str(SIMILARITY)])
        cursor.execute(sql, params)
        return cursor.fetchall()


def _bonus(column: str) -> str:
    return (
        f"CASE WHEN lower({column}) = %(lowered)s THEN 1.0 "
        f"WHEN starts_with(lower({column}), %(lowered)s) THEN 0.5 ELSE 0.0 END"
    )


# The text-like custom values of row alias `alias` as one JSON object (NULL when there are none): what may match.
CF_TEXT = (
    "(SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each({alias}.custom_field_data) e "
    "WHERE e.key = ANY(%(cf)s::text[]))"
)
# Custom values as snippet candidates (order 6, then the field's display order): the label comes from Python.
CF_SNIPPETS = """
            UNION ALL SELECT 6, array_position(%(cf)s::text[], e.key), 'cf:' || e.key,
                CASE jsonb_typeof(e.value)
                    WHEN 'array' THEN (SELECT string_agg(x, ', ') FROM jsonb_array_elements_text(e.value) x)
                    ELSE e.value #>> '{{}}' END
            FROM jsonb_each({alias}.cfdata) e WHERE e.key = ANY(%(cf)s::text[])"""
# The first candidate (ord, sub, field, body) that holds a word of the query, as (field, body); see core/search.py.
SNIPPET = """
    LEFT JOIN LATERAL (
        SELECT s.field, s.body FROM ({candidates}
        ) s(ord, sub, field, body)
        WHERE s.body IS NOT NULL AND s.body <> ''
            AND to_tsvector('simple'::regconfig, s.body) @@ to_tsquery('simple'::regconfig, %(any)s)
        ORDER BY s.ord, s.sub
        LIMIT 1
    ) snip ON true"""
HEADLINE = (
    "ts_headline('simple'::regconfig, translate(snip.body, %(markers)s, ''), "
    "to_tsquery('simple'::regconfig, %(any)s), %(headline)s)"
)


class RecordSearcher(Searcher):
    kinds = RECORD_SEARCH_KINDS
    capabilities = {
        Kind.APPLICATION.value: CATALOG,
        Kind.COMPONENT.value: CATALOG,
        Kind.INTEGRATION.value: CATALOG,
        Kind.AI_AGENT.value: AI_AGENTS,
    }
    LABELS = {
        "description": "Description",
        "purpose": "Purpose",
        "vendor": "Vendor",
        "tags": "Tags",
        "ai_models": "AI models",
        "details": "Details",
    }

    def sources(self, kinds: tuple[str, ...], cf: list[str]) -> list[str]:
        org = "organization_id = %(org)s"
        found = [
            f"SELECT id FROM catalog_resource WHERE {org} AND kind = ANY(%(kinds)s::text[]) AND search_vector @@ @T@"
        ]
        for kind, table in (
            ("application", "catalog_application"),
            ("component", "catalog_component"),
            ("ai_agent", "catalog_aiagent"),
            ("integration", "catalog_integration"),
        ):
            if kind in kinds:
                found.append(f"SELECT resource_id FROM {table} WHERE {org} AND search_vector @@ @T@")
        if "application" in kinds:
            found.append(
                "SELECT x.resource_id FROM catalog_application x JOIN catalog_vendor v ON v.id = x.vendor_id "
                "WHERE v.organization_id = %(org)s AND v.search_vector @@ @A@"
            )
        if "ai_agent" in kinds:
            found += [
                "SELECT am.agent_id FROM catalog_agentmodel am JOIN catalog_aimodel m ON m.id = am.model_id "
                "WHERE m.organization_id = %(org)s AND m.search_vector @@ @A@",
                "SELECT am.agent_id FROM catalog_agentmodel am JOIN catalog_aimodel m ON m.id = am.model_id "
                "JOIN catalog_aiprovider p ON p.id = m.provider_id "
                "WHERE p.organization_id = %(org)s AND p.search_vector @@ @A@",
            ]
        found.append(
            "SELECT rt.resource_id FROM catalog_resourcetag rt JOIN catalog_tag t ON t.id = rt.tag_id "
            "WHERE t.organization_id = %(org)s AND t.search_vector @@ @A@"
        )
        if cf:
            found.append(
                f"SELECT id FROM catalog_resource r WHERE r.{org} AND r.kind = ANY(%(kinds)s::text[]) "
                "AND to_tsvector('simple'::regconfig, r.custom_field_data) @@ @T@ "
                f"AND to_tsvector('simple'::regconfig, {CF_TEXT.format(alias='r')}) @@ @T@"
            )
        return found

    # A record's tags and its agent's models (provider, model, version), aggregated by `agg` for row alias `alias`.
    TAGS = (
        "(SELECT {agg} FROM catalog_resourcetag rt JOIN catalog_tag t ON t.id = rt.tag_id "
        "WHERE rt.resource_id = {alias}.id)"
    )
    MODELS = (
        "(SELECT {agg} FROM catalog_agentmodel am JOIN catalog_aimodel m ON m.id = am.model_id "
        "JOIN catalog_aiprovider p ON p.id = m.provider_id WHERE am.agent_id = {alias}.id)"
    )
    MODEL_NAME = "p.name || ' ' || m.name || CASE WHEN m.version = '' THEN '' ELSE ' ' || m.version END"

    def search(self, ctx: Context) -> dict[str, Group]:
        cf, labels = _text_fields(ctx.org)
        matched, term_params = _matched(self.sources(ctx.kinds, cf), ctx)
        archived = "" if ctx.include_archived else "AND r.archived_at IS NULL"
        as_vector = "setweight(to_tsvector('simple'::regconfig, string_agg({}, ' ')), 'B')"
        tag_vector = self.TAGS.format(agg=as_vector.format("t.name"), alias="r")
        model_vector = self.MODELS.format(agg=as_vector.format(self.MODEL_NAME), alias="r")
        cf_vector = f"setweight(to_tsvector('simple'::regconfig, {CF_TEXT.format(alias='r')}), 'C')"
        tag_text = self.TAGS.format(agg="string_agg(t.name, ', ' ORDER BY lower(t.name))", alias="ranked")
        model_text = self.MODELS.format(
            agg=f"string_agg({self.MODEL_NAME}, ', ' ORDER BY lower(p.name), lower(m.name))", alias="ranked"
        )
        candidates = f"""
            SELECT 1, 0, 'description', ranked.description
            UNION ALL SELECT 2, 0, 'purpose', ranked.purpose
            UNION ALL SELECT 3, 0, 'vendor', ranked.vendor_name
            UNION ALL SELECT 4, 0, 'tags', {tag_text}
            UNION ALL SELECT 5, 0, 'ai_models', {model_text}{CF_SNIPPETS.format(alias="ranked")}
            UNION ALL SELECT 7, 0, 'details', ranked.details"""
        sql = f"""
WITH matched AS (
    {matched}
),
scored AS (
    SELECT r.id, r.kind, r.name, r.lifecycle, r.archived_at, r.description, r.custom_field_data AS cfdata,
        coalesce(a.purpose, c.purpose, g.purpose, i.data_purpose, '') AS purpose,
        v.name AS vendor_name,
        concat_ws(' ', a.version, a.entra_app_id, a.service_principal_id, c.host_runtime, c.schedule_or_trigger,
            c.azure_resource_id, c.external_id, g.knowledge_sources, g.tools_connectors, g.data_access_scope,
            g.run_as_identity, g.external_id, i.protocol, i.schedule, r.cost_center) AS details,
        ts_rank(
            r.search_vector
            || coalesce(a.search_vector, c.search_vector, g.search_vector, i.search_vector, ''::tsvector)
            || coalesce(setweight(ts_filter(v.search_vector, '{{a}}'), 'B'), ''::tsvector)
            || coalesce({tag_vector}, ''::tsvector)
            || CASE WHEN g.resource_id IS NULL THEN ''::tsvector ELSE coalesce({model_vector}, ''::tsvector) END
            || coalesce({cf_vector}, ''::tsvector),
            to_tsquery('simple'::regconfig, %(all)s)
        ) + {_bonus("r.name")} AS score
    FROM matched
    JOIN catalog_resource r ON r.id = matched.id
    LEFT JOIN catalog_application a ON a.resource_id = r.id
    LEFT JOIN catalog_vendor v ON v.id = a.vendor_id
    LEFT JOIN catalog_component c ON c.resource_id = r.id
    LEFT JOIN catalog_aiagent g ON g.resource_id = r.id
    LEFT JOIN catalog_integration i ON i.resource_id = r.id
    WHERE r.organization_id = %(org)s AND r.kind = ANY(%(kinds)s::text[]) {archived}
),
ranked AS (
    SELECT scored.*, row_number() OVER (PARTITION BY kind ORDER BY score DESC, lower(name), id) AS rn,
        count(*) OVER (PARTITION BY kind) AS total
    FROM scored
)
SELECT ranked.id, ranked.kind, ranked.name, ranked.lifecycle, ranked.archived_at IS NOT NULL, ranked.score,
    ranked.total, snip.field, {HEADLINE}
FROM ranked {SNIPPET.format(candidates=candidates)}
WHERE ranked.rn <= %(limit)s
ORDER BY ranked.kind, ranked.rn
"""
        params = {**_common(ctx, cf), **term_params, "kinds": list(ctx.kinds)}
        groups = {kind: Group(kind) for kind in ctx.kinds}
        for rid, kind, name, lifecycle, archived_flag, score, total, field, headline in _rows(sql, params):
            group = groups[kind]
            group.count = total
            group.results.append(
                Hit(
                    id=str(rid),
                    kind=kind,
                    name=name,
                    path=f"{PATHS[kind]}/{rid}",
                    score=float(score),
                    archived=archived_flag,
                    lifecycle=lifecycle,
                    snippet=self.snippet(kind, field, headline, labels),
                )
            )
        return groups

    def snippet(self, kind, field, headline, labels) -> dict[str, Any] | None:
        if field is None:
            return None
        if field.startswith("cf:"):
            return snippet("custom_field", labels.get(field[3:], field[3:]), headline)
        label = "Data purpose" if field == "purpose" and kind == Kind.INTEGRATION else self.LABELS[field]
        return snippet(field, label, headline)

    def similar(self, ctx: Context) -> dict[str, Group]:
        archived = "" if ctx.include_archived else "AND r.archived_at IS NULL"
        sql = f"""
SELECT id, kind, name, lifecycle, archived, score, total FROM (
    SELECT r.id, r.kind, r.name, r.lifecycle, r.archived_at IS NOT NULL AS archived,
        word_similarity(%(lowered)s, r.name) AS score,
        row_number() OVER (PARTITION BY r.kind ORDER BY word_similarity(%(lowered)s, r.name) DESC, lower(r.name), r.id)
            AS rn,
        count(*) OVER (PARTITION BY r.kind) AS total
    FROM catalog_resource r
    WHERE r.organization_id = %(org)s AND r.kind = ANY(%(kinds)s::text[]) AND %(lowered)s <%% r.name {archived}
) close_names WHERE rn <= %(limit)s ORDER BY kind, rn
"""
        params = {"org": ctx.org.pk, "lowered": ctx.query.lowered, "kinds": list(ctx.kinds), "limit": ctx.limit}
        groups = {kind: Group(kind) for kind in ctx.kinds}
        for rid, kind, name, lifecycle, archived_flag, score, total in _similar_rows(sql, params):
            groups[kind].count = total
            groups[kind].results.append(
                Hit(str(rid), kind, name, f"{PATHS[kind]}/{rid}", float(score), archived_flag, lifecycle)
            )
        return groups


class VendorSearcher(Searcher):
    kinds = ("vendor",)
    capabilities = {"vendor": CATALOG}

    def search(self, ctx: Context) -> dict[str, Group]:
        cf, labels = _text_fields(ctx.org)
        sources = ["SELECT id FROM catalog_vendor WHERE organization_id = %(org)s AND search_vector @@ @T@"]
        if cf:
            sources.append(
                "SELECT id FROM catalog_vendor v WHERE v.organization_id = %(org)s "
                "AND to_tsvector('simple'::regconfig, v.custom_field_data) @@ @T@ "
                f"AND to_tsvector('simple'::regconfig, {CF_TEXT.format(alias='v')}) @@ @T@"
            )
        if has_capability(ctx.org, VENDOR_RISK):
            sources += [
                "SELECT vendor_id FROM catalog_vendorriskprofile WHERE organization_id = %(org)s "
                "AND to_tsvector('simple'::regconfig, notes) @@ @T@",
                "SELECT vendor_id FROM catalog_vendorriskdocument WHERE organization_id = %(org)s "
                "AND to_tsvector('simple'::regconfig, title || ' ' || notes) @@ @T@",
            ]
        matched, term_params = _matched(sources, ctx)
        notes = f"""
            SELECT 2, 0, 'notes', ranked.notes{CF_SNIPPETS.format(alias="ranked")}"""
        sql = f"""
WITH matched AS (
    {matched}
),
ranked AS (
    SELECT v.id, v.name, v.notes, v.custom_field_data AS cfdata, count(*) OVER () AS total,
        ts_rank(
            v.search_vector
            || coalesce(setweight(to_tsvector('simple'::regconfig, {CF_TEXT.format(alias="v")}), 'C'), ''::tsvector),
            to_tsquery('simple'::regconfig, %(all)s)
        ) + {_bonus("v.name")} AS score
    FROM matched JOIN catalog_vendor v ON v.id = matched.id
    WHERE v.organization_id = %(org)s
    ORDER BY score DESC, lower(v.name), v.id
    LIMIT %(limit)s
)
SELECT ranked.id, ranked.name, ranked.score, ranked.total, snip.field, {HEADLINE}
FROM ranked {SNIPPET.format(candidates=notes)}
ORDER BY ranked.score DESC, lower(ranked.name), ranked.id
"""
        group = Group("vendor")
        for vid, name, score, total, field, headline in _rows(sql, {**_common(ctx, cf), **term_params}):
            group.count = total
            if field is None:
                found = None
            elif field == "notes":
                found = snippet("notes", "Notes", headline)
            else:
                found = snippet("custom_field", labels.get(field[3:], field[3:]), headline)
            group.results.append(Hit(str(vid), "vendor", name, f"vendors/{vid}", float(score), snippet=found))
        return {"vendor": group}

    def similar(self, ctx: Context) -> dict[str, Group]:
        sql = """
SELECT v.id, v.name, word_similarity(%(lowered)s, v.name) AS score, count(*) OVER () AS total
FROM catalog_vendor v
WHERE v.organization_id = %(org)s AND %(lowered)s <%% v.name
ORDER BY score DESC, lower(v.name), v.id
LIMIT %(limit)s
"""
        group = Group("vendor")
        params = {"org": ctx.org.pk, "lowered": ctx.query.lowered, "limit": ctx.limit}
        for vid, name, score, total in _similar_rows(sql, params):
            group.count = total
            group.results.append(Hit(str(vid), "vendor", name, f"vendors/{vid}", float(score)))
        return {"vendor": group}


class PersonSearcher(Searcher):
    """Names only; never an anonymized person (their vector is empty, and the typo match skips them too)."""

    kinds = ("person",)
    capabilities = {"person": CATALOG}

    def _hits(self, rows) -> dict[str, Group]:
        group = Group("person")
        for pid, name, inactive, score, total in rows:
            group.count = total
            group.results.append(Hit(str(pid), "person", name, f"people/{pid}", float(score), archived=inactive))
        return {"person": group}

    def search(self, ctx: Context) -> dict[str, Group]:
        active = "" if ctx.include_archived else "AND p.active"
        sql = f"""
SELECT p.id, p.name, NOT p.active,
    ts_rank(p.search_vector, to_tsquery('simple'::regconfig, %(all)s)) + {_bonus("p.name")} AS score,
    count(*) OVER () AS total
FROM catalog_person p
WHERE p.organization_id = %(org)s AND p.search_vector @@ to_tsquery('simple'::regconfig, %(all)s) {active}
ORDER BY score DESC, lower(p.name), p.id
LIMIT %(limit)s
"""
        params = {"org": ctx.org.pk, "all": ctx.query.all, "lowered": ctx.query.lowered, "limit": ctx.limit}
        return self._hits(_rows(sql, params))

    def similar(self, ctx: Context) -> dict[str, Group]:
        active = "" if ctx.include_archived else "AND p.active"
        sql = f"""
SELECT p.id, p.name, NOT p.active, word_similarity(%(lowered)s, p.name) AS score, count(*) OVER () AS total
FROM catalog_person p
WHERE p.organization_id = %(org)s AND p.anonymized_at IS NULL AND %(lowered)s <%% p.name {active}
ORDER BY score DESC, lower(p.name), p.id
LIMIT %(limit)s
"""
        params = {"org": ctx.org.pk, "lowered": ctx.query.lowered, "limit": ctx.limit}
        return self._hits(_similar_rows(sql, params))


class AttachmentSearcher(Searcher):
    """Titles (A) and file names (B) of files and links. An attachment is shown with its parent, so it follows the
    parent's visibility: archived with an archived record, and on an AI agent only with the ai_agents capability."""

    kinds = ("attachment",)
    capabilities = {"attachment": ATTACHMENTS}

    def search(self, ctx: Context) -> dict[str, Group]:
        parent_kinds = [k for k in RECORD_SEARCH_KINDS if k != Kind.AI_AGENT or has_capability(ctx.org, AI_AGENTS)]
        archived = "" if ctx.include_archived else "AND r.archived_at IS NULL"
        contract_archived = "" if ctx.include_archived else "AND c.archived_at IS NULL"
        contracts = has_capability(ctx.org, CONTRACTS)
        sql = f"""
WITH ranked AS (
    SELECT at.id, at.title, at.filename, r.id AS rid, r.kind AS rkind, r.name AS rname,
        (r.archived_at IS NOT NULL OR c.archived_at IS NOT NULL) AS rarchived, v.id AS vid, v.name AS vname,
        c.id AS cid, c.name AS cname, count(*) OVER () AS total,
        ts_rank(at.search_vector, to_tsquery('simple'::regconfig, %(all)s)) + {_bonus("at.title")} AS score
    FROM catalog_attachment at
    LEFT JOIN catalog_resource r ON r.id = at.resource_id
    LEFT JOIN catalog_vendor v ON v.id = at.vendor_id
    LEFT JOIN money_contract c ON c.id = at.contract_id
    WHERE at.organization_id = %(org)s AND at.search_vector @@ to_tsquery('simple'::regconfig, %(all)s)
        AND (at.resource_id IS NULL OR (r.kind = ANY(%(parents)s::text[]) {archived}))
        AND (at.contract_id IS NULL OR (%(contracts)s {contract_archived}))
    ORDER BY score DESC, lower(at.title), at.id
    LIMIT %(limit)s
)
SELECT ranked.id, ranked.title, ranked.rid, ranked.rkind, ranked.rname, ranked.rarchived, ranked.vid, ranked.vname,
    ranked.cid, ranked.cname, ranked.score, ranked.total,
    CASE WHEN to_tsvector('simple'::regconfig, ranked.filename) @@ to_tsquery('simple'::regconfig, %(any)s)
        THEN ts_headline('simple'::regconfig, translate(ranked.filename, %(markers)s, ''),
            to_tsquery('simple'::regconfig, %(any)s), %(headline)s) END
FROM ranked
ORDER BY ranked.score DESC, lower(ranked.title), ranked.id
"""
        params = {**_common(ctx, []), "parents": parent_kinds, "contracts": contracts}
        group = Group("attachment")
        for aid, title, rid, rkind, rname, rarchived, vid, vname, cid, cname, score, total, headline in _rows(
            sql, params
        ):
            group.count = total
            if rid is not None:
                parent = {"kind": rkind, "id": str(rid), "name": rname, "path": f"{PATHS[rkind]}/{rid}"}
            elif cid is not None:
                parent = {"kind": "contract", "id": str(cid), "name": cname, "path": f"contracts/{cid}"}
            else:
                parent = {"kind": "vendor", "id": str(vid), "name": vname, "path": f"vendors/{vid}"}
            group.results.append(
                Hit(
                    str(aid),
                    "attachment",
                    title,
                    f"attachments/{aid}",
                    float(score),
                    archived=bool(rarchived),
                    snippet=snippet("filename", "File name", headline),
                    parent=parent,
                )
            )
        return {"attachment": group}


def register() -> None:
    """Called once from CatalogConfig.ready()."""
    for searcher in (RecordSearcher(), VendorSearcher(), PersonSearcher(), AttachmentSearcher()):
        core_search.register(searcher)
